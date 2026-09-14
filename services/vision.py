"""vision — G15: image/sticker -> vision model -> short text (dsh view_image pattern).

Images (incl. gif stickers) are sent to a vision-capable model on the same
gateway; the short description is then fed to the text-only persona LLM.
Unknown images / failures degrade to None (caller keeps old behavior).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
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


# 🔴 S6（2026-09-14，956 次实测标定）：**约定固定返回格式是唯一主导因素**。
#
# 失败机理（191/191 无例外）：content 为空的响应 `finish_reason` **全是 `length`**，
# 且 `reasoning_tokens ≈ max_tokens` —— 模型把预算花在**"输出格式谈判"**上
# （自由文本 prompt 没规定输出形状，它反复推演"要描述？情绪？梗？关键词？多少字？"），
# 正文一个 token 都没轮到。截断处原文可作证：
#   「…关键词：颓废、趴桌、困倦、摆烂。**回答格式：**可见：白发戴灰蝴蝶结的…」
# 约定 JSON schema 后格式谈判消失，reasoning 从"顶格"降到 **p50≈150**。
#
# 实测可用率：现网自由文本 **31.2%** → JSON schema **100%**（n=144，0 截断）。
# 53 张历史失败图：现网 15.1% → 推荐配置 **100%**。
# 稳定性（同图重复）：现网 24 图里 **7 图结果翻转**；推荐配置 72/72 全成功、0 翻转。
VISION_SYSTEM_PROMPT = (
    "你是图片标注器。看图片，只输出**一个 JSON 对象**，不要任何解释、不要 markdown 代码块。"
    '格式固定为：{"desc": "图片描述", "tags": ["关键词1", "关键词2"]}\n'
    "字段约束：\n"
    '- "desc"：中文字符串，只描述图中**确定可见**的内容，长度 8-80 字，一句话写完，不加换行；'
    "若是表情包，在句中点明情绪（如委屈/无语/嘲讽）与可能的梗；不确定的不要写。"
    "**如果是动图，重点说明它在动什么（动作过程）**，不要只描述静帧细节。\n"
    '- "tags"：2-4 个中文字符串，每个 2-6 字，是可用于检索的情绪/动作关键词（如「无语」「抱头」「流泪」）。\n'
    "不要猜测人物身份、不要脑补图中没有的内容；看不清就在 desc 里写「画面模糊，看不清」。"
)

# 提示词复述/自我规训特征（模型把 system 要求或思考过程写进"答案"）
_LEAK_MARKERS = (
    "如是表情包", "不超过80", "不猜测人物", "不要脑补", "需要谨慎", "不能猜",
    "用户说", "本条要求", "分析请求", "草拟描述", "字数检查", "输出格式",
)


def _looks_like_leaked_prompt(text: str) -> bool:
    t = text or ""
    return any(m in t for m in _LEAK_MARKERS)


def parse_vision_json(resp: dict) -> tuple[str, list[str]]:
    """从响应里取 ``(desc, tags)``，容忍各种残缺形态。

    顺序：content/reasoning → 去 ``` 围栏 → ``json.loads`` → 正则抠第一个 ``{...}``
    → 正则只抠 ``"desc"`` → 都失败返回空。
    实测在 442 条真实响应上 **441/442 = 99.8%** 能拿到 desc + 2~4 tags。
    """
    import re as _re

    msg = ((resp.get("choices") or [{}])[0].get("message") or {})

    def pull(text):
        t = str(text or "").strip()
        if not t:
            return None
        if t.startswith("```"):
            t = _re.sub(r"^```[a-zA-Z]*\s*", "", t)
            t = _re.sub(r"\s*```$", "", t).strip()
        try:
            o = json.loads(t)
            if isinstance(o, dict) and o.get("desc"):
                return o
        except Exception:
            pass
        m = _re.search(r"\{.*\}", t, _re.S)
        if m:
            try:
                o = json.loads(m.group(0))
                if isinstance(o, dict) and o.get("desc"):
                    return o
            except Exception:
                pass
        m = _re.search(r'"desc"\s*:\s*"([^"]{2,})"', t)
        return {"desc": m.group(1)} if m else None

    for src in (msg.get("content"), msg.get("reasoning"), msg.get("reasoning_content")):
        o = pull(src)
        if not o:
            continue
        desc = str(o.get("desc") or "").strip()
        if not desc or _looks_like_leaked_prompt(desc):
            continue
        tags = [str(t).strip() for t in (o.get("tags") or []) if str(t).strip()][:4]
        return desc, tags
    return "", []


