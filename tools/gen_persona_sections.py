#!/usr/bin/env python3
"""从设计文档草案抽取八段文案 -> 生成 services/persona_sections.py

用法：

    python3 tools/gen_persona_sections.py            # 生成（覆盖 persona_sections.py）
    python3 tools/gen_persona_sections.py --check    # 只校验生成物与草案一致（CI/单测用）
    python3 tools/gen_persona_sections.py --specs /path/to/docs/specs

设计约束（dev-conventions/code-quality「批量文本手术必须验证边界」）：

* 每个抽取步骤都 assert 关键锚点存在 —— 抽不到就报错，**绝不静默产出空串**；
* 草案（`docs/specs/rp_section*_draft_v1.md`）与角色卡（`rp_character_card_v1.md`）
  **逐段比对**，不一致直接失败（否则两份文案会漂移成双写）；
* 生成后**回读**生成物并与源文本逐段相等性校验。

⚠️ 本脚本在插件仓库内（受版本控制）；设计文档在仓库外的 `docs/specs/`。
   草案改了必须重跑本脚本，否则 `--check` 会失败（单测 `test_persona_source_sync`）。
"""
from __future__ import annotations

import argparse
import difflib
import importlib.util
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEFAULT_SPECS = REPO.parent / "docs" / "specs"
OUT = REPO / "services" / "persona_sections.py"


def blocks(md: str) -> list[str]:
    """取出 markdown 里的围栏代码块。"""
    return re.findall(r"\n```[a-zA-Z]*\n(.*?)\n```", md, re.S)


def split_by_headers(text: str) -> dict[str, str]:
    """按【X】标题切块 -> {标题: 整段（含标题）}"""
    out: dict[str, str] = {}
    for part in re.split(r"(?m)^(?=【)", text.strip()):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"【(.+?)】", part)
        if not m:
            raise SystemExit(f"段首不是【标题】：{part[:40]!r}")
        out[m.group(1)] = part
    return out


def _norm(t: str) -> str:
    return re.sub(r"\s+", "", t)


def _diff(label: str, a: str, b: str) -> None:
    print(f"🔴 {label} 草案与角色卡不一致（两份文案正在漂移）：")
    for line in difflib.unified_diff(a.splitlines(), b.splitlines(),
                                     "draft", "card", lineterm=""):
        print("   ", line)
    raise SystemExit(1)


def build(specs: Path) -> dict:
    """返回 {常量名: 文本} —— 抽取 + 交叉校验。"""
    card_md = (specs / "rp_character_card_v1.md").read_text(encoding="utf-8")
    card_blocks = blocks(card_md)
    if not card_blocks:
        raise SystemExit("rp_character_card_v1.md: 未取到代码块")
    card_block = card_blocks[0]
    for anchor in ("【我是谁】", "【可用的表达标记】"):
        if anchor not in card_block:
            raise SystemExit(f"角色卡块缺少锚点 {anchor}")
    card = split_by_headers(card_block)

    def draft(name: str) -> dict[str, str]:
        md = (specs / name).read_text(encoding="utf-8")
        allb = blocks(md)
        if not allb:
            raise SystemExit(f"{name}: 未取到代码块")
        body = next((x for x in allb if "【" in x), None)
        if body is None:
            raise SystemExit(f"{name}: 未找到含【的正文块")
        return split_by_headers(body)

    d4 = draft("rp_section4_draft_v1.md")
    d6 = draft("rp_section6_draft_v1.md")
    d7 = draft("rp_section7_draft_v1.md")
    d8 = draft("rp_section8_draft_v1.md")

    for label, a, b in (
        ("§4", d4["我待的这个地方"], card["我待的这个地方"]),
        ("§6", d6["行为反应"], card["行为反应"]),
        ("§7-a", d7["我怎么说话"], card["我怎么说话"]),
        ("§7-b", d7["贴贴的说法"], card["贴贴的说法"]),
        ("§7-c", d7["句末的表情"], card["句末的表情"]),
        ("§8", d8["规则"], card["规则"]),
    ):
        if _norm(a) != _norm(b):
            _diff(label, a, b)

    # §3 记忆段：只留头部（"这几天/前几周/…"层标题由记忆管线按"有内容才出现"渲染）
    mem_lines = []
    for ln in card["历史群聊摘要"].splitlines():
        s = ln.strip()
        if s.startswith(("- 09-17", "- 2026-W", "- 2026-08", "- 2025")):
            continue
        if s in ("这几天：", "前几周：", "前几个月：", "更早："):
            continue
        mem_lines.append(ln)
    mem_header = "\n".join(mem_lines).rstrip()
    if "记得的群里最近发生的事" not in mem_header:
        raise SystemExit("记忆段头部丢失")
    if "成员戊拔智齿" in mem_header or "这几天：" in mem_header:
        raise SystemExit("记忆段示例行/层标题未剔净")

    g_md = (specs / "gate_prompt_draft_v1.md").read_text(encoding="utf-8")
    gbody = next((x for x in blocks(g_md) if "【我什么时候会接话】" in x), None)
    if gbody is None:
        raise SystemExit("gate_prompt_draft_v1.md: 未找到 GATE 决策段")
    gate_dec = split_by_headers(gbody)["我什么时候会接话"]
    if "有人在真吵" not in gate_dec or "键政" not in gate_dec:
        raise SystemExit("GATE 决策段内容异常")

    return {
        "s1_who": card["我是谁"] + "\n\n" + card["我是个什么样的人"],
        "s2_goal": card["我在群里想做什么"],
        "s3_memory": mem_header,
        "s4_world": d4["我待的这个地方"],
        "s6_behavior": d6["行为反应"],
        "s7_style": (d7["我怎么说话"] + "\n\n" + d7["贴贴的说法"]
                     + "\n\n" + d7["句末的表情"]),
        "s8_rules": d8["规则"],
        "__gate__": gate_dec,
    }


