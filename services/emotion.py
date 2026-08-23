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
    ) -> None:
        import asyncio
        import threading as _threading
        self._llm_fn = llm_fn
        self._timeout = float(timeout)
        self._cache_ttl = float(cache_ttl)
        self._recent_n = int(recent_n)
        self._cache: dict[str, tuple[float, EmotionState]] = {}
        self._lock = _threading.Lock()

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
    def _parse(text: str) -> EmotionState:
        try:
            import json as _json
            obj = _json.loads(text)
            w = float(obj.get("willingness", 1.0))
            w = max(0.3, min(1.5, w))
            mood = str(obj.get("mood", "")).strip()
            sticker = str(obj.get("sticker", "")).strip()
            return EmotionState(
                global_willingness=w,
                current_mood=mood,
                sticker_prompt=sticker,
            )
        except Exception:
            return EmotionState.neutral()

    async def query(
        self,
        group_id: str,
        recent_msgs: list[dict],
        kg_ctx: Optional["KGContext"] = None,
    ) -> EmotionState:
        import asyncio as _asyncio
        now = time.time()
        with self._lock:
            hit = self._cache.get(group_id)
            if hit and now - hit[0] < self._cache_ttl:
                return hit[1]
        speaker = kg_ctx.current_speaker if kg_ctx else ""
        text = kg_ctx.current_text if kg_ctx else ""
        try:
            prompt = self._build_prompt(recent_msgs, speaker, text)
            raw = await _asyncio.wait_for(self._llm_fn(prompt), timeout=self._timeout)
            st = self._parse(raw)
        except Exception:
            st = EmotionState.neutral()
        with self._lock:
            self._cache[group_id] = (time.time(), st)
        return st