# 图像预处理（降采样/动图）抽到中性模块，与离线入库工具共用。
# 见 services/image_prep.py 的 docstring：实测不降采样会让同一张图的
# prompt_tokens 完全相同、而时延从 4.0s 涨到 29.6s（最坏 127.5s）。
from .image_prep import (
    DESC_MAX_EDGE,
    GIF_INLINE_MAX_BYTES,
    mime_of as _mime_of,
    prepare_bytes_for_vision,
    sniff_mime,
)


async def resolve_image_bytes(img, diag: Optional[dict] = None) -> Optional[bytes]:
    """Resolve a Comp.Image to bytes: 本地路径 -> convert_to_file_path
    (AstrBot MediaResolver handles url/base64/localfile), then manual
    fallbacks: base64:// and http(s) download.

    ``diag``：可选诊断字典。**三条取字节路径全部静默 return None**，
    失败时无法区分"取不到图"与"模型没描述"（2026-09-13 实测有 5/14 次
    `无法识别`，却查不出是哪一层）。这里把每级失败原因记进去。

    返回的 bytes 会附在 ``diag["bytes"]``（仅诊断用，调用方负责不入日志）。
    """
    def _fail(where: str, exc: Optional[BaseException] = None) -> None:
        if diag is None:
            return
        diag.setdefault("resolve_fail", where)
        if exc is not None:
            diag.setdefault("resolve_error", f"{type(exc).__name__}: {exc}")

    try:
        path = await img.convert_to_file_path()
        if path:
            with open(path, "rb") as f:
                data = f.read()
            if diag is not None:
                diag["resolve_via"] = "file_path"
            return data
        _fail("convert_to_file_path returned empty")
    except Exception as e:
        _fail("convert_to_file_path raised", e)
    raw = getattr(img, "file", None) or ""
    if raw.startswith("base64://"):
        try:
            data = base64.b64decode(raw[len("base64://"):])
            if diag is not None:
                diag["resolve_via"] = "base64"
            return data
        except Exception as e:
            _fail("base64 decode failed", e)
            return None
    if raw.startswith("http://") or raw.startswith("https://"):
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(raw)
                if r.status_code == 200:
                    if diag is not None:
                        diag["resolve_via"] = "http"
                    return r.content
                _fail(f"http status {r.status_code}")
        except Exception as e:
            _fail("http download raised", e)
            return None
    _fail(f"unresolvable file={str(raw)[:40]!r}")
    return None


PostFn = Callable[[str, dict], Awaitable[dict]]


def extract_completion_text(resp: dict) -> str:
    """从网关响应取描述文本（**保留作兼容/测试用**；主路径已改为 `parse_vision_json`）。

    ⚠️ S6 标定修正了一处认知：原先以为"模型把答案只写进 reasoning 就收尾"，
    但 191/191 条空 content 的响应 `finish_reason` **全是 `length`** —— 真相是
    **话没说完就被砍断**（见 `VISION_SYSTEM_PROMPT` 上方注释）。本函数仍可用作
    兜底，但**不能**指望它救回截断的响应：实测它只救回 18.8% 的空返回。

    🔴 2026-09-13 实测抓到（识图间歇失败的根因）：部分图片模型会把最终答案
    写进 **`reasoning`** 字段、`content` 留空，例如

        {"content": "", "reasoning": "1. 分析请求… 5. 最后输出：五个Q版角色站于草地…"}

    只读 `content` → 空串 → 调用方渲染成「（配图：无法识别）」，
    **看起来像"识别不了"，实为解析漏了一个字段**（与 B-005 同形）。

    优先级：`content` → `reasoning` 里的"最后输出/结论"段 → reasoning 末行。
    """
    msg = ((resp.get("choices") or [{}])[0].get("message") or {})
    text = str(msg.get("content") or "").strip()
    if text and not _looks_like_leaked_prompt(text):
        return text
    reasoning = str(msg.get("reasoning") or msg.get("reasoning_content") or "").strip()
    if not reasoning:
        return ""
    import re as _re
    for pat in (r"(?:最后输出|最终输出|最终润色|最终答案|结论)[：:]?\s*([^\n]{2,})",
                r"(?:输出|答案)[：:]?\s*([^\n]{2,})$"):
        m = _re.search(pat, reasoning)
        if m:
            cand = m.group(1).strip()
            return "" if _looks_like_leaked_prompt(cand) else cand
    # 兜底：只在**极短**时取末行 —— 长 reasoning 的末行几乎必然是思考片段
    # （实测："用户要求用中文简要描述，不超过80字，如果是表情包要说明情绪…"
    #  这种"我在分析"的句子会被当成图片描述）。宁可返回空让上游重试。
    tail = [l.strip() for l in reasoning.splitlines() if l.strip()]
    cand = tail[-1] if tail else ""
    if len(cand) <= 40 and not _looks_like_leaked_prompt(cand):
        return cand
    return ""


