"""clean_pairs — A7④ RAG 语料清洗：去掉图片占位 reply 对、标记化 context 图片行。

背景（2026-09-09 复盘）：build_dataset 未过滤图片占位 —— merge 把图片消息的
content.text 渲染成 "[图片: xxx.jpg]"，36% 的 reply 是纯图占位（喂 LLM 是
噪音）、62% 的 context 含图行。RAG 命中相关性差一半源于此。

清洗规则：
  1. reply_text 纯图占位（无任何非占位字符）→ 整对丢弃（图流是 A7⑤ 表情通道
     的事，文本 RAG 不留）；reply 中图+文字混合 → 图片占位符删掉保留文字。
  2. context 消息纯图占位 → 替换成 "[图]" 短标记（保留发言顺序/人物语气）；
     图+文字混合 → 去掉占位符保留文字。
  3. reply_text 空/只剩标点 → 丢弃；去首尾空白，压多行。
  4. reply_text 长度 < 2 或 > 200 → 丢弃（超短"打工/投食"单字指令类对风格
     RAG 价值低且易复读）。
输出 data_out/my_conversation_pairs_clean.jsonl（人工核后 rename 覆盖）。

用法: python tools/clean_pairs.py --data data_out [--min-reply 2] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# AstrBot 渲染占位：图片/视频/动画表情/QQ表情/at
PLACEHOLDER_RE = re.compile(
    r"\[(?:图片|图|视频|动画表情|表情|emoji|gif|At|引用消息)[^\]]*\]"
)


def is_pure_placeholder(text: str) -> bool:
    """去掉所有占位符后无可见文字 → 纯占位（线性，无回溯）。"""
    if not (text or "").strip():
        return True
    rest = PLACEHOLDER_RE.sub("", text or "")
    return not rest.strip()


def clean_text(text: str) -> str:
    """去掉占位符，压空白。"""
    t = PLACEHOLDER_RE.sub("", text or "")
    return re.sub(r"\s+", " ", t).strip()


def clean_pair(pair: dict) -> dict | None:
    """清洗单对；返回 None 表示丢弃。"""
    reply = (pair.get("reply_text") or "").strip()
    ctx = pair.get("context") or []

    # reply 纯图 → 丢弃整对
    if is_pure_placeholder(reply):
        return None

    # context 消息：纯图 → [图]；混合 → 去占位留文字
    new_ctx = []
    for m in ctx:
        t = (m.get("text") or "").strip()
        if is_pure_placeholder(t):
            t = "[图]"
        else:
            t = PLACEHOLDER_RE.sub("", t)
            t = re.sub(r"\s+", " ", t).strip()
            if not t:
                continue  # 空行剔除
        nm = dict(m)
        nm["text"] = t
        new_ctx.append(nm)
    if not new_ctx:
        return None

    # reply 混合图+文字 → 去占位
    reply = clean_text(reply)
    if len(reply) < 2 or len(reply) > 200:
        return None

    out = dict(pair)
    out["reply_text"] = reply
    out["context"] = new_ctx
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data_out")
    ap.add_argument("--min-reply", type=int, default=2)
    ap.add_argument("--dry-run", action="store_true", help="只统计不写文件")
    args = ap.parse_args()

    data = Path(args.data)
    src = data / "my_conversation_pairs.jsonl"
    if not src.exists():
        print(f"error: {src} not found", file=sys.stderr)
        return 2

    kept = 0
    dropped_img = 0
    dropped_short = 0
    dropped_ctx = 0
    total = 0
    out_path = data / "my_conversation_pairs_clean.jsonl"

    with open(src, encoding="utf-8") as fin, \
         (open(out_path, "w", encoding="utf-8") if not args.dry_run
          else open("/dev/null", "w")) as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                pair = json.loads(line)
            except json.JSONDecodeError:
                continue
            total += 1
            cleaned = clean_pair(pair)
            if cleaned is None:
                reply = (pair.get("reply_text") or "").strip()
                ctx = pair.get("context") or []
                if is_pure_placeholder(reply):
                    dropped_img += 1
                elif len(clean_text(reply)) < 2 or len(clean_text(reply)) > 200:
                    dropped_short += 1
                else:
                    dropped_ctx += 1
                continue
            fout.write(json.dumps(cleaned, ensure_ascii=False) + "\n")
            kept += 1

    print(f"total={total} kept={kept} dropped(纯图reply)={dropped_img} "
          f"dropped(长度)={dropped_short} dropped(空ctx)={dropped_ctx}")
    if not args.dry_run:
        print(f"written: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
