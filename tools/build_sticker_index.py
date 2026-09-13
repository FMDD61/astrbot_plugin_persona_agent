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

# 送视觉模型前的降采样上限（长边像素）。
# 实测素材：963 张 / 392 MB，最大单张 **13.1 MB** —— 原图 base64 进请求体会
# 让单次调用变得又慢又贵，而描述质量对分辨率并不敏感（≤80 字的短描述）。
# 768px 是实测够用的档位：既保留表情包的表情/文字细节，又把请求体压到几十 KB。
DESC_MAX_EDGE = 768
DESC_JPEG_QUALITY = 82

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

def prepare_for_vision(path: str, max_edge: int = DESC_MAX_EDGE) -> tuple[bytes, str]:
    """把原图压成适合送视觉模型的小图。返回 ``(bytes, mime)``。

    - 长边 > ``max_edge`` 才缩放（小图不动，避免无谓重编码损失）
    - gif 只取第一帧（表情包动图的第一帧通常已含全部语义；也让体积可控）
    - 任何一步失败 → **回退成原图**（宁可慢，也不要因为预处理失败而丢掉描述）
    """
    try:
        from PIL import Image  # type: ignore
        import io
    except ImportError:
        return Path(path).read_bytes(), _mime_of(path)
    try:
        with Image.open(path) as im:
            im.seek(0)                      # gif 取第一帧
            im = im.convert("RGB")
            w, h = im.size
            if max(w, h) > max_edge:
                scale = max_edge / float(max(w, h))
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                               Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=DESC_JPEG_QUALITY, optimize=True)
            return buf.getvalue(), "image/jpeg"
    except Exception:
        return Path(path).read_bytes(), _mime_of(path)


def _mime_of(path: str) -> str:
    ext = Path(path).suffix.lower().lstrip(".")
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/png")


def _clean_env(v: str) -> str:
    """环境变量清洗：去掉首尾空白与**换行**。

    实测踩到：`$(cat key)` 取到的值带尾换行 → 拼进 URL 后 httpx 报
    ``InvalidURL: Invalid port: ':1]'``（报错完全指不到真因）。这类输入
    错误应该在入口处规整，而不是让底层库抛天书。
    """
    return (v or "").strip().strip('"').strip("'").strip()


def _validate_base(api_base: str) -> str:
    """校验 api_base 形态，给出可读报错。返回清洗后的值。"""
    from urllib.parse import urlparse
    b = _clean_env(api_base)
    if not b:
        raise ValueError("STICKER_VISION_API_BASE 为空")
    if any(c in b for c in " \t\n\r"):
        raise ValueError(f"STICKER_VISION_API_BASE 含空白字符（多半是换行混入）: {b!r}")
    u = urlparse(b)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError(f"STICKER_VISION_API_BASE 不是合法 URL: {b!r}")
    return b


def _describe_one(path: str, api_base: str, api_key: str, model: str, timeout: float) -> str:
    """调视觉模型描述一张图。失败返回空串（调用方决定是否中止）。"""
    import httpx

    data, mime = prepare_for_vision(path)
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
    # ⚠️ trust_env=False 是必需的：开发机 `no_proxy` 里含 `[::1]`，而 httpx 会把
    # no_proxy 的每个条目当 URL pattern 解析 → `InvalidURL: Invalid port: ':1]'`，
    # 报错完全指不到真因（实测踩了）。
    # 网关是公网直连（实测 HTTPS 200 / 2.9s，不需要代理），所以绕过 env 代理是安全的。
    with httpx.Client(timeout=timeout, trust_env=False) as c:
        r = c.post(api_base.rstrip("/") + "/chat/completions",
                   headers={"Authorization": f"Bearer {_clean_env(api_key)}"}, json=payload)
        r.raise_for_status()
        d = r.json()
    return extract_text(d)


