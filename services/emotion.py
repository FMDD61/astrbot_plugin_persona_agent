"""EmotionProvider — abstract interface for mood/sticker injection.

Hooks into two points in the reply pipeline:
  1. interjection.decide() — global_willingness modulates trigger probability.
  2. _generate_reply()   — current_mood injected into system_prompt;
                            sticker_prompt triggers an image send via Comp.Image.

v1: DefaultEmotionProvider returns neutral (no emotional influence).
v2: KGContext accepted for structured context (forward-compat with Phase 3 DreamJob).
"""
from __future__ import annotations

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
