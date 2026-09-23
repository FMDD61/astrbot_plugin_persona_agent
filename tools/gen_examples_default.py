#!/usr/bin/env python3
"""从 docs/specs/rp_examples_draft_v1.md 生成示例语料 → **仓库外** `data_out/examples.json`。

用法：

    python3 tools/gen_examples_default.py            # 生成/更新仓库外数据
    python3 tools/gen_examples_default.py --check    # 只校验仓库外数据与草案一致
    python3 tools/gen_examples_default.py --specs … --out …

🔴 **2026-09-23 脱敏后本脚本不再写仓库里的任何文件。**
示例块源自真实群聊语句 → 属**数据**，整批落在仓库外，部署时 scp 到
`<plugin_data>/example_dialogs.json`（`examples.json` 亦可，loader 两个名字都认）。
脚本里有硬闸：`--out` 落在插件仓库内 → 报错退出。

口径（用户 2026-09-20 定，脱敏后不变）：**示例块只保留新 20 条**，部署时用文件替换；
**任何情况下都不回退到旧示例句** —— 旧句（含 `口癖丙` 那一代）在代码里一个字都不存在。

产出：

    <out>/examples.json         20 条语料（结构同 `example_dialogs.json`）
    <out>/examples_frozen.json  内容/渲染指纹（单测的防漂移闸）

纪律同 gen_persona_sections.py：抽不到就报错，绝不静默产出空串。
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEFAULT_SPECS = REPO.parent / "docs" / "specs"
DEFAULT_OUT = REPO.parent / "data_out"   # 仓库外规范数据目录（gitignored）

#: markdown 围栏（构造而非转义）
_FENCE = chr(96) * 3


def blocks(md: str) -> list[str]:
    return re.findall(r"\n" + _FENCE + r"[a-zA-Z]*\n(.*?)\n" + _FENCE, md, re.S)


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


def _assert_outside_repo(out: Path) -> None:
    """硬闸：**绝不允许**把真实语料写回插件仓库（仓库是 public）。"""
    try:
        out.resolve().relative_to(REPO.resolve())
    except ValueError:
        return
    raise SystemExit(
        f"🔴 拒绝写入仓库内路径：{out}\n"
        f"   示例语料属数据，必须落在仓库外（默认 {DEFAULT_OUT}）。"
    )


def rendered_block(header: str, entries: list[dict]) -> str:
    """按 services/examples.py::_render 的口径渲染（指纹闸钉的就是它）。"""
    lines = []
    for ex in entries:
        parts = [f"{m.get('role', '').strip()}: {m.get('content', '').strip()}".strip()
                 for m in (ex.get("messages") or []) if (m.get("content") or "").strip()]
        if len(parts) >= 2:
            topic = (ex.get("topic") or "").strip()
            lines.append((f"[{topic}] " if topic else "") + " ".join(parts))
    return (header + "\n" + "\n".join(lines)) if lines else ""


def n_emote_ok(entries) -> int:
    return sum(1 for e in entries if "[emote:" in json.dumps(e, ensure_ascii=False))


def fingerprints(header: str, entries: list[dict]) -> dict:
    blob = json.dumps(entries, ensure_ascii=False, sort_keys=True)
    return {
        "header": header,
        "count": len(entries),
        "emote_count": n_emote_ok(entries),
        "entries_sha256_12": hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12],
        "rendered_sha256_12": hashlib.sha256(
            rendered_block(header, entries).encode("utf-8")).hexdigest()[:12],
        "note": "由 tools/gen_examples_default.py 产出；单测用它钉住仓库外语料未被手改",
    }


def write_out(out: Path, header: str, entries: list[dict]) -> list[str]:
    _assert_outside_repo(out)
    out.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    p = out / "examples.json"
    # 自带 HEADER：一个文件即完整语料（scp / 备份时不会丢掉头部）
    p.write_text(json.dumps({"header": header, "entries": entries},
                            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    written.append(str(p))
    fp = out / "examples_frozen.json"
    fp.write_text(json.dumps(fingerprints(header, entries), ensure_ascii=False, indent=2)
                  + "\n", encoding="utf-8")
    written.append(str(fp))
    return written


def read_out(out: Path) -> tuple[str, list[dict]]:
    p = out / "examples.json"
    if not p.is_file():
        raise SystemExit(f"仓库外数据不存在：{p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        header = str(data.get("header") or "")
        entries = data.get("entries")
        if not isinstance(entries, list):
            raise SystemExit(f"{p}: entries 不是数组")
        return header, entries
    if not isinstance(data, list):
        raise SystemExit(f"{p}: 顶层既不是数组也不是 {{header, entries}}")
    return "", data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--specs", default=str(DEFAULT_SPECS))
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help="仓库外的数据目录（默认 data_out/）")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    specs = Path(args.specs)
    out = Path(args.out)
    if args.check:
        # 数据/草案不在这台机器（如台式机 git pull 后）→ 没有可比对的东西，不算失败
        if not (out / "examples.json").is_file():
            print(f"仓库外示例数据不存在，跳过一致性校验：{out / 'examples.json'}")
            return 0
        if not specs.is_dir():
            print(f"草案目录不存在，跳过一致性校验：{specs}")
            return 0
        header, entries = build(specs)
        have_header, have = read_out(out)
        if json.dumps(have, ensure_ascii=False, sort_keys=True) != \
                json.dumps(entries, ensure_ascii=False, sort_keys=True):
            print("🔴 仓库外示例语料与 docs/specs 草案不一致")
            print("   草案改了但没重跑生成器？→ python3 tools/gen_examples_default.py")
            a = json.dumps(have, ensure_ascii=False, indent=1).splitlines()
            b = json.dumps(entries, ensure_ascii=False, indent=1).splitlines()
            for line in list(difflib.unified_diff(a, b, "data_out", "docs/specs",
                                                  lineterm=""))[:30]:
                print("   ", line)
            return 1
        if have_header and have_header != header:
            print(f"🔴 HEADER 不一致：data_out={have_header!r} 草案={header!r}")
            return 1
        print(f"✅ 仓库外示例语料与草案一致（{out}）")
        return 0
    if not specs.is_dir():
        print(f"specs 目录不存在：{specs}", file=sys.stderr)
        return 2
    header, entries = build(specs)
    for path in write_out(out, header, entries):
        print("written:", path)
    # 回读校验
    _, back = read_out(out)
    if back != entries:
        raise SystemExit("回读不一致")
    print("  HEADER", header)
    print("  条目", len(back), "条；带 emote 标记", n_emote_ok(back))
    print("  指纹", json.dumps(fingerprints(header, entries), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