def extract_text(resp: dict) -> str:
    """从网关响应里取描述文本，**兼容 content 为空、答案落在 reasoning 里的情况**。

    🔴 2026-09-13 实测抓到（81 张失败样本的根因）：部分图片模型会把最终答案
    写进 **`reasoning`** 字段，而 `content` 是空串 —— 例如：

        {"content": "", "reasoning": "1. 分析用户请求… 5. 最后输出：五个Q版角色站于草地…"}

    只读 `content` 就会拿到空串，表现为"模型没返回"（且**静默**）。
    这里按优先级取：`content` → `reasoning` 里"最后输出/最终"段落 → 整段 reasoning。
    """
    msg = ((resp.get("choices") or [{}])[0].get("message") or {})
    text = str(msg.get("content") or "").strip()
    if text:
        return text
    # 有些适配层叫 reasoning_content，网关这里叫 reasoning —— 都试
    reasoning = str(msg.get("reasoning") or msg.get("reasoning_content") or "").strip()
    if not reasoning:
        return ""
    # 优先取"最终输出/润色/结论"之后的内容
    import re as _re
    m = None
    for pat in (r"(?:最后输出|最终输出|最终润色|最终答案|结论)[：:]?\s*([^\n]{2,})",
                r"(?:输出|答案)[：:]?\s*([^\n]{4,})$"):
        m = _re.search(pat, reasoning)
        if m:
            return m.group(1).strip()
    # 兜底：reasoning 的最后一段非空行
    tail = [l.strip() for l in reasoning.splitlines() if l.strip()]
    return tail[-1] if tail else ""


def describe_all(items: list[dict], *, library_dir: Path, api_base: str, api_key: str,
                 model: str, workers: int = 2, timeout: float = 60.0) -> int:
    """并发给 items 填 desc（原地修改）。返回成功数。

    ⚠️ 路径从 ``library_dir / it["file"]`` 解析，**不依赖中间字段** ——
    merge 后的条目只保证有 file/sha256（曾经因为依赖 it["path"] 而 KeyError）。
    workers 默认 2：4 核机器别打满。
    """
    todo = [it for it in items if not it.get("desc")]
    if not todo:
        return 0
    print(f"  视觉描述 {len(todo)} 张（并发 {workers}，模型 {model}）…")
    done = ok = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_describe_one, str(library_dir / it["file"]),
                          api_base, api_key, model, timeout): it
                for it in todo}
        for fut in cf.as_completed(futs):
            it = futs[fut]
            try:
                it["desc"] = fut.result()
            except Exception as e:
                it["desc"] = ""
                print(f"    ! {it['file']}: {type(e).__name__}: {e}", file=sys.stderr)
            if it.get("desc"):
                ok += 1
            done += 1
            if done % 10 == 0 or done == len(todo):
                print(f"    {done}/{len(todo)}（成功 {ok}）", flush=True)
    return ok


# ---------------------------------------------------------------- 嵌入

# 描述里的"关键词"段：`关键词：开心、惊讶` / `检索关键词：紧张、流汗` 等变体
# 要求「标签词 + 分隔符」成对出现才命中。
# 反例（实测踩到）：「没有关键词段的描述，只有画面说明。」—— 句子里含"关键词"
# 三字但**不是标签**；宽松正则会切出 ['段的描述', '只有画面说明'] 并静默污染索引。
_RE_KW_SEG = __import__("re").compile(
    r"(?:检索关键词|关键词|检索词|适合检索|情绪关键词)\s*[：:]\s*([^\n]{2,80})")


_KW_LABEL = "关键词"


def extract_keywords(desc: str) -> list[str]:
    """从视觉描述里抽出关键词段。

    实测 90% 的描述自带 `关键词：A、B、C`（562 条里 503 条）。**嵌入要用它，
    不要用整段描述** —— 描述中位 90 字符（最长 257），而 `[emote:意图短语]`
    只有 15–25 字符。长描述里混着画面细节/台词/梗，会把嵌入"稀释"，
    导致短查询与长文档的余弦相似度系统性偏低、几乎选不中任何东西。
    短↔短匹配才是对的。
    """
    d = desc or ""
    m = _RE_KW_SEG.search(d)
    if not m:
        return []
    raw = m.group(1)
    parts = [x.strip(" 。.、,，;；") for x in __import__("re").split(r"[、,，;；/|]+", raw)]
    # 过滤：太长的多半是整句而非关键词；单字符无意义
    return [x for x in parts if 1 < len(x) <= 12][:8]


