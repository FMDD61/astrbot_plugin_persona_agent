#!/usr/bin/env python3
"""从设计文档草案抽取八段文案 → 写到**仓库外**的 `data_out/persona/`。

用法：

    python3 tools/gen_persona_sections.py            # 生成/更新仓库外的数据文件
    python3 tools/gen_persona_sections.py --check    # 只校验仓库外数据与草案一致
    python3 tools/gen_persona_sections.py --specs /path/to/docs/specs --out /path/to/data_out

🔴 **2026-09-23 脱敏后本脚本不再写仓库里的任何文件。**
仓库是 public，只保留框架（`services/persona_sections.py` = 结构 + 中性占位）；
真实文案是**数据**，落在仓库外，部署时 scp 到 `<plugin_data>/persona/`。
脚本里有一条硬闸：`--out` 落在插件仓库内 → 直接报错退出（防止有人改回去）。

产出（`<out>/persona/` 下，文件名即 `<data_dir>/persona/` 里的段名）：

    s1_who.md … s8_rules.md     七段文案（逐字，含段首【标题】）
    gate_decision.md            GATE 冻结头部独有的决策段（C22）

以及两份给工具/单测用的清单：

    <out>/persona_sections.json   {"sections": {sid: text}, "gate_decision": text}
    <out>/persona_frozen.json     {"sha256_12": {sid: 前 12 位}, …}（内容指纹闸）

设计约束（dev-conventions/code-quality「批量文本手术必须验证边界」）：

* 每个抽取步骤都 assert 关键锚点存在 —— 抽不到就报错，**绝不静默产出空串**；
* 草案（`docs/specs/rp_section*_draft_v1.md`）与角色卡（`rp_character_card_v1.md`）
  **逐段比对**，不一致直接失败（否则两份文案会漂移成双写）；
* 写出后**回读**并与源文本逐段相等性校验。

⚠️ `sched`（作息）**不由本脚本产出**：它的真身是旧人格文件的 `schedule` 键
   （运行期数据），部署时随 `data_out/persona/sched.md` 一起 scp。
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

#: 段 id -> 输出文件名（去掉 .md）
SECTION_FILES = (
    "s1_who", "s2_goal", "s3_memory", "s4_world", "s6_behavior", "s7_style", "s8_rules",
)
GATE_SID = "gate_decision"


#: markdown 围栏（构造而非转义：脚本本身要能在任意引号风格下编辑）
_FENCE = chr(96) * 3


def blocks(md: str) -> list[str]:
    """取出 markdown 里的围栏代码块。"""
    return re.findall(r"\n" + _FENCE + r"[a-zA-Z]*\n(.*?)\n" + _FENCE, md, re.S)


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
    """返回 {sid: 文本}（含 `__gate__`）—— 抽取 + 交叉校验。"""
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
        if re.match(r"^- (\d{2}-\d{2}|\d{4}-W|\d{4}-\d{2}|\d{4})", s):
            continue
        if s in ("这几天：", "前几周：", "前几个月：", "更早："):
            continue
        mem_lines.append(ln)
    mem_header = "\n".join(mem_lines).rstrip()
    if "记得的群里最近发生的事" not in mem_header:
        raise SystemExit("记忆段头部丢失")
    if "这几天：" in mem_header:
        raise SystemExit("记忆段层标题未剔净")

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


def _assert_outside_repo(out: Path) -> None:
    """硬闸：**绝不允许**把真实文案写回插件仓库（仓库是 public）。"""
    try:
        out.resolve().relative_to(REPO.resolve())
    except ValueError:
        return
    raise SystemExit(
        f"🔴 拒绝写入仓库内路径：{out}\n"
        f"   真实人格文案属数据，必须落在仓库外（默认 {DEFAULT_OUT}）。"
    )


def frozen(secs: dict) -> dict:
    out = {sid: hashlib.sha256(secs[sid].encode("utf-8")).hexdigest()[:12]
           for sid in SECTION_FILES}
    out["__gate__"] = hashlib.sha256(secs["__gate__"].encode("utf-8")).hexdigest()[:12]
    return out


def write_out(out: Path, secs: dict) -> list[str]:
    _assert_outside_repo(out)
    pdir = out / "persona"
    pdir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for sid in SECTION_FILES:
        p = pdir / f"{sid}.md"
        p.write_text(secs[sid].rstrip() + "\n", encoding="utf-8")
        written.append(str(p))
    p = pdir / f"{GATE_SID}.md"
    p.write_text(secs["__gate__"].rstrip() + "\n", encoding="utf-8")
    written.append(str(p))

    bundle = {
        "sections": {sid: secs[sid] for sid in SECTION_FILES},
        GATE_SID: secs["__gate__"],
        "generator": "tools/gen_persona_sections.py",
    }
    (out / "persona_sections.json").write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    written.append(str(out / "persona_sections.json"))
    fp = out / "persona_frozen.json"
    fp.write_text(json.dumps({
        "sha256_12": frozen(secs),
        "note": "由 tools/gen_persona_sections.py 产出；单测用它钉住仓库外文案未被手改",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    written.append(str(fp))
    return written


def read_out(out: Path) -> dict:
    """回读仓库外的数据（persona/*.md 为准）。"""
    pdir = out / "persona"
    secs: dict = {}
    for sid in SECTION_FILES:
        p = pdir / f"{sid}.md"
        if not p.is_file():
            raise SystemExit(f"仓库外数据缺段文件：{p}")
        secs[sid] = p.read_text(encoding="utf-8").strip()
    g = pdir / f"{GATE_SID}.md"
    if not g.is_file():
        raise SystemExit(f"仓库外数据缺段文件：{g}")
    secs["__gate__"] = g.read_text(encoding="utf-8").strip()
    return secs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--specs", default=str(DEFAULT_SPECS))
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help="仓库外的数据目录（默认 data_out/）")
    ap.add_argument("--check", action="store_true",
                    help="只校验仓库外数据与草案一致，不写文件")
    args = ap.parse_args()
    specs = Path(args.specs)
    out = Path(args.out)
    if args.check:
        # 数据不在这台机器（如台式机 `git pull` 后）→ 没有可比对的东西，不算失败
        if not (out / "persona").is_dir():
            print(f"仓库外数据目录不存在，跳过一致性校验：{out}")
            return 0
        if not specs.is_dir():
            print(f"草案目录不存在，跳过一致性校验：{specs}")
            return 0
        want = build(specs)
        have = read_out(out)
        bad = [sid for sid in list(SECTION_FILES) + ["__gate__"]
               if _norm(want[sid]) != _norm(have[sid])]
        if bad:
            print("🔴 仓库外人格文案与 docs/specs 草案不一致：", ", ".join(bad))
            print("   草案改了但没重跑生成器？→ python3 tools/gen_persona_sections.py")
            for sid in bad[:2]:
                for line in list(difflib.unified_diff(
                        have[sid].splitlines(), want[sid].splitlines(),
                        f"data_out/{sid}", f"docs/specs/{sid}", lineterm=""))[:20]:
                    print("   ", line)
            return 1
        print(f"✅ 仓库外人格文案与草案一致（{out}）")
        return 0
    if not specs.is_dir():
        print(f"specs 目录不存在：{specs}", file=sys.stderr)
        return 2
    secs = build(specs)
    for path in write_out(out, secs):
        print("written:", path)
    # 回读校验
    have = read_out(out)
    for sid in list(SECTION_FILES) + ["__gate__"]:
        if _norm(have[sid]) != _norm(secs[sid]):
            raise SystemExit(f"回读不一致: {sid}")
    for sid in SECTION_FILES:
        print(f"  {sid:14s} {len(have[sid]):5d} 字符")
    print(f"  {'__gate__':14s} {len(have['__gate__']):5d} 字符")
    print(f"指纹 sha256[:12] = {json.dumps(frozen(secs), ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
