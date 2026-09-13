"""EmotionProvider — abstract interface for mood/sticker injection.

Hooks into two points in the reply pipeline:
  1. interjection.decide() — global_willingness modulates trigger probability.
  2. _generate_reply()   — current_mood injected into system_prompt;
                            sticker_prompt triggers an image send via Comp.Image.

v1: DefaultEmotionProvider returns neutral (no emotional influence).
v2: KGContext accepted for structured context (forward-compat with Phase 3 DreamJob).
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .kg_provider import KGContext


@dataclass
class EmotionState:
    global_willingness: float = 1.0
    current_mood: str = ""
    sticker_prompt: str = ""

    @staticmethod
    def neutral() -> "EmotionState":
        return EmotionState()


class EmotionProvider(ABC):
    @abstractmethod
    async def query(
        self,
        group_id: str,
        recent_msgs: list[dict],
        kg_ctx: Optional["KGContext"] = None,
    ) -> EmotionState:
        ...


class DefaultEmotionProvider(EmotionProvider):
    async def query(
        self,
        group_id: str,
        recent_msgs: list[dict],
        kg_ctx: Optional["KGContext"] = None,
    ) -> EmotionState:
        return EmotionState.neutral()


# ---------------------------------------------------------------------------
# v1: LLM-backed emotion provider (G10)
# 3 dimensions: willingness (decision multiplier), current_mood (injected into
# system prompt tail), sticker_prompt (sent after the reply). 30s per-group
# cache + 3s timeout, neutral fallback on any failure.
# ---------------------------------------------------------------------------

EMOTION_SYSTEM_PROMPT = (
    "你是一个 QQ 群聊情绪分析器。根据最近对话判断当前氛围，"
    "只输出一个 JSON 对象："
    '{"willingness": 0.0到1.0的浮点数, "mood": "三到十五字的中文情绪短语", '
    '"sticker": "一句话表情包描述，不需要则空字符串"}'
)


class LLMEmotionProvider(EmotionProvider):
    def __init__(
        self,
        llm_fn,
        *,
        timeout: float = 3.0,
        cache_ttl: float = 30.0,
        recent_n: int = 12,
        now_utc_fn=None,
    ) -> None:
        import asyncio
        import threading as _threading
        self._llm_fn = llm_fn
        self._timeout = float(timeout)
        self._cache_ttl = float(cache_ttl)
        self._recent_n = int(recent_n)
        self._cache: dict[str, tuple[float, EmotionState]] = {}
        self._lock = _threading.Lock()
        # A7④: 时钟注入（离线重放按场景时刻推进情绪缓存；缺省真实时钟）
        self._now = now_utc_fn or time.time
        # S0 观测：最近一次查询的降级原因（None = 正常）。
        # 静默回退中性值曾把「每次都超时」伪装成「模型稳定输出中性」，
        # 隐藏了十几天（2026-09-13 实测 2262 条 trace 全为 1.0/""）。
        # pipeline 每轮读走后由下一次 query 覆盖，无跨消息串味。
        self.last_error: Optional[str] = None
        # 观测计数（进程生命周期内）
        self.stats = {"ok": 0, "cached": 0, "timeout": 0, "error": 0, "parse_fail": 0}

    def _build_prompt(self, recent_msgs: list[dict], speaker: str, text: str) -> str:
        lines = []
        for m in recent_msgs[-self._recent_n:]:
            role = m.get("role", "")
            name = m.get("name", "") or ("夕化炭" if role == "assistant" else "群友")
            content = (m.get("content", "") or "").strip()
            if not content:
                continue
            lines.append(f"{name}: {content}")
        prompt = "最近对话：\n" + "\n".join(lines) if lines else "最近对话：空"
        if speaker:
            prompt += f"\n当前说话人：{speaker}，本条消息：{text}"
        return prompt

    @staticmethod
    def _extract_json(text: str) -> str:
        """容错：模型偶尔把 JSON 包在 ```json 围栏或前后带解释文字里。"""
        t = (text or "").strip()
        if t.startswith("```"):
            t = t.split("\n", 1)[-1]
            if t.rstrip().endswith("```"):
                t = t.rstrip()[:-3]
            t = t.strip()
        if t.startswith("{") and t.endswith("}"):
            return t
        i, j = t.find("{"), t.rfind("}")
        if 0 <= i < j:
            return t[i : j + 1]
        return t

    @classmethod
    def _parse(cls, text: str) -> EmotionState:
        """解析情绪 JSON。**解析失败即抛异常**（由 query 记为 parse_fail）——
        旧实现用 except → neutral 把「格式不合」与「真的中性」混为一谈。"""
        import json as _json
        obj = _json.loads(cls._extract_json(text))
        w = float(obj.get("willingness", 1.0))
        w = max(0.3, min(1.5, w))
        return EmotionState(
            global_willingness=w,
            current_mood=str(obj.get("mood", "")).strip(),
            sticker_prompt=str(obj.get("sticker", "")).strip(),
        )

    async def query(
        self,
        group_id: str,
        recent_msgs: list[dict],
        kg_ctx: Optional["KGContext"] = None,
    ) -> EmotionState:
        import asyncio as _asyncio
        now = self._now()
        with self._lock:
            hit = self._cache.get(group_id)
            if hit and now - hit[0] < self._cache_ttl:
                self.last_error = None
                self.stats["cached"] += 1
                return hit[1]
        self.last_error = None
        speaker = kg_ctx.current_speaker if kg_ctx else ""
        text = kg_ctx.current_text if kg_ctx else ""
        st: EmotionState
        try:
            prompt = self._build_prompt(recent_msgs, speaker, text)
            raw = await _asyncio.wait_for(self._llm_fn(prompt), timeout=self._timeout)
            st = self._parse(raw)
            self.stats["ok"] += 1
        except _asyncio.TimeoutError:
            # ★ 主因（2026-09-13 实测）：模型思考 4–8s，而默认超时 3s 每次都超
            st = EmotionState.neutral()
            self.last_error = f"timeout after {self._timeout:g}s"
            self.stats["timeout"] += 1
        except Exception as e:
            st = EmotionState.neutral()
            self.last_error = f"{type(e).__name__}: {e}"
            if isinstance(e, ValueError):
                self.stats["parse_fail"] += 1
            else:
                self.stats["error"] += 1
        with self._lock:
            self._cache[group_id] = (self._now(), st)
        return st
