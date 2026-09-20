#!/usr/bin/env python3
"""从 docs/specs/rp_examples_draft_v1.md 生成 services/examples_default.py（C1/C2）。

用法：

    python3 tools/gen_examples_default.py            # 生成（覆盖 examples_default.py）
    python3 tools/gen_examples_default.py --check    # 只校验一致性（单测用）

口径（用户 2026-09-20 定）：**示例块只保留新 20 条**，部署时用文件替换的方式；
**任何情况下都不回退到旧示例句** —— 所以旧句在这份代码里一个字都不存在，
数据目录文件缺失时回落的就是这 20 条。

纪律同 gen_persona_sections.py：抽不到就报错，绝不静默产出空串。
"""
from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEFAULT_SPECS = REPO.parent / "docs" / "specs"
OUT = REPO / "services" / "examples_default.py"


def blocks(md: str) -> list[str]:
    return re.findall(r"\n```[a-zA-Z]*\n(.*?)\n```", md, re.S)


def parse_entries(body: str) -> list[dict]:
    """把草案的逐行示例解析成 example_dialogs.json 的结构。"""
    out: list[dict] = []
    for raw in body.strip().splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^\[(?P<topic>[^\]]+)\]\s*(?P<rest>.+)$", line)
        if not m:
            raise SystemExit(f"示例行格式不对（缺 [话题] 前缀）: {line[:60]!r}")
        topic = m.group("topic").strip()
        parts = [x for x in re.split(r"\s{2,}", m.group("rest").strip()) if x]
        messages = []
        for part in parts:
            if ":" not in part:
                raise SystemExit(f"示例片段缺 role: {part[:40]!r}")
            role, content = part.split(":", 1)
            messages.append({"role": role.strip(), "content": content.strip()})
        if len(messages) < 2:
            raise SystemExit(f"示例条目少于两条消息: {line[:60]!r}")
        out.append({"topic": topic, "messages": messages})
    return out


def build(specs: Path) -> tuple[str, list[dict]]:
    md = (specs / "rp_examples_draft_v1.md").read_text(encoding="utf-8")
    bs = blocks(md)
    if not bs:
        raise SystemExit("rp_examples_draft_v1.md: 未取到代码块")
    header_raw = bs[0].strip()
    if not header_raw.startswith("示例对话"):
        raise SystemExit(f"HEADER 块异常: {header_raw[:40]!r}")
    body = next((b for b in bs if b.lstrip().startswith("[")), None)
    if body is None:
        raise SystemExit("未找到以话题前缀开头的示例正文块")
    entries = parse_entries(body)
    if len(entries) != 20:
        raise SystemExit(f"示例条数不是 20（实际 {len(entries)}）")
    # ⚠️ 草案 §C 的覆盖矩阵声称"8 条带 [emote:]"，但 §B 的实际文本里只有 5 条 ——
    # 文档与文本不一致（已向使用者报备）。这里**只报告不设限**：文案以 §B 的实际文本为准，
    # 生成器不替作者改数。
    return header_raw, entries


def render(header: str, entries: list[dict]) -> str:
    doc = [
        "示例块默认文案（C1/C2）—— 由 docs/specs 的草案生成，勿手改。",
        "",
        "生成器：tools/gen_examples_default.py（改文案请改 docs/specs/rp_examples_draft_v1.md，再重跑）",
        "一致性由单测 test_examples_source_sync 守住。",
        "",
        "用户 2026-09-20：**示例块只保留新 20 条**；部署用文件替换，**任何情况下不回退旧句** ——",
        "所以旧示例句在这份代码里一个字都不存在。",
    ]
    q = chr(34) * 3
    lines = [q] + doc + [q]
    lines += [
        "from __future__ import annotations",
        "",
        "#: 头部（草案 §A：删掉了旧的两条「规则A/规则B」—— 规则B 已证伪，规则A 是错误示例）",
        f"HEADER = {header!r}",
        "",
        "#: 20 条示例（草案 §B），结构同数据目录的 example_dialogs.json",
        "ENTRIES: list[dict] = [",
    ]
    for e in entries:
        lines.append(f"    {e!r},")
    lines.append("]")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--specs", default=str(DEFAULT_SPECS))
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    specs = Path(args.specs)
    if not specs.is_dir():
        print(f"specs 目录不存在：{specs}", file=sys.stderr)
        return 2
    header, entries = build(specs)
    rendered = render(header, entries)
    if args.check:
        cur = OUT.read_text(encoding="utf-8")
        if cur != rendered:
            print("examples_default.py 与草案不一致，重跑 tools/gen_examples_default.py")
            for line in list(difflib.unified_diff(cur.splitlines(), rendered.splitlines(),
                                                  "committed", "from_drafts", lineterm=""))[:30]:
                print("   ", line)
            return 1
        print("examples_default.py 与草案一致")
        return 0
    OUT.write_text(rendered, encoding="utf-8")
    print("written:", OUT)
    spec = importlib.util.spec_from_file_location("examples_default", OUT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if mod.HEADER != header or mod.ENTRIES != entries:
        raise SystemExit("回读不一致")
    print("  HEADER", header)
    print("  条目", len(mod.ENTRIES), "条；带 emote 标记", n_emote_ok(mod.ENTRIES))
    return 0


def n_emote_ok(entries) -> int:
    return sum(1 for e in entries if "[emote:" in json.dumps(e, ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())
