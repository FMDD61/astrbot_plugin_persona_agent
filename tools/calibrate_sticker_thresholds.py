#!/usr/bin/env python3
"""calibrate_sticker_thresholds — 用真实意图短语给贴纸选择标定阈值（S3②）。

## 为什么需要

`StickerService` 的 `min_score=0.45` 与 `margin=0.06` 是**初始拍脑袋值**。
小样本实测已经暴露问题：把「害羞地脸红」与完全无关的「生气 瞪眼 不满」
放在一起，后者的余弦**也有 0.478** —— 比 `min_score=0.45` 还高。
即：**任何意图都能"匹配上"最接近的那张，哪怕完全不相关**，而选不中的行为是
静默跳过，用户只会觉得"它怎么从不发表情"。

本工具用真实分布定阈：
  - **正样本**：意图短语 ~ 应该被选中的贴纸关键词（同主题改写）
  - **负样本**：意图短语 ~ 不相关的贴纸（异主题）
  输出两组分数的分布，据此给出 min_score / margin 的建议值。

## 用法

    python3 -m tools.calibrate_sticker_thresholds --index data_out/sticker_index.json
    python3 -m tools.calibrate_sticker_thresholds --index ... --intents my_intents.txt

无 `--intents` 时从索引自身的 tags 里自动构造正/负样本（同词为正中、
随机交叉为负），足以看出分布形状。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

DEFAULT_MODEL = "BAAI/bge-base-zh-v1.5"


def load_items(index_path: Path) -> list[dict]:
    with open(index_path, encoding="utf-8") as f:
        d = json.load(f)
    return [it for it in (d.get("items") or [])
            if it.get("embedding") and (it.get("tags") or it.get("desc"))]


def cos(a, b) -> float:
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(len(s) * p)))
    return s[i]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="贴纸选择阈值标定（S3②）")
    ap.add_argument("--index", required=True, help="sticker_index.json")
    ap.add_argument("--intents", default="", help="意图短语文件（每行一条，可选）")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--samples", type=int, default=3000, help="负样本采样数")
    args = ap.parse_args(argv)

    idx = Path(args.index).expanduser().resolve()
    items = load_items(idx)
    if len(items) < 2:
        print(f"索引可用条目太少（{len(items)}）—— 先建库", file=sys.stderr)
        return 1
    print(f"索引: {idx}（{len(items)} 条可用）")

    # ---- 嵌入意图 ----
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    from sentence_transformers import SentenceTransformer  # type: ignore
    m = SentenceTransformer(args.model, local_files_only=True)

    if args.intents:
        raw = [l.strip() for l in Path(args.intents).read_text(encoding="utf-8").splitlines()
               if l.strip() and not l.startswith("#")]
        intents = raw
        print(f"意图短语: {len(intents)} 条（来自 {args.intents}）")
    else:
        # 用各条目的 tags 交叉构造：同条目的 tags 作正样本查询
        intents = []
        for it in items:
            tags = [str(t) for t in (it.get("tags") or [])][:3]
            if tags:
                intents.append(" ".join(tags))
        intents = intents[:400]
        print(f"意图短语: {len(intents)} 条（由索引 tags 自动构造）")

    qvecs = m.encode(intents, normalize_embeddings=True)
    qvecs = qvecs.tolist() if hasattr(qvecs, "tolist") else [list(v) for v in qvecs]

    # ---- 正样本：查询 vs 其来源条目（tags 模式）/ 全部条目取 top1（intents 模式）----
    pos: list[float] = []
    top1: list[float] = []
    for qv in qvecs:
        sims = [cos(qv, it["embedding"]) for it in items]
        best = max(sims)
        top1.append(best)
        pos.append(best)          # top1 作为"最相关"的代理

    # ---- 负样本：随机配对 ----
    rnd = random.Random(42)
    neg: list[float] = []
    for _ in range(min(args.samples, len(items) * 20)):
        qv = qvecs[rnd.randrange(len(qvecs))]
        it = items[rnd.randrange(len(items))]
        neg.append(cos(qv, it["embedding"]))

    print()
    print("=== top1 分数分布（每条意图的最相关贴纸）===")
    for p in (0.05, 0.25, 0.5, 0.75, 0.95):
        print(f"  p{int(p*100):02d} = {pct(top1, p):.3f}")
    print(f"  min = {min(top1):.3f}  max = {max(top1):.3f}")
    print()
    print("=== 随机配对分布（= 噪声水平）===")
    for p in (0.05, 0.5, 0.95, 0.99):
        print(f"  p{int(p*100):02d} = {pct(neg, p):.3f}")
    print(f"  max = {max(neg):.3f}")

    noise_p99 = pct(neg, 0.99)
    top1_p05 = pct(top1, 0.05)
    print()
    print("=== 建议 ===")
    print(f"  噪声 p99 = {noise_p99:.3f} → min_score 应显著高于它，否则'任何意图都能匹配上'")
    print(f"  top1 p05 = {top1_p05:.3f} → 若理想匹配大多在它之上，阈值可取两者之间")
    lo = max(noise_p99, 0.0)
    hi = top1_p05
    if hi > lo:
        print(f"  ⇒ 建议 min_score ≈ {(lo + hi) / 2:.2f}（在噪声 {lo:.2f} 与弱匹配 {hi:.2f} 之间）")
    else:
        print(f"  ⇒ ⚠️ 噪声与弱匹配重叠（{lo:.2f} ≥ {hi:.2f}）："
              f"说明关键词区分度不足，应先改描述/标签，而不是调阈值")
    print(f"  margin（top1-top2 分差）建议从 top1 分布的分差 p25 起步，当前无数据时保持 0.06 并观察")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
