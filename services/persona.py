"""八段人格装配（C7 / C10）—— **纯逻辑**，不做 IO。

背景（`BUGS.md` B-037）：旧装配把 `system_prompt_fragments.json` 的键硬编码成
一个 7 元组（`style_profile.py:181`），2026-07-26 的 v0.4 整份重写之后**键元组没跟着改**
→ 22 键里只有 6 键（910 / 6145 字符）进过 prompt，其余 14 键被静默丢弃。
本模块把"八段 → 键 → 装配顺序"改成**声明式注册表**，装配结果可枚举、可留痕。

设计依据（`docs/specs/prompt_v2_rp.md`）：

| 段 | id | 载体 | 说明 |
|---|---|---|---|
| §1 基本角色设定 | `s1_who` | 冻结头部 | D8：只写"我是谁"，不写防御性规则 |
| 作息 | `sched` | 冻结头部 | **无内置默认**：沿用 `system_prompt_fragments.json` 的 `schedule` 键 |
| §2 Goal | `s2_goal` | 冻结头部 | D25：只从风格源的真实行为提炼 |
| §3 记忆 | `s3_memory` | 冻结头部（D1） | 头部恒定 + **动态层**（轮转时冻结，§4.2 各层不重叠） |
| §4 世界 | `s4_world` | 冻结头部 | 群介绍；群规不在这里（D27/D31 → Gate） |
| §5.1 角色图谱 | —— | 独立块 | 仍走 `member_relations.json`（`relations_block()`），不进本表 |
| §5.2 状态 | —— | 会话尾本轮块 | `volatile_line()` + PHI（逐轮变，不进冻结头） |
| §6 行为反应 | `s6_behavior` | 冻结头部 | D28：只决定"说什么"，不写"怎么说" |
| §7 语言风格 | `s7_style` | 冻结头部 + 示例块（D29） | |
| §8 允许行为 | `s8_rules` | 冻结头部 + 工具语法块 | 工具语法块（`[emote:]`/`[poke:]`）单独装配，不进本表（R1） |

**三段不同的装配视图**（同一批段，不同 order —— D26：段与视图的对应关系不进文案层）：

* `RP_ORDER`      —— RP 角色卡（§1+作息+§2+§3+§4+§6+§7+§8）
* `GATE_ORDER`    —— Gate 冻结头部（§1+§2+§4 + `GATE_DECISION_SECTION`，C22/D27/D31/D32）
* `SUMMARY_ORDER` —— 总结类调用的身份（§1+§2，C32/summary_v2 §4）

⚠️ Gate 头部与 RP 头部**从第一个字节起就不同**（Gate 在 §4 之后插自己的决策段）→
`_assemble_base()` 里"RP 与 Gate 逐字节相同、前缀缓存被两次调用复用"那句注释
**从 C22 起不成立**（`prompt_v2_gate.md` §2）。
"""
from __future__ import annotations

from typing import Iterable, Mapping, Optional, Sequence

from .persona_sections import GATE_DECISION_SECTION, SECTION_TEXT, SECTION_TITLES

#: RP 角色卡段序（冻结头部）
RP_ORDER: tuple[str, ...] = (
    "s1_who",
    "sched",
    "s2_goal",
    "s3_memory",
    "s4_world",
    "s6_behavior",
    "s7_style",
    "s8_rules",
)

#: Gate 冻结头部段序（C22）：§1+§2+§4，决策段由 :func:`gate_head` 追加
GATE_ORDER: tuple[str, ...] = ("s1_who", "s2_goal", "s4_world")

#: 总结类调用（日记/周/月/年）的身份段：§1+§2（C32）
SUMMARY_ORDER: tuple[str, ...] = ("s1_who", "s2_goal")

#: §3 的层：(layers 键, 显示标题)。顺序即渲染顺序；空层整段不出现（§4.4）
MEMORY_LAYERS: tuple[tuple[str, str], ...] = (
    ("recent_days", "这几天"),
    ("recent_weeks", "前几周"),
    ("recent_months", "前几个月"),
    ("older", "更早"),
)

#: 全部段 id（含无内置默认的 `sched`）
ALL_SECTIONS: tuple[str, ...] = tuple(SECTION_TEXT.keys()) + ("sched",)

#: **内容为空时按设计整段省略**的段（不算降级）。
#: §4.4：没有内容的层整段不出现 —— 首月只有日层时不留空标题；全空时 §3 整段省略。
#: 所以「§3 没进提示词」在 C4 落地前是**正常态**，不能当成段丢失告警
#: （否则降级信号恒亮 = 没有信号）。
OPTIONAL_WHEN_EMPTY: tuple[str, ...] = ("s3_memory",)


def section_title(sid: str) -> str:
    """段的中文标题（**只用于日志/自检，不进提示词** —— D26）。"""
    if sid == "sched":
        return "作息（沿用 system_prompt_fragments.json 的 schedule 键）"
    return SECTION_TITLES.get(sid, sid)


