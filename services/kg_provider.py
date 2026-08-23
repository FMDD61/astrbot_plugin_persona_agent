"""KGProvider — abstract knowledge-graph injection interface.

① 检索层: 每次 LLM 调用时注入结构化风格指引到 contexts 末尾.

v2: ``MultiSignalKGProvider`` — dense(BGE) + BM25(FTS5) + entity(alias overlap)
   多信号融合检索, 替代 v1 RagKGProvider.
"""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from .rag_service import RagService
from .memory_store import MemoryStore


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


class MultiSignalKGProvider(KGProvider):
    """多信号融合: dense(BGE向量) + BM25(关键词) + entity(实体匹配) → 加权排序."""

    def __init__(
        self,
        rag: RagService,
        store: MemoryStore,
        k_retrieve: int = 8,
        top_n_final: int = 3,
        max_chars: int = 400,
        w_dense: float = 0.60,
        w_bm25: float = 0.20,
        w_entity: float = 0.20,
    ) -> None:
        self._rag = rag
        self._store = store
        self._k_retrieve = k_retrieve
        self._top_n_final = top_n_final
        self._max_chars = max_chars
        self._w_dense = w_dense
        self._w_bm25 = w_bm25
        self._w_entity = w_entity

    async def query(self, ctx: KGContext) -> Optional[KGResult]:
        query_text = self._build_query(ctx)
        if not query_text:
            return None

        # 1. dense (BGE vector) — CPU-bound, keep off the event loop (G1)
        dense_hits = await asyncio.to_thread(
            self._rag.query,
            context_text=query_text,
            k=self._k_retrieve,
            now_utc=time.time(),
            top_n_final=self._k_retrieve,
        )
        dense_map: dict[str, float] = {}
        for h in dense_hits:
            dense_map[h.get("id", "")] = h.get("score", 0.0)

        # 2. BM25 (FTS5 keyword)
        bm25_hits = self._store.search_bm25(ctx.current_text, limit=self._k_retrieve)
        max_bm25_rank = max((abs(h["rank"]) for h in bm25_hits), default=1)
        bm25_map: dict[str, float] = {}
        for h in bm25_hits:
            doc_key = (h["alias"] + ":" + h["text"])[:64]
            bm25_map[doc_key] = min(abs(h["rank"]) / max_bm25_rank, 1.0)

        # 3. entity overlap
        entities = self._store.get_entities(ctx.current_text)
        entity_aliases = {e.alias for e in entities}
        entity_map: dict[str, float] = {}
        for h in dense_hits:
            doc = h.get("document", "")
            overlap = sum(1 for a in entity_aliases if a in doc)
            entity_map[h.get("id", "")] = min(overlap / max(len(entity_aliases), 1), 1.0)

        # 4. fused rank
        fused: list[dict] = []
        for h in dense_hits:
            doc_id = h.get("id", "")
            doc_key = (h.get("document", "")[:64])
            score = (
                self._w_dense * dense_map.get(doc_id, 0.0)
                + self._w_bm25 * bm25_map.get(doc_key, 0.0)
                + self._w_entity * entity_map.get(doc_id, 0.0)
            )
            fused.append({**h, "fused_score": round(score, 4)})

        fused.sort(key=lambda x: -x["fused_score"])
        top = fused[:self._top_n_final]

        if not top:
            return None

        # 5. graph augmentation: 查询当前说话人与 bot 的互动模式
        relation_block = self._format_relation(ctx)

        content = self._format(top, relation_block)
        if not content:
            return None

        return KGResult(
            content=content,
            metadata={
                "provider": "multisignal_v2",
                "top_score": top[0].get("fused_score", 0),
                "num_hits": len(top),
                "dense_hits": len(dense_hits),
                "bm25_hits": len(bm25_hits),
                "entity_count": len(entities),
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
        return "\n".join(lines)

    def _format_relation(self, ctx: KGContext) -> str:
        if ctx.current_speaker == "<bot>":
            return ""
        rel = self._store.get_relation(ctx.current_speaker, "<bot>")
        if rel is None:
            return ""
        parts = [
            f"你和{ctx.current_speaker}互动{rel.interaction_count}次, "
            f"关系等级: {rel.closeness}."
        ]
        if rel.common_topics:
            parts.append(f"共同话题: {'、'.join(rel.common_topics[:3])}.")
        return "\n".join(parts)

    def _format(self, hits: list[dict], relation_block: str = "") -> str:
        header = "[记忆] 以下是历史相似对话中你的回复方式（仅供风格参考，不要复读）："
        if relation_block:
            header += f"\n{relation_block}"
        lines = [header]
        total = 0
        for i, h in enumerate(hits, 1):
            meta = h.get("metadata") or {}
            reply_text = (meta.get("reply_text") or "").strip()
            if not reply_text:
                continue
            block = (
                f"示例{i} (得分={h.get('fused_score', h.get('score', 0)):.2f}):\n"
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
