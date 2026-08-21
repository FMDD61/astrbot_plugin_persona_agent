"""RagService — retrieve historical conversation pairs that resemble the
current live group context, and provide an in-prompt example block.

Backing store: ChromaDB persistent collection built by tools/rebuild_chroma.py.

Re-rank formula (plan §6, initial stack — no BM25 / reranker yet):

    final_score = 0.70 * dense_similarity
                + 0.20 * recency
                + 0.10 * hour_match

  - dense_similarity = 1 - cosine_distance  (Chroma reports distance; cosine in [0,2])
  - recency = exp(-age_days / 180)          (180-day half-life, soft)
  - hour_match = 1 if |reply_hour_utc - now_hour| <= 1 else 0

Public API (called by interjection / reply pipeline):

    rag = RagService(data_dir, model_name=..., backend=None)
    hits = rag.query(context_text, k=8, now_utc=None, top_n_final=3)
    block = rag.format_examples(hits)   # str, ready to splice into the prompt

Lazy-loading: chromadb and sentence-transformers are imported only when
the first real query is made. On hosts without these deps (e.g. this Windows
workspace), inject an `EmbeddingBackend` mock at construction time.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol


DEFAULT_MODEL = "BAAI/bge-base-zh-v1.5"
DEFAULT_COLLECTION = "persona_pairs"
DEFAULT_DB_SUBDIR = "chromadb"


class EmbeddingBackend(Protocol):
    """Anything that can embed a list of strings into list[list[float]]."""
    dim: int
    def encode(self, texts: list[str]) -> list[list[float]]: ...


class ChromaBackend(Protocol):
    """Subset of Chroma collection we use."""
    def query(self, query_embeddings: list[list[float]], n_results: int) -> dict: ...
    def count(self) -> int: ...


class RagService:
    def __init__(
        self,
        data_dir: str | os.PathLike,
        model_name: str = DEFAULT_MODEL,
        collection_name: str = DEFAULT_COLLECTION,
        backend: Optional[EmbeddingBackend] = None,
        collection: Optional[ChromaBackend] = None,
        recency_half_life_days: float = 180.0,
        weights: tuple[float, float, float] = (0.70, 0.20, 0.10),
    ) -> None:
        self._dir = Path(data_dir)
        self._model_name = model_name
        self._collection_name = collection_name
        self._injected_backend = backend
        self._injected_collection = collection
        self._backend: Optional[EmbeddingBackend] = backend
        self._collection: Optional[ChromaBackend] = collection
        self._client = None
        self._lock = threading.Lock()
        self._recency_hl = float(recency_half_life_days)
        self._w_dense, self._w_recency, self._w_hour = weights

    # ---- lazy loaders (real deps) ----
    def _ensure_backend(self) -> EmbeddingBackend:
        if self._backend is not None:
            return self._backend
        with self._lock:
            if self._backend is not None:
                return self._backend
            from sentence_transformers import SentenceTransformer  # type: ignore
            # 只从本地 HF 缓存加载，禁止 SentenceTransformer() 联网校验元数据。
            # 否则在 huggingface.co 不可达时地址 http_backoff 无限重试，同步阻塞 AstrBot 事件循环（表现为“主动回复卡死”）。
            model = SentenceTransformer(self._model_name, local_files_only=True)
            dim = int(model.get_sentence_embedding_dimension())

            class _STBackend:
                def __init__(self, m, d):
                    self._m = m
                    self.dim = d
                def encode(self, texts):
                    return self._m.encode(texts, normalize_embeddings=True).tolist()
            self._backend = _STBackend(model, dim)
            return self._backend

    def _ensure_collection(self) -> ChromaBackend:
        if self._collection is not None:
            return self._collection
        with self._lock:
            if self._collection is not None:
                return self._collection
            import chromadb  # type: ignore
            db_dir = self._dir / DEFAULT_DB_SUBDIR
            self._client = chromadb.PersistentClient(path=str(db_dir))
            self._collection = self._client.get_collection(self._collection_name)
            return self._collection

    # ---- ranking ----
    @staticmethod
    def _dense_from_distance(distance: float) -> float:
        # ChromaDB cosine distance is in [0, 2]; similarity = 1 - distance/2 -> [0, 1].
        # But our embeddings are L2-normalised, so distance ~ [0, 2] and sim = 1 - d.
        # We clamp.
        sim = 1.0 - float(distance)
        return max(0.0, min(1.0, sim))

    def _recency(self, reply_ts_iso: str, now_utc: float) -> float:
        try:
            ts = datetime.fromisoformat(reply_ts_iso.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError, AttributeError):
            return 0.0
        age_days = max(0.0, (now_utc - ts) / 86400.0)
        return math.exp(-age_days / self._recency_hl)

    @staticmethod
    def _hour_match(reply_hour: int, now_hour: int) -> float:
        if reply_hour < 0 or now_hour < 0:
            return 0.0
        diff = abs(reply_hour - now_hour)
        # wrap-around 0/23 also close
        diff = min(diff, 24 - diff)
        return 1.0 if diff <= 1 else 0.0

    # ---- public API ----
    def query(
        self,
        context_text: str,
        k: int = 8,
        now_utc: Optional[float] = None,
        top_n_final: int = 3,
    ) -> list[dict]:
        """Return a ranked list of hits (length <= top_n_final).

        Each hit:
          {"id", "document", "metadata", "dense", "recency", "hour_match", "score"}
        """
        if not context_text or not context_text.strip():
            return []

        backend = self._ensure_backend()
        coll = self._ensure_collection()
        qvec = backend.encode([context_text])

        raw = coll.query(query_embeddings=qvec, n_results=k)
        # Chroma 0.5 response shape: keys are lists of lists (one per query).
        ids = (raw.get("ids") or [[]])[0]
        docs = (raw.get("documents") or [[]])[0]
        metas = (raw.get("metadatas") or [[]])[0]
        dists = (raw.get("distances") or [[]])[0]

        now_utc = now_utc if now_utc is not None else time.time()
        now_hour = datetime.fromtimestamp(now_utc, tz=timezone.utc).hour

        hits = []
        for i in range(len(ids)):
            meta = metas[i] or {}
            dense = self._dense_from_distance(dists[i] if i < len(dists) else 1.0)
            recency = self._recency(meta.get("reply_ts", ""), now_utc)
            hour_match = self._hour_match(int(meta.get("reply_hour_utc", -1)), now_hour)
            score = (self._w_dense * dense
                     + self._w_recency * recency
                     + self._w_hour * hour_match)
            hits.append({
                "id": ids[i],
                "document": docs[i] if i < len(docs) else "",
                "metadata": meta,
                "dense": round(dense, 4),
                "recency": round(recency, 4),
                "hour_match": round(hour_match, 4),
                "score": round(score, 4),
            })

        hits.sort(key=lambda h: -h["score"])
        return hits[:top_n_final]

    def format_examples(self, hits: list[dict], max_chars: int = 800) -> str:
        """Render hits as a compact in-prompt example block.

        Format:
            # 历史相似回复参考
            示例1:
              上下文: <last 3 lines>
              我的回复: <reply_text>
            示例2: ...
        """
        if not hits:
            return ""
        lines = ["# 历史相似回复参考（仅供风格借鉴，不要直接复述）"]
        total = 0
        for i, h in enumerate(hits, 1):
            meta = h.get("metadata") or {}
            ctx = (h.get("document") or "").splitlines()
            tail = ctx[-3:] if len(ctx) > 3 else ctx
            block = (f"示例{i} (score={h['score']:.2f}):\n"
                     f"  上下文片段:\n    " + "\n    ".join(tail) + "\n"
                     f"  我的回复: {meta.get('reply_text','')}")
            total += len(block)
            if total > max_chars:
                break
            lines.append(block)
        return "\n".join(lines)

    # ---- placeholders for future extension ----
    def query_with_bm25(self, *args, **kwargs):
        """Reserved: dense + BM25 hybrid. Not implemented in v1."""
        return self.query(*args, **kwargs)

    def query_with_reranker(self, *args, **kwargs):
        """Reserved: cross-encoder reranker. Not implemented in v1."""
        return self.query(*args, **kwargs)