# 提示词复述/自我规训特征（模型把 system 要求或思考过程写进"答案"）
_LEAK_MARKERS = (
    "如是表情包", "不超过80", "不猜测人物", "不要脑补", "需要谨慎", "不能猜",
    "用户说", "本条要求", "分析请求", "草拟描述", "字数检查",
)


def _looks_like_leaked_prompt(text: str) -> bool:
    t = text or ""
    return any(m in t for m in _LEAK_MARKERS)


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
        flush_every: int = 5,
        flush_interval: float = 30.0,
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
        # ---- 流式落盘（2026-09-13，B-014 同批）：原实现只在 terminate 落盘，
        #      实测 7 次成功识图后文件仍不存在 → 跨重启复用完全没生效。
        self._flush_every = max(1, int(flush_every))
        self._flush_interval = max(1.0, float(flush_interval))
        self._dirty_since_flush = 0
        self._last_flush_ts = time.time()
        self._flush_count = 0
        self._flushed_entries = 0
        # S0 观测：最近一次失败原因 + 分类计数（与 emotion/gate 同一套）。
        # 「无法识别」在调用方看来是单一结果，但底层至少有 4 种成因
        # （取不到图 / 模型空返回 / 异常 / 超时）—— 不分开就只能猜。
        self.last_error: Optional[str] = None
        self.stats = {"ok": 0, "cache_hit": 0, "empty": 0, "timeout": 0, "error": 0}
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

    def _touch_persist(self, h: str, desc: str) -> None:
        """命中/新增：更新 last_ts/hits + 标脏 + 超限 LRU 淘汰最久未用。

        **流式落盘（2026-09-13 修）**：原实现只标脏，而 `_flush_persist` 的
        唯一调用点是 `main.terminate()` —— 一旦进程被 SIGKILL/强退，所有描述
        全部丢失。实测跑了 7 次成功识图，`image_desc_cache.json` 至今不存在
        （"惰性 flush"那一半从未实现，跨重启复用完全没生效）。

        现在按**计数 + 时间双阈值**流式落盘（先到者触发）：
          - 新增/命中累计 `_flush_every`（默认 5）条 → 立即落盘
          - 距上次落盘超过 `_flush_interval`（默认 30s）→ 立即落盘
        I/O 在锁**外**执行（`_flush_persist` 内部自带锁），避免阻塞其它协程。
        最坏情况（崩溃）只丢最后几条描述。
        """
        now = time.time()
        entry = self._persist.get(h)
        if entry:
            entry["last_ts"] = now
            entry["hits"] = entry.get("hits", 0) + 1
        else:
            self._persist[h] = {"desc": desc, "last_ts": now, "hits": 1}
        self._persist_dirty = True
        self._dirty_since_flush += 1
        # LRU 淘汰：超上限时移除 last_ts 最旧（最久未使用）的条目
        while len(self._persist) > self._persist_max and self._persist:
            oldest_k = min(self._persist, key=lambda k: self._persist[k]["last_ts"])
            del self._persist[oldest_k]
            self._evicted += 1
        # 双阈值判断（只读，快速）→ 需要落盘时执行
        due = (
            self._dirty_since_flush >= self._flush_every
            or (now - self._last_flush_ts) >= self._flush_interval
        )
        if due:
            # ⚠️ 必须调**无锁**版本：本方法可能在 `with self._lock` 内被调用
            # （describe_bytes 的新增分支就是），再取同一把非重入锁会死锁
            # —— 2026-09-13 实测把整个测试套件挂死（600s 超时）。
            self._flush_persist_locked()

    def _flush_persist_locked(self) -> None:
        """落盘（原子写 tmp→rename）。**要求调用方已持有 `self._lock`。**

        状态快照与写盘都在锁内完成：文件只有几百条 × 每条几十字节，
        写盘微秒级，为它拆"锁外写 + 回写状态"会引入竞态（两个协程各写一半
        状态），得不偿失。
        """
        if not self._persist_path or not self._persist_dirty:
            return
        import os
        try:
            tmp = self._persist_path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(self._persist, ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._persist_path)
        except OSError:
            return  # 落盘失败不 crash，下次再试
        self._persist_dirty = False
        self._flushed_entries += self._dirty_since_flush
        self._dirty_since_flush = 0
        self._last_flush_ts = time.time()
        self._flush_count += 1

    def _flush_persist(self) -> None:
        """取锁后落盘（terminate / 手动 flush 路径）。"""
        with self._lock:
            self._flush_persist_locked()

    def flush(self) -> None:
        """显式落盘（terminate / 手动）。"""
        self._flush_persist()

    def snapshot(self) -> dict:
        """观测：缓存大小/上限/淘汰数/命中分布（判断 persist_max 是否够）。"""
        with self._lock:
            return {
                "persist_enabled": bool(self._persist_path),
                "persist_size": len(self._persist),
                "persist_max": self._persist_max,
                "persist_evicted": self._evicted,
                "persist_flush_count": self._flush_count,
                "persist_flushed_entries": self._flushed_entries,
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

    async def describe_bytes(self, data: bytes, diag: Optional[dict] = None) -> Optional[str]:
        # S0 观测：与 emotion/gate 同一套 —— 内部失败必须可分辨。
        # 此前 4 条静默 return None（空数据/空描述/异常/超时）在调用方看来
        # 都是"无法识别"，2026-09-13 实测 5/14 次失败却查不出哪一层。
        self.last_error: Optional[str] = None
        if not data:
            self.last_error = "empty bytes"
            if diag is not None:
                diag["vision_fail"] = "empty bytes"
            return None
        h = hashlib.sha256(data).hexdigest()
        if diag is not None:
            diag["hash"] = h[:16]
            diag["bytes"] = len(data)
        with self._lock:
            hit = self._cache.get(h)
            if hit and time.time() - hit[0] < self._cache_ttl:
                if diag is not None:
                    diag["cache"] = "memory"
                self.stats["cache_hit"] += 1
                return hit[1]
            # A7③ 第二道：持久缓存命中 → 免调视觉模型（跨重启复用）
            if self._persist_path:
                pent = self._persist.get(h)
                if pent:
                    self._touch_persist(h, str(pent.get("desc", "")))
                    self._cache[h] = (time.time(), str(pent["desc"]))
                    if diag is not None:
                        diag["cache"] = "persist"
                    self.stats["cache_hit"] += 1
                    return str(pent["desc"])
        try:
            # 🔴 S6：**先降采样再发送**。此前是原图直送 —— 实测同一张图
            # prompt_tokens 完全相同（412 vs 412，网关侧图像 token 数固定），
            # 而时延从 4.0s 涨到 29.6s（最坏 127.5s），纯属白烧上传时间。
            # 且 >15s 的离群点**全部**是大 GIF，正是它们撞破了 15s 超时。
            data, mime = prepare_bytes_for_vision(data)
            if diag is not None:
                diag["mime"] = mime
                diag["prepared_bytes"] = len(data)
            b64 = base64.b64encode(data).decode()
            payload = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": VISION_SYSTEM_PROMPT},
                    {"role": "user", "content": [
                        {"type": "text", "text": "描述这张图片。"},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ]},
                ],
                # S6 标定：2048 而非 512 —— 512 下 reasoning 直接顶格（p50=512）；
                # 2048 下配对实测**无代价**（同 120 张：时延中位 −0.02s、
                # completion_tokens 反而 216 vs 247、reasoning 不发散 p50=131）。
                "max_tokens": 2048,
                # 0.0：JSON 抽取任务要确定性；实测 0.0/0.3/0.7 在 JSON 下均 100%，
                # 取 0.0 是为了可复现。
                "temperature": 0.0,
                # ⚠️ **必须显式发送**。网关无 off/none 档，只能发 low 或完全不发，
                # 而实测"不发"更糟（p95 10.0s / max 26.1s vs low 的 5.9s / 5.9s）。
                "reasoning_effort": self._reasoning_effort,
            }
            out = await asyncio.wait_for(self._post(payload), timeout=self._timeout)
            desc, tags = parse_vision_json(out)
            if diag is not None and tags:
                diag["tags"] = tags
            desc = desc[: self._desc_max_chars]
            if not desc:
                # 模型返回空（多为思考吃光 max_tokens）
                self.last_error = "empty completion (model returned no content)"
                self.stats["empty"] += 1
                if diag is not None:
                    diag["vision_fail"] = self.last_error
                return None
            with self._lock:
                self._cache[h] = (time.time(), desc)
                # A7③: 新描述写持久层（标脏，惰性 flush）
                if self._persist_path:
                    self._touch_persist(h, desc)
            self.stats["ok"] += 1
            if diag is not None:
                diag["cache"] = "miss"
            return desc
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            self.stats["timeout" if isinstance(e, asyncio.TimeoutError) else "error"] += 1
            if diag is not None:
                diag["vision_fail"] = self.last_error
            return None

    async def describe_image(self, img, diag: Optional[dict] = None) -> Optional[str]:
        if diag is None:
            diag = {}
        data = await resolve_image_bytes(img, diag=diag)
        if not data:
            # 取字节这一级就失败了 —— 与"模型没描述"必须分开记
            self.last_error = str(diag.get("resolve_fail") or "resolve failed")
            if diag.get("resolve_error"):
                self.last_error += f" ({diag['resolve_error']})"
            self.stats["error"] += 1
            return None
        return await self.describe_bytes(data, diag=diag)