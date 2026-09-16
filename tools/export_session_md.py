#!/usr/bin/env python3
"""export_session_md —— 把 session JSON 导出成**给人看的 markdown**（S16）。

## 为什么（用户 2026-09-15）

> "人工看：我认为这是必须的一环，LLM 总是比人工快，所以关键在**提高人工看的
>  效率**。脚本化 session 日志导出成 markdown 文件的流程，结构化文档辅助下，
>  人工查看的速度能大涨。人工要求看到 **LLM 能看到和不能看到的**，包括不同的
>  消息类型（system, user, assistant, tool 等）、**思维链**、KG 内容等等"

对话质量的评估**最终必须靠人工抽样** —— 我的自动化检测器已实测有 3 类 confound
（昵称后缀、别称、玩梗用名），数字不可靠。所以工具的目标是**让人的单位时间
看得更多、更准**：

- **分段渲染**：每条消息一个小节，带序号（便于口头引用"第 42 条"）
- **显式标注思维链**：它不进 LLM 上下文，但对排查"为什么这么回"最关键
- **内部元数据可见**：`_mid`/`_uin` 是人类排查线索（我的检测器就是缺它们才误判）
- **额外上下文**：KG 尾注 / RAG 命中 / 当时的 system prompt / 可变块 —— 这些
  "LLM 能看到但不在 session 里"的内容，靠 `--extra` 一并附上

## 用法

    # 导出单个 session
    python3 -m tools.export_session_md \\
        --session /path/session_881438753_2026-09-16.json --out review.md

    # 导出目录下全部 session（每群每天一个文件）
    python3 -m tools.export_session_md --dir /path/plugin_data --out-dir ./exports

    # 只看最近 N 条（大 session 动辄 1300+ 条，人工看不完）
    python3 -m tools.export_session_md --session ... --out out.md --tail 200

    # 带额外上下文（KG/RAG 来自 trace_log，可另用 --extra-from-trace）
    python3 -m tools.export_session_md --session ... --out out.md \\
        --extra-from-trace /path/logs/881438753/trace_log.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

#: 消息类型的中文标签（用户点名要看"不同的消息类型"）
ROLE_LABEL = {
    "system": "系统",
    "user": "群友",
    "assistant": "机器人",
    "tool": "工具",
    "function": "函数",
}
#: 内部键 → 展示名（人工排查线索）
INTERNAL_LABEL = {
    "_mid": "消息ID",
    "_uin": "QQ",
    "_reasoning": "思维链",
}


def render_message(m: dict, index: int, *, max_reasoning: int = 0) -> str:
    """把一条消息渲染成 markdown 小节。

    ``max_reasoning>0`` 时截断思维链（默认 0 = 全文，用户要求"完整思维链"）。
    """
    if not isinstance(m, dict):
        return f"### [{index}] （非法条目）\n\n```\n{m!r}\n```\n"
    role = str(m.get("role") or "unknown")
    label = ROLE_LABEL.get(role, role)
    name = m.get("name")
    head = f"### [{index}] {role}"
    if label != role:
        head += f"（{label}）"
    if name:
        head += f" · {name}"

    lines = [head, ""]
    content = str(m.get("content") or "")
    if content:
        lines += ["```text", content, "```", ""]
    else:
        lines += ["_（空内容）_", ""]

    # 内部元数据（LLM 看不到，但人工排查需要）
    metas = []
    for k, lbl in INTERNAL_LABEL.items():
        if k == "_reasoning":
            continue
        v = m.get(k)
        if v:
            metas.append(f"{lbl}=`{v}`")
    if metas:
        lines += ["> " + " · ".join(metas), ""]

    # 思维链：**显式标注**（它是最关键的排查材料，但不在 LLM 上下文里）
    reasoning = str(m.get("_reasoning") or "")
    if reasoning:
        if max_reasoning and len(reasoning) > max_reasoning:
            reasoning = reasoning[:max_reasoning] + f"\n…（截断，共 {len(reasoning)} 字）"
        lines += ["<details><summary>🧠 思维链（<b>不进 LLM 上下文</b>）</summary>",
                  "", "```text", reasoning, "```", "", "</details>", ""]
    return "\n".join(lines)


def export_markdown(session_path: Path, out_path: Path, *,
                    tail: int = 0, extra: dict | None = None,
                    max_reasoning: int = 0) -> dict:
    """导出一个 session JSON → markdown。返回统计。**绝不抛**。"""
    stats: dict = {"source": str(session_path), "out": str(out_path)}
    try:
        payload = json.loads(Path(session_path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("session 文件顶层不是对象")
    except Exception as e:
        stats["error"] = f"{type(e).__name__}: {e}"
        try:
            Path(out_path).write_text(
                f"# 导出失败\n\n`{session_path}`\n\n```\n{stats['error']}\n```\n",
                encoding="utf-8")
        except OSError:
            pass
        return stats

    gid = str(payload.get("group_id") or "")
    day = str(payload.get("day") or "")
    msgs = payload.get("messages") or []
    blocks = payload.get("sys_blocks") or {}
    sys_prompt = str(payload.get("system_prompt") or "")

    # 还原可变块标记
    rendered: list[dict] = []
    if sys_prompt:
        rendered.append({"role": "system", "content": sys_prompt})
    for m in msgs:
        if isinstance(m, dict) and "__sys_block__" in m:
            c = blocks.get(str(m["__sys_block__"]))
            if c:
                rendered.append({"role": "system", "content": c,
                                 "_kind": "可变块（设定更新）"})
            continue
        if isinstance(m, dict):
            rendered.append(m)

    total = len(rendered)
    shown = rendered[-tail:] if tail and tail < total else rendered
    start_idx = total - len(shown)

    parts: list[str] = [
        f"# Session 导出 · 群 {gid} · {day}",
        "",
        f"- 源文件：`{session_path}`",
        f"- 消息总数：**{total}**" + (f"（本次展示最后 {len(shown)} 条）" if tail and tail < total else ""),
        f"- 思维链：{'有' if any(m.get('_reasoning') for m in rendered) else '无'}",
        "",
        "> 说明：**思维链不进 LLM 上下文**，但列在这里供人工排查。",
        "> `_mid`/`_uin` 等内部元数据同样不进 LLM 请求。",
        "",
        "---",
        "",
    ]

    if extra:
        parts += ["## 额外上下文（LLM 能看到，但不在 session 里）", ""]
        for k, v in extra.items():
            parts += [f"### {k}", "", "```text",
                      str(v)[:4000], "```", ""]
        parts += ["---", ""]

    reasoning_chars = 0
    for i, m in enumerate(shown):
        parts.append(render_message(m, start_idx + i, max_reasoning=max_reasoning))
        reasoning_chars += len(str(m.get("_reasoning") or ""))

    try:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text("\n".join(parts), encoding="utf-8")
    except OSError as e:
        stats["error"] = f"写入失败 {type(e).__name__}: {e}"
        return stats

    stats.update({"messages": total, "shown": len(shown),
                  "reasoning_chars": reasoning_chars,
                  "out_bytes": Path(out_path).stat().st_size})
    return stats


def _load_trace_extra(trace_path: Path, limit: int = 8) -> dict:
    """从 trace_log 取最近的 KG/RAG 内容，作为"LLM 能看到但不在 session 里"的补充。"""
    out: dict = {}
    try:
        rows = []
        for line in Path(trace_path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        rows = [r for r in rows if isinstance(r, dict)][-limit:]
        kgs = [str((r.get("kg_tail") or "")) for r in rows if r.get("kg_tail")]
        if kgs:
            out["KG 尾注（最近几条）"] = "\n---\n".join(kgs[-3:])
        rag = []
        for r in rows:
            for h in (r.get("rag") or [])[:2]:
                doc = str((h or {}).get("document") or "")[:200]
                if doc:
                    rag.append(f"[{h.get('score')}] {doc}")
        if rag:
            out["RAG 命中（最近几条）"] = "\n".join(rag[-6:])
    except OSError:
        pass
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="session JSON → 人工可读 markdown")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--session", help="单个 session JSON")
    src.add_argument("--dir", help="插件数据目录（导出其下全部 session_*.json）")
    ap.add_argument("--out", help="输出 .md（--session 时用）")
    ap.add_argument("--out-dir", default="./session_exports", help="--dir 时的输出目录")
    ap.add_argument("--tail", type=int, default=0, help="只导出最后 N 条（0=全部）")
    ap.add_argument("--max-reasoning", type=int, default=0,
                    help="思维链截断长度（0=完整，用户要求默认完整）")
    ap.add_argument("--extra-from-trace", default="",
                    help="从 trace_log.jsonl 补充 KG/RAG 上下文")
    args = ap.parse_args(argv)

    extra = {}
    if args.extra_from_trace:
        extra = _load_trace_extra(Path(args.extra_from_trace))
        print(f"额外上下文: {list(extra) or '（未取到）'}")

    if args.session:
        out = Path(args.out or (Path(args.session).with_suffix(".md")))
        st = export_markdown(Path(args.session), out, tail=args.tail,
                             extra=extra, max_reasoning=args.max_reasoning)
        if st.get("error"):
            print(f"❌ {st['error']}", file=sys.stderr)
            return 1
        print(f"✅ {out}  {st['shown']}/{st['messages']} 条 "
              f"思维链 {st['reasoning_chars']} 字 {st['out_bytes']//1024}KB")
        return 0

    d = Path(args.dir).expanduser().resolve()
    files = sorted(d.glob("session_*.json"))
    if not files:
        print(f"{d} 下没有 session_*.json", file=sys.stderr)
        return 1
    out_dir = Path(args.out_dir).expanduser().resolve()
    ok = 0
    for f in files:
        st = export_markdown(f, out_dir / (f.stem + ".md"), tail=args.tail,
                             extra=extra, max_reasoning=args.max_reasoning)
        if st.get("error"):
            print(f"❌ {f.name}: {st['error']}", file=sys.stderr)
        else:
            ok += 1
            print(f"✅ {f.name} → {st['shown']} 条 "
                  f"（思维链 {st['reasoning_chars']} 字）")
    print(f"\n共导出 {ok}/{len(files)} 个 → {out_dir}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
