#!/usr/bin/env python3
"""build_sticker_index — 贴纸库离线入库工具（S3②，spec §4.3）。

把 `sticker_library/` 里的原图变成 `sticker_index.json`：视觉描述 + BGE 嵌入。

## 流程

    scan（sha256 去重）→ [--vision 描述] → BGE 嵌入 → 写索引（增量，不覆盖人工修正）

## 设计要点

- **增量**：已入库的 sha256 跳过（保留其 desc/tags/embedding）；`--force` 才重算。
  这样人工改过 desc/tags 的条目不会被工具覆盖（红线 #3：工具只建议不覆盖）。
- **`--review`**：打印表格供人工过目。描述错了会导致"该发害羞却发了嘲讽"，
  所以入库前**必须**有人看一眼。
- **并发 2**：台式机 4 核且 AstrBot 常驻 BGE —— 打满核会拖慢线上回复。
- **纯 CPU 工具**：嵌入用 BGE（本地 HF 缓存，离线）；描述用视觉模型（要网关 key）。
  没给 `--vision` 时 desc 留空，只算嵌入 —— 但**空 desc 的贴纸基本选不中**，
  所以正式入库一定要带 `--vision`。

用法：
    python3 -m tools.build_sticker_index --dir data_out/sticker_library --vision
    python3 -m tools.build_sticker_index --dir ... --review        # 只看，不写
    python3 -m tools.build_sticker_index --dir ... --force         # 重算全部
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
DEFAULT_INDEX = "sticker_index.json"
DEFAULT_MODEL = "BAAI/bge-base-zh-v1.5"

# 视觉描述提示词：与 services/vision.py 同源（表情包要说明情绪与梗）
VISION_SYS = (
    "用中文简要描述这张图片中确定可见的内容，不超过80字；"
    "如是表情包说明其情绪和梗，并给出 2-4 个适合检索的情绪/动作关键词。"
    "不要猜测人物身份、不要脑补图中没有的内容；看不清就说看不清。"
)


# ---------------------------------------------------------------- 扫描

def scan_images(library_dir: Path) -> tuple[list[dict], list[str]]:
    """列出目录内图片 + sha256。**同内容只取第一张**（按文件名排序）。

    返回 ``(条目, 被跳过的重复文件名)``。为什么在扫描阶段就去重：同图改名
    （或 `001.png` / `001 - 副本.png`）会让索引出现两条一模一样的贴纸，
    既浪费库位又让选择器面对无意义的并列候选。
    """
    out: list[dict] = []
    skipped: list[str] = []
    seen: set[str] = set()
    if not library_dir.is_dir():
        return out, skipped
    for p in sorted(library_dir.iterdir()):
        if not p.is_file() or p.suffix.lower() not in EXTS:
            continue
        try:
            data = p.read_bytes()
        except OSError as e:
            print(f"  ! 读取失败 {p.name}: {e}", file=sys.stderr)
            continue
        sha = hashlib.sha256(data).hexdigest()
        if sha in seen:
            skipped.append(p.name)
            continue
        seen.add(sha)
        out.append({
            "file": p.name,
            "path": str(p),
            "sha256": sha,
            "bytes": len(data),
        })
    return out, skipped


# ---------------------------------------------------------------- 描述（可选）

def _describe_one(path: str, api_base: str, api_key: str, model: str, timeout: float) -> str:
    """调视觉模型描述一张图。失败返回空串（调用方决定是否中止）。"""
    import httpx

    data = Path(path).read_bytes()
    ext = Path(path).suffix.lower().lstrip(".")
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/png")
    b64 = base64.b64encode(data).decode()
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": VISION_SYS},
            {"role": "user", "content": [
                {"type": "text", "text": "描述这张图片。"},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ]},
        ],
        "max_tokens": 512, "temperature": 0.3, "reasoning_effort": "low",
    }
    with httpx.Client(timeout=timeout) as c:
        r = c.post(api_base.rstrip("/") + "/chat/completions",
                   headers={"Authorization": f"Bearer {api_key}"}, json=payload)
        r.raise_for_status()
        d = r.json()
    return (((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()


def describe_all(items: list[dict], *, api_base: str, api_key: str, model: str,
                 workers: int = 2, timeout: float = 60.0) -> None:
    """并发给 items 填 desc（原地修改）。workers 默认 2：4 核机器别打满。"""
    todo = [it for it in items if not it.get("desc")]
    if not todo:
        return
    print(f"  视觉描述 {len(todo)} 张（并发 {workers}，模型 {model}）…")
    done = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_describe_one, it["path"], api_base, api_key, model, timeout): it
                for it in todo}
        for fut in cf.as_completed(futs):
            it = futs[fut]
            try:
                it["desc"] = fut.result()
            except Exception as e:
                it["desc"] = ""
                print(f"    ! {it['file']}: {type(e).__name__}: {e}", file=sys.stderr)
            done += 1
            if done % 5 == 0 or done == len(todo):
                print(f"    {done}/{len(todo)}")


# ---------------------------------------------------------------- 嵌入

def embed_all(items: list[dict], *, model_name: str) -> int:
    """给 items 填 embedding（原地修改）。返回维度。"""
    todo = [it for it in items if not it.get("embedding")]
    if not todo:
        return int(items[0]["embedding"].__len__()) if items and items[0].get("embedding") else 0
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except ImportError:
        print("  ! sentence-transformers 不可用 → 跳过嵌入（索引将不可用于选择）", file=sys.stderr)
        return 0
    print(f"  载入嵌入模型 {model_name} …")
    m = SentenceTransformer(model_name, local_files_only=True)
    dim = int(m.get_sentence_embedding_dimension())
    # 描述 + tags 一起编码：tags 是人工修正通道，应该影响检索
    texts = [" ".join([it.get("desc", "")] + list(it.get("tags") or [])) for it in todo]
    vecs = m.encode(texts, normalize_embeddings=True).tolist()
    for it, v in zip(todo, vecs):
        it["embedding"] = [round(float(x), 6) for x in v]
    print(f"    {len(todo)} 条，维度 {dim}")
    return dim


# ---------------------------------------------------------------- 索引读写

def load_index(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "model": DEFAULT_MODEL, "dim": 0, "items": []}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or not isinstance(d.get("items"), list):
            raise ValueError("bad shape")
        return d
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"  ! 既有索引不可读（将当作空库）: {e}", file=sys.stderr)
        return {"version": 1, "model": DEFAULT_MODEL, "dim": 0, "items": []}


def merge(existing: dict, scanned: list[dict], *, force: bool) -> tuple[list[dict], int, int]:
    """按 sha256 合并。返回 (条目, 新增数, 复用数)。

    **复用 = 保留既有的 desc/tags/embedding** —— 人工修正不被覆盖（红线 #3）。
    """
    by_sha = {it.get("sha256"): it for it in existing.get("items", []) if it.get("sha256")}
    out: list[dict] = []
    added = reused = 0
    for s in scanned:
        prev = by_sha.get(s["sha256"])
        if prev and not force:
            out.append(prev)
            reused += 1
            continue
        stem = Path(s["file"]).stem
        out.append({
            "id": prev.get("id") if prev else stem,
            "file": s["file"],
            "sha256": s["sha256"],
            "bytes": s["bytes"],
            "desc": "" if force or not prev else prev.get("desc", ""),
            "tags": ([] if force or not prev else prev.get("tags") or []),
            "embedding": None if force else (prev or {}).get("embedding"),
            "added_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        })
        added += 1
    return out, added, reused


def review_table(items: list[dict]) -> None:
    print()
    print(f"{'id':<22} {'file':<26} {'desc'}")
    print("-" * 100)
    for it in items:
        d = (it.get("desc") or "").replace("\n", " ")
        flag = "  ⚠️空描述(基本选不中)" if not d else ""
        print(f"{(it.get('id') or '')[:21]:<22} {(it.get('file') or '')[:25]:<26} {d[:44]}{flag}")
    missing_emb = sum(1 for it in items if not it.get("embedding"))
    if missing_emb:
        print(f"\n⚠️ {missing_emb} 条缺 embedding → 无法参与选择（检查 sentence-transformers）")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="贴纸库离线入库（S3②）")
    ap.add_argument("--dir", required=True, help="贴纸原图目录（人工投喂）")
    ap.add_argument("--out", default="", help=f"索引输出路径（默认 <dir>/../{DEFAULT_INDEX}）")
    ap.add_argument("--vision", action="store_true", help="调视觉模型生成 desc（正式入库必带）")
    ap.add_argument("--model", default="deepseek/deepseek-v4.1-flash",
                    help="视觉模型名（须带网关命名空间前缀）")
    ap.add_argument("--embed-model", default=DEFAULT_MODEL)
    ap.add_argument("--force", action="store_true", help="重算全部（会丢弃人工修正！）")
    ap.add_argument("--review", action="store_true", help="只打印审核表，不写文件")
    ap.add_argument("--workers", type=int, default=2, help="视觉并发（默认 2，别打满 4 核）")
    args = ap.parse_args(argv)

    lib = Path(args.dir).expanduser().resolve()
    out = Path(args.out).expanduser().resolve() if args.out else lib.parent / DEFAULT_INDEX
    print(f"库目录: {lib}")
    print(f"索引:   {out}")

    scanned, dupes = scan_images(lib)
    print(f"\n扫描到 {len(scanned)} 张图（{sum(s['bytes'] for s in scanned)/1024:.0f} KB）")
    if dupes:
        print(f"  跳过 {len(dupes)} 张内容重复（同 sha256）：{', '.join(dupes[:6])}"
              + (" …" if len(dupes) > 6 else ""))
    if not scanned:
        print("库为空 —— 先把贴纸放进目录再跑")
        return 1

    existing = load_index(out)
    items, added, reused = merge(existing, scanned, force=args.force)
    print(f"新增/重算 {added} 条，复用（保留人工修正）{reused} 条")

    if args.vision and any(not it.get("desc") for it in items):
        api_base = os.environ.get("STICKER_VISION_API_BASE", "")
        api_key = os.environ.get("STICKER_VISION_API_KEY", "")
        if not api_base or not api_key:
            print("  ! 未提供 STICKER_VISION_API_BASE/KEY → 跳过视觉描述", file=sys.stderr)
        else:
            describe_all(items, api_base=api_base, api_key=api_key,
                         model=args.model, workers=args.workers)

    dim = embed_all(items, model_name=args.embed_model)

    review_table(items)
    if args.review:
        print("\n[--review] 未写文件。确认无误后去掉 --review 重跑。")
        return 0

    payload = {
        "version": 1,
        "model": args.embed_model,
        "dim": dim,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "items": items,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(out) + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, out)
    print(f"\n✅ 写入 {out}（{len(items)} 条，dim={dim}）")
    empty = sum(1 for it in items if not (it.get("desc") or "").strip())
    if empty:
        print(f"⚠️ {empty} 条描述为空 → 基本选不中。带 --vision 重跑。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
