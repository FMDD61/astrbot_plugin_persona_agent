# -*- coding: utf-8 -*-
"""FamiliarityService —— 群友熟悉度提案（S12）。

## 用户定稿的规则（2026-09-14/15）

- **只升不降**：LLM 只能提议更近的等级，降级永不产出
- **新成员入列自动**（`new`），**其余升级必须人工批准**
- **人工可绕过所有限制**：直接改 `member_relations.json`（mtime 热重载）
- **判断依据 = 日记**（不是原始聊天记录）+ **必须附旧版关系图谱**作对照
- **候选集 = 日记里出现的人**（不做量化筛选 —— 用户明确"做成定量太死板"；
  日记本身就是 LLM 做的定性筛选："这周谁在我眼里有分量"）
- **提案只在周报任务内产出**，生命周期一周，**新批覆盖旧批**（不拼接）
  ⇒ 因此 `apply N` 的序号在单批内稳定，**不需要"序号→稳定 id"翻译层**
- **描述（`notes`）与等级是两套东西**：描述给 LLM 较大自主权、不需批准、人工可改回去

## 依赖（用户自己指出的耦合）

    日记质量 → 决定谁被写进日记 → 决定谁能被提议提升

若日记总只写那几张老面孔，**新人的熟悉度永远提不上去**。提案量长期偏低时，
应回头查日记提示词，而不是怀疑本模块。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PROPOSAL_FILE = "familiarity_proposals.json"

# 等级序（越大越近）。**只有序关系有意义**，数值本身不参与任何计算。
CLOSENESS_ORDER = {"new": 0, "known": 1, "close": 2}
CLOSENESS_CN = {"new": "新人", "known": "认识", "close": "熟人"}

VALID_STATUS = ("pending", "approved", "rejected", "expired")


def closeness_rank(v: str) -> int:
    """等级的序。未知值当最低档（保守：不会误判为可升）。"""
    return CLOSENESS_ORDER.get(str(v or "").strip(), -1)


def is_upgrade(from_v: str, to_v: str) -> bool:
    """`to` 是否严格比 `from` 更近。**只升不降的唯一判据。**"""
    return closeness_rank(to_v) > closeness_rank(from_v)


@dataclass
class Proposal:
    index: int = 0                 # 展示与 apply 用（单批内稳定）
    uin: str = ""
    alias: str = ""
    from_closeness: str = ""
    to_closeness: str = ""
    reason: str = ""
    status: str = "pending"
    decided_at: str = ""

    def to_json(self) -> dict:
        return {
            "index": self.index, "uin": self.uin, "alias": self.alias,
            "from": self.from_closeness, "to": self.to_closeness,
            "reason": self.reason, "status": self.status,
            "decided_at": self.decided_at,
        }


@dataclass
class ProposalBatch:
    generated_at: str = ""
    period: str = ""               # 周标签（如 2026-W37）
    proposals: list[Proposal] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"generated_at": self.generated_at, "period": self.period,
                "proposals": [p.to_json() for p in self.proposals]}


def parse_proposals(raw: str, current: dict[str, str]) -> list[Proposal]:
    """把 LLM 输出解析成提案列表（**只留合法升级**）。

    ``current`` 是 ``{uin: 当前等级}``。四道过滤，任一不过即丢弃：
      1. uin 必须在 ``current`` 里（防幻觉出不存在的人）
      2. `to` 必须是已知等级
      3. **必须是升级**（`is_upgrade`）—— 只升不降
      4. 不能与自己相同

    容忍 JSON 围栏与前后闲话；解析失败返回空表（**绝不猜**）。
    """
    import re
    t = (raw or "").strip()
    if not t:
        return []
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    obj = None
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
            except json.JSONDecodeError:
                obj = None
    if not isinstance(obj, dict):
        return []
    items = obj.get("proposals")
    if not isinstance(items, list):
        return []
    out: list[Proposal] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        uin = str(it.get("uin") or "").strip()
        to_v = str(it.get("to") or "").strip()
        if not uin or uin not in current:
            continue
        if closeness_rank(to_v) < 0:
            continue
        from_v = current[uin]
        if not is_upgrade(from_v, to_v):
            continue
        out.append(Proposal(
            index=0, uin=uin,
            alias=str(it.get("alias") or "").strip(),
            from_closeness=from_v, to_closeness=to_v,
            reason=str(it.get("reason") or "").strip()[:80],
        ))
    for i, p in enumerate(out, 1):
        p.index = i
    return out


# ---------------------------------------------------------------- 提示词

PROPOSAL_SYSTEM = (
    "你在回顾过去一周，判断**你和哪些群友的关系比现在更近了**。\n"
    "只输出一个 JSON 对象，不要解释、不要 markdown 代码块：\n"
    '{"proposals": [{"uin": "QQ号", "alias": "称呼", "to": "known", '
    '"reason": "不超过30字"}]}\n'
    "\n"
    "规则：\n"
    "- `to` 只能是 `known` 或 `close`，且**必须比该群友当前的等级更近**\n"
    "- 只对你**确实感觉更近了**的人提议；没有就输出空数组\n"
    "- `reason` 说清「为什么更近」（一起聊过什么、他对你说过什么），不要空泛\n"
    "- **不要**提议把任何人降级；降级不会发生\n"
    "- 宁少勿多：一两个人足矣，没必要把一周里出现过的名字都提一遍"
)


def build_proposal_prompt(relations_block: str, diaries: list[dict],
                          current: dict[str, str]) -> str:
    """组装提案提示词 —— **必须附旧版关系图谱**（用户明确要求）。

    没有它，模型不知道现状，会提议已经是 close 的人。
    """
    parts: list[str] = []
    parts.append("## 旧版群友关系图谱（现状，不要重复提议已经是该等级的人）\n")
    parts.append(relations_block.strip() or "（空）")
    parts.append("\n## 本周日记\n")
    for d in diaries:
        parts.append(f"［{d.get('day')}］\n{d.get('summary')}\n")
    parts.append("\n请只对**日记里出现过的**群友判断，输出 JSON。")
    return "\n".join(parts)


# ---------------------------------------------------------------- 存储

class ProposalStore:
    """提案的读写（单文件，**新批覆盖旧批** —— 用户明确不要拼接）。"""

    def __init__(self, data_dir: str | Path) -> None:
        self._p = Path(data_dir) / PROPOSAL_FILE

    def load(self) -> ProposalBatch:
        try:
            d = json.loads(self._p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ProposalBatch()
        batch = ProposalBatch(generated_at=str(d.get("generated_at") or ""),
                              period=str(d.get("period") or ""))
        for it in (d.get("proposals") or []):
            if not isinstance(it, dict):
                continue
            batch.proposals.append(Proposal(
                index=int(it.get("index") or 0),
                uin=str(it.get("uin") or ""),
                alias=str(it.get("alias") or ""),
                from_closeness=str(it.get("from") or ""),
                to_closeness=str(it.get("to") or ""),
                reason=str(it.get("reason") or ""),
                status=str(it.get("status") or "pending"),
                decided_at=str(it.get("decided_at") or ""),
            ))
        return batch

    def save(self, batch: ProposalBatch) -> None:
        self._p.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._p.with_suffix(self._p.suffix + ".tmp")
        tmp.write_text(json.dumps(batch.to_json(), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self._p)

    def replace(self, proposals: list[Proposal], *, period: str,
                generated_at: str = "") -> ProposalBatch:
        """用新一批**整体覆盖**（不保留旧批的已决条目）。"""
        batch = ProposalBatch(
            generated_at=generated_at or time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                       time.gmtime()),
            period=period, proposals=proposals)
        self.save(batch)
        return batch


# ---------------------------------------------------------------- 决策

def decide(batch: ProposalBatch, index: int, action: str,
           current: dict[str, str]) -> tuple[bool, str, Optional[Proposal]]:
    """对第 ``index`` 条提案做 approve/reject。返回 ``(成功, 消息, 提案)``。

    设计要点（回应用户的三个提问）：
      - **序号语义**：提案列表里的 1-based 序号。单批内稳定（新批覆盖旧批，
        不拼接），所以**不需要"序号→稳定 id"翻译层**
      - **幂等**：重复批准已批的 → 返回成功但明确说"已是该状态，未重复执行"；
        **不报错**（用户重复输入是常态）
      - **非法输入**：非数字/越界/负数由调用方先行校验；这里再兜一次
      - **只升不降二次校验**：`apply` 时**重新读当前等级**再判一次 ——
        防"提案生成后、批准前，人工手改了等级"导致提案过期反而降级
    """
    if action not in ("approve", "reject"):
        return False, f"未知操作 {action!r}", None
    if index < 1 or index > len(batch.proposals):
        return False, f"序号超出范围（当前共 {len(batch.proposals)} 条）", None
    p = batch.proposals[index - 1]
    want = "approved" if action == "approve" else "rejected"
    if p.status == want:
        return True, f"#{index} 已是「{want}」，未重复执行", p
    if p.status in ("approved", "rejected"):
        return False, f"#{index} 已「{p.status}」，不能再改（如需变更请人工改配置）", p

    if action == "approve":
        # 二次校验：当前等级可能已被人工改过
        now_v = current.get(p.uin)
        if now_v is None:
            return False, f"#{index} {p.alias or p.uin} 已不在成员表里", p
        if now_v == p.to_closeness:
            # 人工已经升过了 —— 视为成功（幂等），避免"看起来失败其实已达成"
            p.status = "approved"
            p.decided_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            return True, f"#{index} {p.alias or p.uin} 已是「{p.to_closeness}」，无需重复提升", p
        if not is_upgrade(now_v, p.to_closeness):
            return False, (f"#{index} 当前等级为「{now_v}」，提升到「{p.to_closeness}」"
                           f"不是升级（提案已过期？）"), p
        p.from_closeness = now_v          # 用真实当前值，保证记录准确

    p.status = want
    p.decided_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return True, "", p


def parse_indices(arg: str, total: int) -> tuple[list[int], str]:
    """解析 `apply` 的序号参数。返回 ``(序号列表, 错误消息)``。

    用户确认的设计：**支持多个空格分隔的序号；不支持区间**（少一种语法 = 少一种误用）。
    非法输入分类报错而不是笼统"参数错误"：
      - 空 → 提示用法
      - 非数字 → 指出是哪个 token 不是数字
      - 越界/<=0 → 指出范围
    """
    arg = (arg or "").strip()
    if not arg:
        return [], "请指定序号，如 `/admin relations apply 1`"
    toks = arg.split()
    out: list[int] = []
    for tk in toks:
        if not tk.lstrip("+-").isdigit():
            return [], f"「{tk}」不是数字。用法：`/admin relations apply 1 3`"
        n = int(tk)
        if n < 1 or n > total:
            return [], f"序号 {n} 超出范围（1–{total}）"
        if n in out:
            continue                      # 重复序号静默去重，不报错
        out.append(n)
    if not out:
        return [], "没有有效序号"
    return out, ""
