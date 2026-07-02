"""KGProvider — abstract knowledge-graph injection interface.

Each LLM call appends a short ``system`` message to the session containing
situational guidance retrieved from an external knowledge source. The plugin
only depends on the abstract interface; the concrete implementation can be
swapped without touching the caller.

v1: ``RagKGProvider`` wraps the existing chromadb-backed ``RagService``.
Future: issue #2 replaces this with a true knowledge-graph provider.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from .rag_service import RagService


@dataclass
class KGContext:
    recent_messages: list[dict]
    current_speaker: str
    current_text: str
    group_id: str


@dataclass
class KGResult:
    content: str
    metadata: dict = field(default_factory=dict)


class KGProvider(ABC):
    @abstractmethod
    async def query(self, ctx: KGContext) -> Optional[KGResult]:
        ...


class RagKGProvider(KGProvider):
    def __init__(
        self,
        rag: RagService,
        k_retrieve: int = 8,
        top_n_final: int = 3,
        max_chars: int = 400,
    ) -> None:
        self._rag = rag
        self._k_retrieve = k_retrieve
        self._top_n_final = top_n_final
        self._max_chars = max_chars

    async def query(self, ctx: KGContext) -> Optional[KGResult]:
        query_text = self._build_query(ctx)
        if not query_text:
            return None

        hits = self._rag.query(
            context_text=query_text,
            k=self._k_retrieve,
            now_utc=time.time(),
            top_n_final=self._top_n_final,
        )
        if not hits:
            return None

        content = self._format(hits)
        if not content:
            return None

        return KGResult(
            content=content,
            metadata={
                "provider": "rag_v1",
                "top_score": hits[0].get("score", 0),
                "num_hits": len(hits),
            },
        )

    def _build_query(self, ctx: KGContext) -> str:
        lines = []
        for m in ctx.recent_messages:
            role = m.get("role", "")
            name = m.get("name", "")
            text = (m.get("content", "") or "").strip()
            if not text:
                continue
            if role == "system":
                continue
            if role == "assistant":
                lines.append(f"我: {text}")
            elif name:
                lines.append(f"{name}: {text}")
            else:
                lines.append(f"群友: {text}")
        if not lines:
            return ""
        return "\n".join(lines)

    def _format(self, hits: list[dict]) -> str:
        lines = [
            "[记忆] 以下是历史相似对话中你的回复方式（仅供风格参考，不要复读）："
        ]
        total = 0
        for i, h in enumerate(hits, 1):
            meta = h.get("metadata") or {}
            reply_text = (meta.get("reply_text") or "").strip()
            if not reply_text:
                continue
            block = (
                f"示例{i} (相似度={h.get('score', 0):.2f}):\n"
                f"  你当时的回复: {reply_text}"
            )
            total += len(block)
            if total > self._max_chars:
                break
            lines.append(block)
        return "\n".join(lines) if len(lines) > 1 else ""

    @staticmethod
    def disabled() -> KGResult:
        return KGResult(content="", metadata={"provider": "disabled"})
