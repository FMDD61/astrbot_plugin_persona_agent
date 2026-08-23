"""vision — G15: image/sticker -> vision model -> short text (dsh view_image pattern).

Images (incl. gif stickers) are sent to a vision-capable model on the same
gateway; the short description is then fed to the text-only persona LLM.
Unknown images / failures degrade to None (caller keeps old behavior).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import threading
import time
from typing import Awaitable, Callable, Optional

# Common QQ built-in face ids -> names (zero-cost local map; images/stickers go to vision)
FACES = {
    0: "惊讶", 1: "撇嘴", 2: "色", 3: "发呆", 4: "得意", 5: "流泪", 6: "害羞",
    7: "闭嘴", 8: "睡", 9: "大哭", 10: "尴尬", 11: "发怒", 12: "调皮", 13: "呲牙",
    14: "微笑", 15: "难过", 16: "酷", 17: "汗", 18: "抓狂", 19: "吐", 20: "偷笑",
    21: "可爱", 22: "白眼", 23: "傲慢", 24: "饥饿", 25: "困", 26: "惊恐", 27: "流汗",
    28: "憨笑", 29: "悠闲", 30: "奋斗", 31: "咒骂", 32: "疑问", 33: "嘘", 34: "晕",
    35: "衰", 36: "骷髅", 37: "敲打", 38: "再见", 39: "擦汗", 40: "抠鼻", 41: "鼓掌",
    42: "糗大了", 43: "坏笑", 44: "左哼哼", 45: "右哼哼", 46: "哈欠", 47: "鄙视",
    48: "委屈", 49: "快哭了", 50: "阴险", 51: "亲亲", 52: "吓", 53: "可怜", 54: "菜刀",
    55: "西瓜", 56: "啤酒", 57: "篮球", 58: "乒乓", 59: "咖啡", 60: "饭", 61: "猪头",
    62: "玫瑰", 63: "凋谢", 64: "嘴唇", 65: "爱心", 66: "心碎", 67: "蛋糕", 68: "闪电",
    69: "炸弹", 70: "刀", 71: "足球", 72: "便便", 73: "月亮", 74: "太阳", 75: "礼物",
    76: "拥抱", 77: "强", 78: "弱", 79: "握手", 80: "胜利", 81: "抱拳", 82: "勾引",
    83: "拳头", 84: "差劲", 85: "爱你", 86: "NO", 87: "OK", 96: "干杯", 106: "瞌睡",
    116: "发疯", 126: "眨眼",
}


def face_name(fid: int) -> str:
    try:
        return FACES.get(int(fid)) or f"表情#{fid}"
    except (TypeError, ValueError):
        return f"表情#{fid}"


def sniff_mime(data: bytes) -> str:
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


async def resolve_image_bytes(img) -> Optional[bytes]:
    """Resolve a Comp.Image to bytes: 本地路径 -> convert_to_file_path
    (AstrBot MediaResolver handles url/base64/localfile), then manual
    fallbacks: base64:// and http(s) download."""
    try:
        path = await img.convert_to_file_path()
        if path:
            with open(path, "rb") as f:
                return f.read()
    except Exception:
        pass
    raw = getattr(img, "file", None) or ""
    if raw.startswith("base64://"):
        try:
            return base64.b64decode(raw[len("base64://"):])
        except Exception:
            return None
    if raw.startswith("http://") or raw.startswith("https://"):
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(raw)
                if r.status_code == 200:
                    return r.content
        except Exception:
            return None
    return None


PostFn = Callable[[str, dict], Awaitable[dict]]


class VisionService:
    """Vision description with per-image hash cache (TTL) and timeout."""

    def __init__(
        self,
        api_base: str,
        api_key: str,
        model: str = "mimo-v2.5",
        *,
        timeout: float = 15.0,
        cache_ttl: float = 30.0,
        desc_max_chars: int = 120,
        http_post: Optional[PostFn] = None,
    ) -> None:
        self._url = api_base.rstrip("/") + "/chat/completions"
        self._key = api_key
        self._model = model
        self._timeout = float(timeout)
        self._cache_ttl = float(cache_ttl)
        self._desc_max_chars = int(desc_max_chars)
        self._http_post = http_post  # injectable for tests
        self._cache: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    async def _post(self, payload: dict) -> dict:
        if self._http_post is not None:
            return await self._http_post(self._url, payload)
        import httpx
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(
                self._url,
                json=payload,
                headers={"Authorization": f"Bearer {self._key}"},
            )
            r.raise_for_status()
            return r.json()

    async def describe_bytes(self, data: bytes) -> Optional[str]:
        if not data:
            return None
        h = hashlib.sha256(data).hexdigest()
        with self._lock:
            hit = self._cache.get(h)
            if hit and time.time() - hit[0] < self._cache_ttl:
                return hit[1]
        try:
            b64 = base64.b64encode(data).decode()
            mime = sniff_mime(data)
            payload = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content":
                        "用中文简要描述图片中确定可见的内容，不超过80字；如是表情包说明其情绪和梗。不要猜测人物身份、不要脑补图中没有的内容；看不清就说看不清。"},
                    {"role": "user", "content": [
                        {"type": "text", "text": "描述这张图片。"},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ]},
                ],
                "max_tokens": 160,
                "temperature": 0.3,
            }
            out = await asyncio.wait_for(self._post(payload), timeout=self._timeout)
            desc = ((out.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            desc = str(desc).strip()[: self._desc_max_chars]
            if not desc:
                return None
            with self._lock:
                self._cache[h] = (time.time(), desc)
            return desc
        except Exception:
            return None

    async def describe_image(self, img) -> Optional[str]:
        data = await resolve_image_bytes(img)
        if not data:
            return None
        return await self.describe_bytes(data)