def render_memory_block(layers: Optional[Mapping[str, Iterable[str]]]) -> str:
    """把各层的一句话摘要渲染成 §3 的动态正文。

    ```
    这几天：
    - 09-17 成员戊拔智齿前紧张到肚子疼，大家轮流摸摸；成员乙饿得嗷嗷叫
    前几周：
    - 2026-W37 ……
    ```

    * **没有内容的层整段不出现**（§4.4）—— 首月只有日层时不留空标题；
    * 全空 → 返回空串 → 调用方**整段省略 §3**（而不是留一个空壳标题）；
    * 行内不做任何截断/加工（截断是生成侧的责任，见 `summary_v2.md` §2）。
    """
    if not layers:
        return ""
    out: list[str] = []
    for key, title in MEMORY_LAYERS:
        rows = [str(x).strip() for x in (layers.get(key) or ()) if str(x).strip()]
        if not rows:
            continue
        out.append(f"{title}：")
        out.extend(f"- {r}" for r in rows)
    return "\n".join(out)


def compose(
    texts: Mapping[str, str],
    order: Sequence[str],
    *,
    memory_block: str = "",
) -> tuple[str, tuple[str, ...]]:
    """按 `order` 拼装冻结文本。

    返回 `(文本, 实际参与的段 id)` —— 第二个返回值是**装配留痕**：
    段缺失（空文案 / 无默认且数据目录未提供）必须可枚举，否则就是又一个静默丢弃。

    `s3_memory` 特例：`memory_block` 为空时**整段不出现**（内容全空时不该留标题）。
    """
    parts: list[str] = []
    used: list[str] = []
    for sid in order:
        body = str(texts.get(sid) or "").strip()
        if sid == "s3_memory":
            if not memory_block.strip():
                continue
            body = (body + "\n\n" + memory_block.strip()).strip() if body else memory_block.strip()
        if not body:
            continue
        parts.append(body)
        used.append(sid)
    return "\n\n".join(parts), tuple(used)


def gate_head(texts: Mapping[str, str], meta: Optional[dict] = None) -> str:
    """Gate 冻结头部 = §1+§2+§4 ＋ GATE 独有决策段（C22）。

    ``meta``（可选，回传诊断信息）：``identity_empty`` = §1/§2/§4 一段都没装上；
    ``used_identity`` = 实际装上的身份段。调用方据此决定要不要退回裁判 system
    —— 只剩决策段时，Gate 就没有"我"了，而 C22 的前提正是这三段。
    """
    base, used = compose(texts, GATE_ORDER)
    if meta is not None:
        meta["identity_empty"] = not base.strip()
        meta["used_identity"] = list(used)
    parts = [p for p in (base, GATE_DECISION_SECTION.strip()) if p]
    return "\n\n".join(parts)


def summary_head(texts: Mapping[str, str]) -> str:
    """总结类 LLM 的 system = §1+§2（C32）。

    为什么不给全量人格（`summary_v2.md` §4）：① §6/§7/§8 是"怎么说话"，
    与"记什么"无关；② D30 —— 提示词措辞会被模仿，聊天腔会让总结层层累积；
    ③ 工具标记（`[emote:]`）有被照抄进日记正文的风险。
    """
    text, _used = compose(texts, SUMMARY_ORDER)
    return text


def manifest(
    texts: Mapping[str, str],
    order: Sequence[str] = RP_ORDER,
    *,
    memory_block: str = "",
    assembled: str = "",
) -> dict:
    """装配清单（供启动自检 / trace 用，降级必须可见）。

    🔴 **omitted 与 missing 必须分开**（独立核验 B1）：
      * ``omitted`` —— **按设计省略**（§3 在没有记忆正文时整段不出现，§4.4）；
      * ``missing`` —— **该有却没有**（段文案为空，且不属于「空则省略」那一类）。

    两者混成一个 ``missing``，会让 C4 落地前的**每一轮**都带降级标记 ——
    恒亮的降级信号等于没有信号，真降级时反而没人看。
    """
    text, used = compose(texts, order, memory_block=memory_block)
    used_set = set(used)
    omitted: list[str] = []
    missing: list[str] = []
    for sid in order:
        if sid in used_set:
            continue
        (omitted if sid in OPTIONAL_WHEN_EMPTY else missing).append(sid)
    return {
        "order": list(order),
        "used": list(used),
        "omitted": omitted,
        "missing": missing,
        "chars": {sid: len(str(texts.get(sid) or "")) for sid in order},
        # 装配后的**真实长度**（段间以空行相接、去尾空白），
        # 与 sum(chars.values()) **不是同一个量** —— 日志只报这个，
        # 免得启动日志与自检的 sp_len 对不上（独立核验 N8）。
        "assembled_chars": len(assembled) if assembled else len(text),
    }
