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
    """Vision description with per-image hash cache (TTL) and timeout.

    A7③ 持久哈希缓存：
      - 内存 TTL 缓存（现状，第一道，防连发）
      - 持久 JSON 缓存 image_desc_cache.json（第二道，跨重启复用描述；
        sha256 → {desc, last_ts, hits}；LRU 淘汰最久未命中；上限 persist_max）
      命中即刷新 last_ts + hits（LRU 语义：高频表情不被"早进缓存"误杀）。
      落盘节流：新增/命中仅更新内存，标脏后惰性 flush + terminate flush。
    """

    def __init__(
        self,
        api_base: str,
        api_key: str,
        model: str = "deepseek-v4-flash-vision-exp",
        *,
        timeout: float = 15.0,
        cache_ttl: float = 30.0,
        desc_max_chars: int = 120,
        reasoning_effort: str = "low",
        http_post: Optional[PostFn] = None,
        persist_path: Optional[str] = None,
        persist_max: int = 2000,
    ) -> None:
        self._url = api_base.rstrip("/") + "/chat/completions"
        self._key = api_key
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._timeout = float(timeout)
        self._cache_ttl = float(cache_ttl)
        self._desc_max_chars = int(desc_max_chars)
        self._http_post = http_post  # injectable for tests
        self._cache: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()
        # ---- A7③ 持久 LRU 缓存 ----
        self._persist_path = str(persist_path) if persist_path else ""
        self._persist_max = int(persist_max)
        self._persist: dict[str, dict] = {}   # sha256 -> {desc,last_ts,hits}
        self._persist_dirty = False           # 内存有未落盘变更
        self._evicted = 0                     # 淘汰计数（观测上限是否够）
        if self._persist_path:
            self._load_persist()

    # ---- A7③ 持久缓存 ----

    def _load_persist(self) -> None:
        """启动加载持久缓存（文件损坏/缺失则空）。"""
        try:
            import json as _json
            with open(self._persist_path, encoding="utf-8") as f:
                data = _json.load(f)
            if isinstance(data, dict):
                self._persist = {
                    k: {
                        "desc": str(v.get("desc", "")),
                        "last_ts": float(v.get("last_ts", 0.0)),
                        "hits": int(v.get("hits", 0)),
                    }
                    for k, v in data.items()
                    if isinstance(v, dict) and v.get("desc")
                }
        except Exception:
            self._persist = {}

    def _flush_persist(self) -> None:
        """落盘（原子写 tmp→rename）。仅在有脏数据时写；失败静默。"""
        if not self._persist_path or not self._persist_dirty:
            return
        import json as _json
        import os
        try:
            tmp = self._persist_path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                _json.dump(self._persist, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._persist_path)
            self._persist_dirty = False
        except OSError:
            pass  # 落盘失败不 crash

    def _touch_persist(self, h: str, desc: str) -> None:
        """命中/新增：更新 last_ts/hits + 标脏 + 超限 LRU 淘汰最久未用。"""
        now = time.time()
        entry = self._persist.get(h)
        if entry:
            entry["last_ts"] = now
            entry["hits"] = entry.get("hits", 0) + 1
        else:
            self._persist[h] = {"desc": desc, "last_ts": now, "hits": 1}
        self._persist_dirty = True
        # LRU 淘汰：超上限时移除 last_ts 最旧（最久未使用）的条目
        while len(self._persist) > self._persist_max and self._persist:
            oldest_k = min(self._persist, key=lambda k: self._persist[k]["last_ts"])
            del self._persist[oldest_k]
            self._evicted += 1

    def flush(self) -> None:
        """显式落盘（terminate/定时调用）。"""
        with self._lock:
            self._flush_persist()

    def snapshot(self) -> dict:
        """观测：缓存大小/上限/淘汰数/命中分布（判断 persist_max 是否够）。"""
        with self._lock:
            return {
                "persist_enabled": bool(self._persist_path),
                "persist_size": len(self._persist),
                "persist_max": self._persist_max,
                "persist_evicted": self._evicted,
                "hits_top": sorted(
                    (v.get("hits", 0) for v in self._persist.values()),
                    reverse=True,
                )[:10],
            }

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
            # A7③ 第二道：持久缓存命中 → 免调视觉模型（跨重启复用）
            if self._persist_path:
                pent = self._persist.get(h)
                if pent:
                    self._touch_persist(h, str(pent.get("desc", "")))
                    self._cache[h] = (time.time(), str(pent["desc"]))
                    return str(pent["desc"])
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
                "max_tokens": 512,
                "temperature": 0.3,
                "reasoning_effort": self._reasoning_effort,
            }
            out = await asyncio.wait_for(self._post(payload), timeout=self._timeout)
            desc = ((out.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            desc = str(desc).strip()[: self._desc_max_chars]
            if not desc:
                return None
            with self._lock:
                self._cache[h] = (time.time(), desc)
                # A7③: 新描述写持久层（标脏，惰性 flush）
                if self._persist_path:
                    self._touch_persist(h, desc)
            return desc
        except Exception:
            return None

    async def describe_image(self, img) -> Optional[str]:
        data = await resolve_image_bytes(img)
        if not data:
            return None
        return await self.describe_bytes(data)