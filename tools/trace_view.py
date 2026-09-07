"""trace_view.py — 把 trace_log.jsonl 渲染成可读报告（A7 2b）。

设计目标：接近 dsh「模型可见，日志即记录」——在终端/dsh 会话里直接查看
一次消息处理的完整决策链，不需要另搞前端。

用法：
  python -m astrbot_plugin_persona_agent.tools.trace_view \
      --data-dir <plugin_data_dir> [--group 123456789] [--limit 20] [--since 2026-09-07]

默认 --data-dir 指向插件运行时数据目录（含 logs/<group_id>/trace_log.jsonl）。
也兼容读取旧根目录 trace_log.jsonl（无 logs/ 结构时）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _find_trace_files(data_dir: Path, group: str = "") -> list[Path]:
    """Locate trace_log.jsonl(s): logs/<g>/trace_log.jsonl or legacy root."""
    files: list[Path] = []
    if group:
        p = data_dir / "logs" / group / "trace_log.jsonl"
        if p.exists():
            files.append(p)
        else:
            # fall back to legacy root
            root = data_dir / "trace_log.jsonl"
            if root.exists():
                files.append(root)
    else:
        per_group = sorted((data_dir / "logs").glob("*/trace_log.jsonl")) if (data_dir / "logs").exists() else []
        files.extend(per_group)
        root = data_dir / "trace_log.jsonl"
        if root.exists():
            files.append(root)
    return files


def _load_records(paths: list[Path], limit: int) -> list[dict]:
    recs: list[dict] = []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        recs.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
    recs.sort(key=lambda r: r.get("ts_epoch") or 0.0)
    return recs[-limit:] if limit > 0 else recs


def _render_one(r: dict) -> str:
    lines: list[str] = []
    inp = r.get("input") or {}
    gid = r.get("group_id") or inp.get("group_id") or "?"
    ts = r.get("ts") or ""
    at = " @bot" if inp.get("is_at") else ""
    lines.append(f"━━━ [{ts}] group={gid}{at} ━━━")
    lines.append(f"  入站: {inp.get('text', '')!r}  (sender={inp.get('sender_alias') or inp.get('sender_uin')})")

    hg = r.get("hard_gate") or {}
    if hg:
        lines.append(f"  硬闸: {hg.get('action')}/{hg.get('trigger')} — {hg.get('reason')}")

    gate = r.get("gate")
    if gate is not None:
        flag = " (fallback)" if gate.get("fallback") else (" (cached)" if gate.get("cached") else "")
        lines.append(f"  GateLLM: reply={gate.get('reply')}{flag} — {gate.get('reason')}")

    rag = r.get("rag") or []
    if rag:
        lines.append("  RAG 命中:")
        for h in rag:
            score = h.get("score")
            doc = (h.get("document") or "")[:120]
            lines.append(f"    [{score}] {doc}")
    else:
        lines.append("  RAG 命中: (无)")

    emo = r.get("emotion") or {}
    if emo:
        lines.append(f"  情绪: willingness={emo.get('willingness')} mood={emo.get('mood')!r} sticker={bool(emo.get('sticker'))}")

    kg = r.get("kg_tail")
    if kg:
        lines.append(f"  KG 尾注: {kg[:120]}")

    sess = r.get("session") or {}
    if sess:
        lines.append(f"  session: {sess.get('size')} msgs / {sess.get('chars')} chars")

    if "temperature" in r:
        lines.append(f"  温度: {r.get('temperature')}")

    raw = r.get("raw_generation")
    if raw is not None:
        lines.append(f"  LLM 原始: {raw}")
    final = r.get("final_text")
    if final is not None:
        lines.append(f"  最终发送: {final}")

    err = r.get("error")
    if err:
        lines.append(f"  ⚠ 错误: {err}")
    silent = r.get("silent_reason") or ("" if not r.get("final_text") and not raw else "")
    # silent reason surfaced via SendIntent is embedded by caller as extra keys;
    # nothing else to add here.
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Render trace_log.jsonl as a readable report")
    ap.add_argument("--data-dir", required=True, help="插件运行时数据目录")
    ap.add_argument("--group", default="", help="只显示某群 (logs/<group_id>/)")
    ap.add_argument("--limit", type=int, default=20, help="最多显示最近 N 轮 (默认 20, 0=全部)")
    ap.add_argument("--since", default="", help="只显示 ts >= 该时间前缀 (如 2026-09-07)")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        print(f"data_dir 不存在: {data_dir}", file=sys.stderr)
        return 1

    files = _find_trace_files(data_dir, args.group)
    if not files:
        print("未找到 trace_log.jsonl（检查 --data-dir / --group）", file=sys.stderr)
        return 1

    recs = _load_records(files, args.limit)
    if args.since:
        recs = [r for r in recs if (r.get("ts") or "").startswith(args.since)]

    print(f"# trace 报告: {len(recs)} 轮" + (f" (group={args.group})" if args.group else ""))
    print(f"# 来源: {', '.join(str(f) for f in files)}\n")
    for r in recs:
        print(_render_one(r))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