SECTION_TITLES = {
    "s1_who": "基本角色设定",
    "s2_goal": "我在群里想做什么",
    "s3_memory": "历史群聊摘要（头部；正文由记忆摘要管线注入）",
    "s4_world": "世界——群聊",
    "s6_behavior": "行为反应",
    "s7_style": "语言风格",
    "s8_rules": "规则（引用打标）",
}


def py_literal(s: str) -> str:
    if '"""' in s:
        raise SystemExit("默认文案含三引号，需换写法")
    return '"""' + s + '"""'


def render(texts: dict) -> str:
    lines = [
        '"""人格段落文案（C7/C10）—— **由 docs/specs 的草案生成，勿手改**。',
        "",
        "生成器：tools/gen_persona_sections.py（改文案请改 docs/specs 下的草案，再重跑）",
        "一致性由单测 tests/test_persona_source_sync.py 守住（草案与生成物必须逐字相同）。",
        "来源：",
        "  * §1/§2/§3  <- docs/specs/rp_character_card_v1.md（八段拼装 v1）",
        "  * §4        <- docs/specs/rp_section4_draft_v1.md",
        "  * §6        <- docs/specs/rp_section6_draft_v1.md",
        "  * §7        <- docs/specs/rp_section7_draft_v1.md",
        "  * §8        <- docs/specs/rp_section8_draft_v1.md",
        "  * GATE 决策段 <- docs/specs/gate_prompt_draft_v1.md",
        '"""',
        "from __future__ import annotations",
        "",
        "",
        "SECTION_TEXT: dict[str, str] = {",
    ]
    for sid, title in SECTION_TITLES.items():
        lines.append(f"    # {title}")
        lines.append(f"    {sid!r}: {py_literal(texts[sid])},")
    lines.append("}")
    lines.append("")
    lines.append("#: 段 id -> 中文标题（日志/自检用；不进提示词，见 D26）")
    lines.append("SECTION_TITLES: dict[str, str] = {")
    for sid, title in SECTION_TITLES.items():
        lines.append(f"    {sid!r}: {title!r},")
    lines.append("}")
    lines.append("")
    lines.append("#: GATE 冻结头部独有决策段（C22）")
    lines.append(f"GATE_DECISION_SECTION: str = {py_literal(texts['__gate__'])}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--specs", default=str(DEFAULT_SPECS))
    ap.add_argument("--check", action="store_true",
                    help="只校验生成物与草案一致，不写文件")
    args = ap.parse_args()
    specs = Path(args.specs)
    if not specs.is_dir():
        print(f"specs 目录不存在：{specs}", file=sys.stderr)
        return 2
    texts = build(specs)
    rendered = render(texts)
    if args.check:
        current = OUT.read_text(encoding="utf-8")
        if current != rendered:
            print("🔴 services/persona_sections.py 与 docs/specs 草案不一致。")
            print("   草案改了但没重跑生成器？→ python3 tools/gen_persona_sections.py")
            for line in list(difflib.unified_diff(
                    current.splitlines(), rendered.splitlines(),
                    "committed", "from_drafts", lineterm=""))[:40]:
                print("   ", line)
            return 1
        print("✅ persona_sections.py 与草案一致")
        return 0
    OUT.write_text(rendered, encoding="utf-8")
    print("written:", OUT)
    # 回读校验
    spec = importlib.util.spec_from_file_location("persona_sections", OUT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for sid in SECTION_TITLES:
        if mod.SECTION_TEXT[sid] != texts[sid]:
            raise SystemExit(f"回读不一致: {sid}")
    if mod.GATE_DECISION_SECTION != texts["__gate__"]:
        raise SystemExit("回读不一致: GATE_DECISION_SECTION")
    for sid, v in mod.SECTION_TEXT.items():
        print(f"  {sid:14s} {len(v):5d} 字符")
    print(f"  {'GATE_DECISION':14s} {len(mod.GATE_DECISION_SECTION):5d} 字符")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