def embed_all(items: list[dict], *, model_name: str, backend=None) -> int:
    """给 items 填 embedding（原地修改）。返回维度。

    ``backend`` 可注入（测试用）—— 形如 sentence-transformers 的对象：
    需有 ``encode(texts, normalize_embeddings=True) -> list[list[float]]``
    与 ``get_sentence_embedding_dimension()``。
    """
    todo = [it for it in items if not it.get("embedding")]
    if not todo:
        return int(items[0]["embedding"].__len__()) if items and items[0].get("embedding") else 0
    m = backend
    if m is None:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError:
            print("  ! sentence-transformers 不可用 → 跳过嵌入（索引将不可用于选择）",
                  file=sys.stderr)
            return 0
        print(f"  载入嵌入模型 {model_name} …")
        m = SentenceTransformer(model_name, local_files_only=True)
    dim = int(m.get_sentence_embedding_dimension())
    # 嵌入文本优先级（短↔短匹配）：
    #   ① tags（人工修正通道，最高优先）
    #   ② 从描述里抽出的关键词段（90% 的描述自带）
    #   ③ 整段描述（兜底：没有关键词段时只能用它）
    texts = []
    for it in todo:
        tags = [str(t) for t in (it.get("tags") or []) if str(t).strip()]
        if not tags:
            kws = extract_keywords(it.get("desc", ""))
            if kws:
                it["tags"] = kws          # 落进索引，供人工修正与 LLM 精选
            tags = kws
        basis = " ".join(tags) if tags else (it.get("desc") or "")
        if not basis.strip():
            basis = it.get("file", "")    # 连描述都没有 → 用文件名占位（不会选中的）
        texts.append(basis)
    raw_vecs = m.encode(texts, normalize_embeddings=True)
    # sentence-transformers 返回 ndarray（.tolist()），测试桩可能直接给 list
    vecs = raw_vecs.tolist() if hasattr(raw_vecs, "tolist") else [list(v) for v in raw_vecs]
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


def retry_missing(items: list[dict], *, library_dir: Path, api_base: str, api_key: str,
                  model: str, rounds: int = 2, pause: float = 3.0) -> int:
    """对描述为空的条目**重试**（429 限流 / 空返回是主要成因）。

    实测（2026-09-13，959 张）：并发 2 时失败率约 46%，其中大部分是
    `429 Too Many Requests`（**并发不是主因，是网关侧限流**）与模型空返回。
    这些多数是瞬时的 —— 放慢节奏重试即可救回大半。

    策略：逐张串行 + 每张之间 sleep `pause`，共 `rounds` 轮；只补空的，
    已有描述的条目不动（增量语义）。
    """
    import time as _t
    total_ok = 0
    for r in range(rounds):
        todo = [it for it in items if not (it.get("desc") or "").strip()]
        if not todo:
            break
        print(f"  重试第 {r + 1}/{rounds} 轮：待补 {len(todo)} 张（串行 + {pause}s 间隔）",
              flush=True)
        ok = 0
        for i, it in enumerate(todo, 1):
            try:
                d = _describe_one(str(library_dir / it["file"]),
                                  api_base, api_key, model, 90.0)
                if d:
                    it["desc"] = d
                    ok += 1
            except Exception as e:
                print(f"    ! {it['file'][:36]}: {type(e).__name__}", flush=True)
            if i % 20 == 0:
                print(f"    {i}/{len(todo)}（本轮成功 {ok}）", flush=True)
            _t.sleep(pause)
        total_ok += ok
        print(f"    本轮救回 {ok} 张", flush=True)
        if ok == 0:
            break
    return total_ok


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
    ap.add_argument("--retry-rounds", type=int, default=0,
                    help="描述为空时补跑轮数（串行 + 间隔；429 限流场景很有用）")
    ap.add_argument("--retry-pause", type=float, default=3.0, help="重试轮内每张间隔秒数")
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
            try:
                api_base = _validate_base(api_base)
            except ValueError as e:
                print(f"  ! {e} → 跳过视觉描述", file=sys.stderr)
                api_base = ""
            if api_base:
                describe_all(items, library_dir=lib, api_base=api_base, api_key=api_key,
                             model=args.model, workers=args.workers)
                if args.retry_rounds > 0:
                    retry_missing(items, library_dir=lib, api_base=api_base,
                                  api_key=api_key, model=args.model,
                                  rounds=args.retry_rounds, pause=args.retry_pause)

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
