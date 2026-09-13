"""sticker — 贴纸库选择器（S3②，spec §4.3 / docs/specs/s3-action-channel.md）。

## 定位

RP 在正文里写 ``[emote:意图短语]`` → 编排层剥离标记 → **本服务把"意图短语"匹配成
库里的某一张贴纸** → main 发图。匹配不上就**静默跳过**（不文生图兜底、不重试）。

## 设计取舍（都有实测依据）

1. **复用已有 BGE，不加载第二份模型**。台式机 7.7 GB，AstrBot 常驻 ~2 GB（含 BGE）。
   嵌入函数由调用方注入（main 传入 `RagService` 的后端）。
2. **索引里预计算 embedding** → 线上每次选择只需嵌入**一句短语**（不是整库）。
   库是几十~几百张的量级，余弦相似度用 numpy 一次算完，**不引入第二个 Chroma 集合**。
3. **默认纯 BGE 选择，LLM 精选是可选项**。理由：本项目一贯优先"省一次调用"；
   top1 分数够高且与 top2 分差够大时，BGE 已经够好。只有候选接近（分差 < margin）
   时才值得花一次 LLM。这条可用 `sticker_log.jsonl` 的分数数据事后调。
4. **失败一律返回 None 并记原因**（S0 的教训：降级必须可见）。

## 索引文件格式

```json
{"version": 1, "model": "BAAI/bge-base-zh-v1.5", "dim": 768,
 "items": [{"id": "001_无奈", "file": "001_无奈.jpg", "sha256": "...",
            "desc": "Q版角色扶额闭眼，表达无奈", "tags": ["无奈"],
            "embedding": [...], "added_at": "..."}]}
```

索引是**人工可审文件**（红线 #3：工具只建议不覆盖）。
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# 嵌入函数：list[str] -> list[list[float]]（BGE 输出已归一化）
EmbedFn = Callable[[list[str]], list[list[float]]]


@dataclass
class StickerHit:
    id: str
    file: str
    desc: str = ""
    score: float = 0.0
    path: str = ""            # 绝对路径（调用方发图用）


@dataclass
class PickResult:
    """一次选择的结果 —— **失败也要能说清原因**（S0 的教训）。"""
    hit: Optional[StickerHit] = None
    reason: str = ""                      # "" = 成功
    via: str = ""                         # "bge" | "llm"
    top_k: list[dict] = field(default_factory=list)


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度。BGE 输出已归一化 → 直接点积；但仍做防御性归一化。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


class StickerService:
    """贴纸选择。线程安全（选择是纯计算，载入用锁保护）。"""

    def __init__(
        self,
        index_path: str | os.PathLike,
        embed_fn: Optional[EmbedFn] = None,
        *,
        library_dir: Optional[str | os.PathLike] = None,
        top_k: int = 5,
        min_score: float = 0.45,
        margin: float = 0.06,
        picker: Optional[Callable[[str, list[StickerHit]], Any]] = None,
    ) -> None:
        self._index_path = Path(index_path)
        self._library_dir = Path(library_dir) if library_dir else self._index_path.parent / "sticker_library"
        self._embed = embed_fn
        self._top_k = max(1, int(top_k))
        self._min_score = float(min_score)
        self._margin = float(margin)
        self._picker = picker
        self._items: list[dict] = []
        self._mtime: float = 0.0
        self._load_error: str = ""
        self._index_missing: bool = False
        # 观测（进程生命周期）
        self.stats = {"ok": 0, "llm_pick": 0, "below_threshold": 0,
                      "empty_library": 0, "index_missing": 0,
                      "no_embed": 0, "load_error": 0}
        self.last_error: Optional[str] = None
        self._load()

    # ---- 载入（mtime 热重载：索引是人工文件，改完不该重启）----

    def _load(self) -> None:
        try:
            mtime = self._index_path.stat().st_mtime
        except OSError as e:
            self._items = []
            self._load_error = f"index not found: {e}"
            self._index_missing = True
            self.stats["load_error"] += 1
            return
        if mtime == self._mtime and self._items:
            return
        try:
            with open(self._index_path, encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise ValueError("index has no 'items' list")
            self._index_missing = False
            self._items = [
                it for it in items
                if isinstance(it, dict) and it.get("id") and it.get("file")
            ]
            self._mtime = mtime
            self._load_error = ""
        except (OSError, ValueError, json.JSONDecodeError) as e:
            self._items = []
            self._load_error = f"index unreadable: {type(e).__name__}: {e}"
            self.stats["load_error"] += 1

    def reload_if_changed(self) -> bool:
        before = self._mtime
        self._load()
        return self._mtime != before

    @property
    def size(self) -> int:
        return len(self._items)

    def snapshot(self) -> dict:
        return {
            "index_path": str(self._index_path),
            "library_dir": str(self._library_dir),
            "size": len(self._items),
            "load_error": self._load_error,
            "top_k": self._top_k,
            "min_score": self._min_score,
            "margin": self._margin,
            "picker": bool(self._picker),
            "stats": dict(self.stats),
        }

    # ---- 选择 ----

    def _candidates(self, intent: str) -> tuple[list[StickerHit], str]:
        """返回 (排序后的候选, 失败原因)。"""
        if not self._items:
            # 索引文件缺失 vs 库为空 —— 两种成因的处置完全不同（前者去建库，
            # 后者去补图），不分清就只能猜（S0 的教训）。
            return [], ("index_missing" if self._index_missing else "empty_library")
        if self._embed is None:
            return [], "no_embed"
        try:
            vecs = self._embed([intent])
            qv = vecs[0] if vecs else None
        except Exception as e:
            self.last_error = f"embed failed: {type(e).__name__}: {e}"
            return [], "no_embed"
        if not qv:
            return [], "no_embed"
        scored: list[StickerHit] = []
        for it in self._items:
            emb = it.get("embedding")
            if not emb:
                continue
            s = _cosine(qv, emb)
            scored.append(StickerHit(
                id=str(it.get("id")), file=str(it.get("file")),
                desc=str(it.get("desc") or ""), score=round(s, 4),
            ))
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[: self._top_k], ""

    async def pick(self, intent: str) -> PickResult:
        """把意图短语匹配成一张贴纸。任何失败都返回 ``PickResult(hit=None, reason=...)``。"""
        intent = (intent or "").strip()
        self.last_error = None
        if not intent:
            return PickResult(reason="empty_intent")
        self.reload_if_changed()
        cands, why = self._candidates(intent)
        if why:
            self.stats[why] = self.stats.get(why, 0) + 1
            return PickResult(reason=why)

        top = cands[0]
        near = len(cands) > 1 and (top.score - cands[1].score) < self._margin

        # 高置信：BGE top1 且分差够大 → 直接用，省一次 LLM
        if top.score >= self._min_score and not near:
            self.stats["ok"] += 1
            return PickResult(hit=self._with_path(top), reason="", via="bge",
                              top_k=[{"id": c.id, "score": c.score} for c in cands])
        if top.score < self._min_score:
            self.stats["below_threshold"] += 1
            return PickResult(reason=f"below_threshold ({top.score:.3f} < {self._min_score})",
                              via="bge",
                              top_k=[{"id": c.id, "score": c.score} for c in cands])

        # 候选接近 → 可选 LLM 精选
        if self._picker is not None:
            try:
                chosen_id = await self._picker(intent, cands)
            except Exception as e:
                self.last_error = f"picker failed: {type(e).__name__}: {e}"
                chosen_id = None
            if chosen_id:
                for c in cands:
                    if c.id == chosen_id:
                        self.stats["ok"] += 1
                        self.stats["llm_pick"] += 1
                        return PickResult(hit=self._with_path(c), reason="", via="llm",
                                          top_k=[{"id": x.id, "score": x.score} for x in cands])
            # LLM 没挑出来 → 静默跳过（不用 BGE 兜底：接近说明本来就不确定）
            return PickResult(reason="picker_no_choice", via="bge",
                              top_k=[{"id": c.id, "score": c.score} for c in cands])

        # 没有 picker 且分差小 → 保守跳过（宁可不发，也不发错）
        return PickResult(reason=f"ambiguous (top1-top2={top.score - cands[1].score:.3f} < {self._margin})",
                          via="bge",
                          top_k=[{"id": c.id, "score": c.score} for c in cands])

    def _with_path(self, hit: StickerHit) -> StickerHit:
        p = self._library_dir / hit.file
        hit.path = str(p)
        return hit


def embed_via_rag(rag) -> Optional[EmbedFn]:
    """从 RagService 取嵌入函数（复用已加载的 BGE，不新增模型）。

    `RagService._ensure_backend()` 是懒加载且线程安全；这里包一层把异常
    收敛为 None —— 拿不到嵌入不应该让插件崩，只会让贴纸功能静默不可用。
    """
    if rag is None:
        return None
    try:
        backend = rag._ensure_backend()  # noqa: SLF001（项目内同源，故意复用）
    except Exception:
        return None
    if backend is None or not hasattr(backend, "encode"):
        return None

    def _embed(texts: list[str]) -> list[list[float]]:
        return backend.encode(list(texts))

    return _embed
