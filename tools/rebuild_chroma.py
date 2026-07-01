"""§6 rebuild_chroma.py — embed §4 conversation pairs into a ChromaDB collection.

Reads:
  data_out/my_conversation_pairs.jsonl

Writes (when not --dry-run):
  data_out/chromadb/                    persistent ChromaDB store
  data_out/rebuild_chroma_summary.json  counts, model, embedding dim, elapsed

Each conversation pair becomes one document:
  id        = "pair_<index>"
  document  = context joined as "<name>: <text>\\n..." (the search side; we want
              to retrieve historical pairs whose context resembles the current
              live conversation)
  metadata  = {reply_ts, reply_text, reply_n_burst, trigger, reply_to_name,
               reply_hour_utc, context_n, context_span_seconds, last_ctx_ts}
  embedding = BGE-encoded document

The rag_service consumes this directly: query the collection with the current
group context, then for each hit synthesize an in-prompt example from
`metadata.reply_text`.

Run:
  python -m astrbot_plugin_persona_agent.tools.rebuild_chroma --dry-run
      # validates JSONL schema, builds documents, writes summary, NO embedding,
      # NO chromadb dependency required. Safe to run on Windows.

  python -m astrbot_plugin_persona_agent.tools.rebuild_chroma
      # full rebuild. Requires: pip install chromadb sentence-transformers
      # First run will download the BGE model (~400 MB for base, ~2 GB for m3).

Notes:
  - All heavy deps (chromadb, sentence_transformers) are imported lazily inside
    main() so that --dry-run and `python -m ...` -h work on machines without them.
  - Default model is BAAI/bge-base-zh-v1.5 (Chinese-optimised, 768-dim).
  - The collection is dropped and recreated on every run -> rebuilds are idempotent.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator


COLLECTION_NAME = "persona_pairs"
DEFAULT_MODEL = "BAAI/bge-base-zh-v1.5"
DEFAULT_BATCH = 32

REQUIRED_PAIR_FIELDS = ("reply_ts", "reply_text", "context", "trigger")
REQUIRED_CTX_FIELDS = ("ts", "name", "uin", "text")


def iter_pairs(path: Path) -> Iterator[dict]:
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def validate_pair(pair: dict, idx: int) -> list[str]:
    errs = []
    for k in REQUIRED_PAIR_FIELDS:
        if k not in pair:
            errs.append(f"pair[{idx}] missing field {k!r}")
    ctx = pair.get("context") or []
    if not ctx:
        errs.append(f"pair[{idx}] empty context")
    for j, m in enumerate(ctx[:5]):  # spot-check first 5
        for k in REQUIRED_CTX_FIELDS:
            if k not in m:
                errs.append(f"pair[{idx}].context[{j}] missing field {k!r}")
    return errs


def build_document(pair: dict) -> str:
    """Render context as the search-side document."""
    lines = []
    for m in pair["context"]:
        name = m.get("name") or m.get("uin") or "?"
        text = m.get("text") or ""
        lines.append(f"{name}: {text}")
    return "\n".join(lines)


def parse_ts(ts: str) -> float | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def reply_hour_utc(ts: str) -> int | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).hour
    except (ValueError, TypeError, AttributeError):
        return None


def build_metadata(pair: dict) -> dict:
    ctx = pair["context"]
    first_ts = parse_ts(ctx[0]["ts"]) if ctx else None
    last_ts = parse_ts(ctx[-1]["ts"]) if ctx else None
    span = int(last_ts - first_ts) if (first_ts and last_ts) else 0
    return {
        "reply_ts": pair["reply_ts"],
        "reply_text": pair["reply_text"],
        "reply_n_burst": int(pair.get("n_burst", 1)) if isinstance(pair.get("n_burst", 1), int) else 1,
        "trigger": pair.get("trigger", "window"),
        "reply_to_name": pair.get("reply_to_name") or "",
        "reply_hour_utc": reply_hour_utc(pair["reply_ts"]) if pair.get("reply_ts") else -1,
        "context_n": len(ctx),
        "context_span_seconds": span,
        "last_ctx_ts": ctx[-1]["ts"] if ctx else "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Embed §4 pairs into ChromaDB.")
    parser.add_argument("--data", default="data_out", help="data directory")
    parser.add_argument("--collection", default=COLLECTION_NAME)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate JSONL and build documents/metadata; "
                             "do NOT load chromadb or run embedding. Safe on Windows.")
    parser.add_argument("--limit", type=int, default=0, help="cap pairs (debug)")
    args = parser.parse_args()

    data = Path(args.data)
    pairs_path = data / "my_conversation_pairs.jsonl"
    if not pairs_path.exists():
        sys.stderr.write(f"missing {pairs_path}; run build_dataset.py first.\n")
        return 2

    sys.stderr.write("scanning pairs...\n"); sys.stderr.flush()
    t0 = time.time()

    docs: list[str] = []
    ids: list[str] = []
    metas: list[dict] = []
    errors: list[str] = []
    n = 0

    for i, pair in enumerate(iter_pairs(pairs_path)):
        if args.limit and i >= args.limit:
            break
        errs = validate_pair(pair, i)
        if errs:
            errors.extend(errs)
            continue
        doc = build_document(pair)
        if not doc.strip():
            errors.append(f"pair[{i}] produced empty document")
            continue
        ids.append(f"pair_{i}")
        docs.append(doc)
        metas.append(build_metadata(pair))
        n += 1

    sys.stderr.write(f"prepared {n:,} docs in {time.time()-t0:.1f}s "
                     f"({len(errors)} errors)\n")
    if errors[:10]:
        sys.stderr.write("first errors:\n")
        for e in errors[:10]:
            sys.stderr.write(f"  {e}\n")

    summary = {
        "pairs_total": n,
        "errors": len(errors),
        "model": args.model,
        "collection": args.collection,
        "dry_run": bool(args.dry_run),
        "scan_seconds": round(time.time() - t0, 2),
        "embedding_seconds": None,
        "embedding_dim": None,
        "first_doc_preview": docs[0][:300] if docs else "",
        "first_meta_keys": sorted(metas[0].keys()) if metas else [],
        "limit": args.limit,
    }

    if args.dry_run:
        out = data / "rebuild_chroma_summary.json"
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        if out.exists():
            out.replace(out.with_suffix(out.suffix + ".bak"))
        tmp.replace(out)
        sys.stderr.write(f"DRY-RUN: wrote {out}. No embedding, no chromadb.\n")
        return 0

    # ---- Real run: lazy imports ----
    sys.stderr.write("importing chromadb + sentence_transformers (slow)...\n")
    sys.stderr.flush()
    try:
        import chromadb  # type: ignore
        from chromadb.config import Settings  # type: ignore
        from sentence_transformers import SentenceTransformer  # type: ignore
    except ImportError as e:
        sys.stderr.write(f"missing dependency: {e}\n"
                         f"install with: pip install -r astrbot_plugin_persona_agent/requirements.txt\n")
        return 3

    db_dir = data / "chromadb"
    db_dir.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(db_dir))
    # Drop and recreate for idempotency.
    try:
        client.delete_collection(args.collection)
    except Exception:
        pass
    coll = client.create_collection(args.collection, metadata={"hnsw:space": "cosine"})

    sys.stderr.write(f"loading model {args.model} ...\n"); sys.stderr.flush()
    model = SentenceTransformer(args.model)
    t1 = time.time()
    total = len(docs)
    for start in range(0, total, args.batch):
        chunk = docs[start:start + args.batch]
        emb = model.encode(chunk, normalize_embeddings=True, show_progress_bar=False).tolist()
        coll.add(
            ids=ids[start:start + args.batch],
            documents=chunk,
            embeddings=emb,
            metadatas=metas[start:start + args.batch],
        )
        if (start // args.batch) % 20 == 0:
            sys.stderr.write(f"  embedded {start + len(chunk):,}/{total:,}\n")
            sys.stderr.flush()
    dt_emb = time.time() - t1
    summary["embedding_seconds"] = round(dt_emb, 2)
    summary["embedding_dim"] = len(emb[0]) if emb else None

    out = data / "rebuild_chroma_summary.json"
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if out.exists():
        out.replace(out.with_suffix(out.suffix + ".bak"))
    tmp.replace(out)
    sys.stderr.write(f"done. embedded {total:,} pairs in {dt_emb:.1f}s. "
                     f"collection={args.collection} dir={db_dir}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
