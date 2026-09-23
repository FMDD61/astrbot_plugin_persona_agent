"""人格段落的**框架**（C7/C10）—— 只有段结构、段首标题与中性占位。

🔴 **真实文案不在本仓库**（仓库是 public，只保留框架 —— 2026-09-23 脱敏）。

真身在**仓库外**的 `data_out/persona/<sid>.md`，部署时 `scp` 到
`<plugin_data>/persona/`。加载顺序见 `style_profile.persona_section_texts()`：

    <data_dir>/persona/<sid>.md 有内容   → 用它（部署形态）
    没有文件 / 文件为空                   → 用本文件的中性占位（**框架态**）

⚠️ 框架态**必须留痕**（本项目的病根就是静默降级）：

* `StyleProfile.persona_manifest()["placeholder"]` 列出仍在用中性占位的段；
* 启动自检 `[persona] ⚠️ …` 会点名；
* pipeline trace 写 `persona_placeholder`。

⚠️ 例外：`s8_rules` 是**框架协议**（`[r]` 引用打标由 `text_style.py` 解析），
不含任何具体信息，因此保留在代码里，不参与"框架态"判定。
"""
from __future__ import annotations

from typing import Mapping

#: 中性占位标记 —— 一段文案里出现它，就说明该段**没加载到真实数据**
PLACEHOLDER_MARK = "【框架占位】"

#: 段 id -> 段首【标题】。**这是框架**（装配顺序与三段视图按它对齐），不是文案。
#: 改这里必须同步改 = 单测 test_persona_sections 的段序断言。
SECTION_MARKERS: dict[str, str] = {
    's1_who': '我是谁',
    's2_goal': '我在群里想做什么',
    's3_memory': '历史群聊摘要',
    's4_world': '我待的这个地方',
    's6_behavior': '行为反应',
    's7_style': '我怎么说话',
    's8_rules': '规则',
}

#: GATE 冻结头部独有的决策段（C22）。与 `sched` 同族：**内容可外置**成
#: `<data_dir>/persona/gate_decision.md`，缺文件时用下面的中性占位。
GATE_DECISION_SID = "gate_decision"

#: GATE 决策段的段首标题
GATE_DECISION_MARKER = "我什么时候会接话"


def _placeholder(marker: str, sid: str) -> str:
    """构造一段中性占位：段首标题（框架）+ 未加载说明（可见的降级痕迹）。"""
    return (
        f"【{marker}】\n"
        f"{PLACEHOLDER_MARK}本段文案未加载。\n"
        f"真实文案在仓库外：`data_out/persona/{sid}.md`，"
        f"部署时 scp 到 `<plugin_data>/persona/` 后重启生效。"
    )


#: 段 id -> 中性占位文案。**这里不放任何真实人格文案**（2026-09-23 起）。
SECTION_TEXT: dict[str, str] = {
    # 基本角色设定
    's1_who': _placeholder(SECTION_MARKERS['s1_who'], 's1_who'),
    # 我在群里想做什么
    's2_goal': _placeholder(SECTION_MARKERS['s2_goal'], 's2_goal'),
    # 历史群聊摘要（头部；正文由记忆摘要管线注入）
    's3_memory': _placeholder(SECTION_MARKERS['s3_memory'], 's3_memory'),
    # 世界——群聊
    's4_world': _placeholder(SECTION_MARKERS['s4_world'], 's4_world'),
    # 行为反应
    's6_behavior': _placeholder(SECTION_MARKERS['s6_behavior'], 's6_behavior'),
    # 语言风格
    's7_style': _placeholder(SECTION_MARKERS['s7_style'], 's7_style'),
    # 规则（引用打标）—— **框架协议**，不是文案：`[r]` 由 text_style.py 解析。
    # 真实文案里这一段也只有这一条规则，不含任何具体信息，故不随数据外置。
    's8_rules': """【规则】
- 想引用【现在要回应的】那条，就在回复最前面写 [r]；不引用就不写。""",
}

#: 段 id -> 中文标题（日志/自检用；不进提示词，见 D26）
SECTION_TITLES: dict[str, str] = {
    's1_who': '基本角色设定',
    's2_goal': '我在群里想做什么',
    's3_memory': '历史群聊摘要（头部；正文由记忆摘要管线注入）',
    's4_world': '世界——群聊',
    's6_behavior': '行为反应',
    's7_style': '语言风格',
    's8_rules': '规则（引用打标）',
}

#: GATE 冻结头部独有决策段（C22）—— 中性占位；真身在 persona/gate_decision.md
GATE_DECISION_SECTION: str = _placeholder(GATE_DECISION_MARKER, GATE_DECISION_SID)


def is_placeholder(text: str) -> bool:
    """该段文案是否带中性占位标记（= 没加载到真实数据）。"""
    return PLACEHOLDER_MARK in (text or "")


def placeholder_sections(texts: Mapping[str, str]) -> list[str]:
    """→ 仍在用中性占位的段 id 列表（**降级必须可见**：这个列表就是仪表盘）。

    判定用**逐字相等**而不是子串：文件内容 == 内置占位 也算没加载真实文案
    （首启物化会把占位写进 `persona/*.md`，那不算"自定义"）。
    `sched` 没有内置默认（空 = 真缺段，走 manifest 的 missing），不在这里判。
    """
    out: list[str] = []
    for sid, default in SECTION_TEXT.items():
        if is_placeholder(default) and str(texts.get(sid) or "") == default:
            out.append(sid)
    if str(texts.get(GATE_DECISION_SID) or "") == GATE_DECISION_SECTION:
        out.append(GATE_DECISION_SID)
    return out
