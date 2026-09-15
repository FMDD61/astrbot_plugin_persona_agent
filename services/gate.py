"""GateService — 独立"要不要回"决策层（双层 LLM 的上层闸）。

Spec: docs/specs/chatbox-rp-tool-dual-channel.md §4.1。

职责：
  - 仅在规则硬闸（interjection 的 RAG 候选触发）之后、RP 生成之前被调用，
    对"这一句值不值得接"做中性分析师判断（非角色视角）。
  - 输出结构化 {reply: yes/no, reason}，只回/不回 + 理由；
    不输出生成性文本、不带表情/动作指令（表达留给 RP 层）。
  - 同群决策节流：`decide_cooldown_sec` 窗口内只决策一次，窗口内复用结果
    （连续消息不反复调 LLM）。
  - 任何失败（超时/网络/解析/LLM 异常）→ 保守静默（reply=False, fallback=True），
    由调用方记入 trace/decision log。绝不抛出。

与 emotion.py 同构：llm_fn 由宿主注入（纯逻辑，无 astrbot import，可单测）。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

# 中性分析师系统提示：判断"这句话值不值得接"，不代入角色。
# A7④ 安全阀化：同时判 conflict（是否正在冲突/口角——冲突中机器人绝不发言，
# 避免煽风点火）。单 prompt 双任务：reply（参与度）+ conflict（安全闸）。
GATE_SYSTEM_PROMPT = (
    "你是一个 QQ 群聊参与度与安全分析师。你的任务（两个独立判断）：\n"
    "A) 在当前对话语境下，机器人是否应该接这句话；\n"
    "B) 当前对话是否正在发生冲突。\n"
    "\n"
    "【A. 要不要接】判断依据（按权重）：\n"
    "1. 这句话是否在向机器人提问、点名、寻求回应（@ 了机器人时通常应回）；\n"
    "2. 话题是否新鲜、有可接的空间（不是纯寒暄/已结束话题/两人私聊式对话）；\n"
    "3. 是否有情绪需求（求安慰/分享欲）值得回应；\n"
    "4. 参考风格片段：机器人（模仿某群友风格）能不能自然接上这句话；\n"
    "5. 不要因为消息多就想回，克制优先：拿不准就选择不回。\n"
    "\n"
    "【B. 是否冲突】冲突 = 真实的恶意对抗，例如：\n"
    "- 人身攻击、辱骂、诅咒、威胁；\n"
    "- 群体对立（以地域/性别/圈子/立场划分阵营互相攻击）；\n"
    "- 阴阳怪气持续升级、公开挂人、翻旧账对线。\n"
    "**不算冲突**（这些是正常群聊，不要误判）：\n"
    "- 玩笑互怼 / 嘴炮约战（如「你个菜鸡」「solo 啊」「输了叫爸爸」）——"
    "双方明显在乐、有来有回、无真实恶意；\n"
    "- 观点争论、技术辩论、吐槽抱怨（即使语气冲）；\n"
    "- 单方面情绪发泄但不指向具体人。\n"
    "判据：**看有没有真实的恶意与伤害意图**，而不是看语气强不强、用词糙不糙。\n"
    "拿不准时按冲突处理（安全优先）——但玩笑互怼要明确判为非冲突。\n"
    "\n"
    "冲突规则（安全优先）：\n"
    "- 若正在发生冲突（B 为 true），机器人绝不发言（reply 强制 false）——"
    "避免煽风点火；\n"
    "- 冲突判定从严：疑似真实冲突即 true。\n"
    "只输出一个 JSON 对象，不要输出其他内容：\n"
    '{"reply": true或false, "conflict": true或false, "reason": "一句话理由（不超过20字）"}'
)


# S4：共享上下文模式下的**判定指令**（拼在末尾那条 user 消息里）。
#
# ⚠️ 这里的"判断维度约束"不是可选项 —— 2026-09-13 实测抓到 Gate **自行引入
# 未声明的拒答维度**：它把「你怎么知道我昨晚只靠郊狼的环就把自己电🐍了」
# 判为「内容涉性暗示，不宜回应」。该结论本身无害（那条确实没 @ 机器人），
# 但同类判断作用于**该回的消息**时会变成静默误杀 —— 而这是本轮唯一
# "会悄悄降低回复质量"的路径。故在此显式收紧：只判两件事，不得引入内容审查。
GATE_JUDGE_INSTRUCTION = (
    "请只判断两件事，**不要引入任何其他维度**：\n"
    "A) 机器人**现在接这句话合不合适**（值不值得接）；\n"
    "B) 当前对话**是否正在发生真实冲突**。\n"
    "\n"
    "关于 A（值不值得接）：\n"
    "- 看：是否在向机器人提问/点名/寻求回应；话题是否新鲜、有可接的空间；\n"
    "  是否有情绪需求（求安慰/分享欲）；机器人（模仿某群友）能不能自然接上。\n"
    "- **克制优先**：拿不准就选不回。\n"
    "\n"
    "关于 B（是否冲突）：冲突 = 真实的恶意对抗（人身攻击、辱骂、威胁、群体对立、\n"
    "阴阳怪气持续升级、公开挂人）。\n"
    "**不算冲突**：玩笑互怼/嘴炮约战（双方在乐、无真实恶意）、观点争论、技术辩论、\n"
    "吐槽抱怨（哪怕语气冲）、单方面情绪发泄但不指向具体人。\n"
    "判据是**有没有真实的恶意与伤害意图**，不看语气强不强、用词糙不糙。\n"
    "冲突时 reply 强制 false（避免煽风点火）。拿不准时按冲突处理，但玩笑互怼要判非冲突。\n"
    "\n"
    "⚠️ **不评判话题本身的内容与尺度**：群友聊什么、用词荤素、玩什么梗，都不是\n"
    "「该不该接」的理由。判断依据只有上面 A/B 两条 —— 由人格设定决定这个「人」\n"
    "会接什么话，不由你做内容审查。\n"
    "\n"
    "只输出一个 JSON 对象，不要输出其他内容：\n"
    '{"reply": true或false, "conflict": true或false, "reason": "一句话理由（不超过20字）"}'
)


@dataclass
class GateDecision:
    reply: bool            # True=放行 RP 生成；False=静默
    reason: str            # 理由（LLM 给的，或 fallback 说明）
    conflict: bool = False # A7④ 安全阀：True=正在冲突，强制不发言（勿煽风点火）
    fallback: bool = False  # True=本次是降级结果（超时/解析失败等）
    cached: bool = False    # True=命中同群节流窗口缓存
    ts: float = 0.0

    def to_log(self, group_id: str, sender_uin: str = "") -> dict:
        return {
            "reply": self.reply,
            "conflict": self.conflict,
            "reason": self.reason,
            "fallback": self.fallback,
            "cached": self.cached,
            "group_id": str(group_id or ""),
            "sender_uin": str(sender_uin or ""),
            # S4 观测补漏：此前 gate_log **没有时间戳**（ts 只在 trace 里），
            # 导致无法统计"决策到达率/缓存命中率随时间的变化"——实测排查时
            # 只能靠外部监视器估算窗口。这里补上 UTC ISO + epoch。
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.ts or time.time())),
            "ts_epoch": round(float(self.ts or time.time()), 3),
        }


class GateService:
    def __init__(
        self,
        # S4: llm_fn(prompt) 或 llm_fn(None, messages=[...]) —— 两种调用形态
        llm_fn: Callable[..., Awaitable[str]],
        *,
        timeout: float = 3.0,
        decide_cooldown_sec: float = 8.0,
        recent_n: int = 15,
        max_rag_hits: int = 3,
        system_prompt: str = GATE_SYSTEM_PROMPT,
        shared_system_prompt: str = "",
        now_utc_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self._llm_fn = llm_fn
        self._timeout = float(timeout)
        self._cooldown = float(decide_cooldown_sec)
        self._recent_n = int(recent_n)
        self._max_rag_hits = int(max_rag_hits)
        self._system_prompt = system_prompt
        # S4：共享上下文模式的 system prompt（= RP 的人格提示词，含 163 人别名块）。
        # 为空则退回旧单条模式的 GATE_SYSTEM_PROMPT —— 不因缺配置而失效。
        self._shared_system_prompt = shared_system_prompt or system_prompt
        self._lock = threading.Lock()
        # per-group decision cache for cooldown-window reuse
        self._cache: dict[str, tuple[float, GateDecision]] = {}
        # A7④: 时钟注入（离线重放按场景时刻推进节流窗口；缺省真实时钟）
        self._now = now_utc_fn or time.time
        # S0 观测：最近一次决策的降级原因（None = 正常）。失败会静默降级为
        # 「不发言」，与模型的正常 no-reply 判定无法区分 → 必须显式带出。
        self.last_error: Optional[str] = None
        self.stats = {"ok": 0, "cached": 0, "timeout": 0, "error": 0, "parse_fail": 0}

    # ---- prompt building ----

    def _build_prompt(
        self,
        recent_msgs: list[dict],
        current_speaker: str,
        current_text: str,
        rag_hits: Optional[list[dict]] = None,
        is_at: bool = False,
    ) -> str:
        lines: list[str] = []
        for m in recent_msgs[-self._recent_n:]:
            role = m.get("role", "")
            name = m.get("name", "") or ("机器人" if role == "assistant" else "群友")
            content = (m.get("content", "") or "").strip()
            if not content:
                continue
            lines.append(f"{name}: {content}")
        prompt = "最近对话：\n" + ("\n".join(lines) if lines else "（空）")
        prompt += f"\n\n当前说话人：{current_speaker}，本条消息：{current_text}"
        # A7④: @ 提示——被 @ 通常应回（reply 倾向 true），但冲突时仍不发言。
        if is_at:
            prompt += "\n（本条消息 @ 了机器人：通常应当回复；但若正发生冲突，reply 仍必须为 false）"
        if rag_hits:
            hit_lines = []
            for h in rag_hits[: self._max_rag_hits]:
                # RagService.query returns {"id","document","metadata","score"}
                # — read document first, tolerate text/content aliases.
                txt = (
                    (h.get("document") or h.get("text") or h.get("content") or "")
                ).strip()
                score = h.get("score", "")
                if txt:
                    if isinstance(score, float):
                        hit_lines.append(f"- [{score:.2f}] {txt[:120]}")
                    else:
                        hit_lines.append(f"- {txt[:120]}")
            if hit_lines:
                prompt += "\n\n风格参考片段（机器人风格源的相似历史发言）：\n" + "\n".join(hit_lines)
        prompt += "\n\n请判断机器人是否应该接这句话，只输出 JSON。"
        return prompt

    def _build_shared_messages(
        self,
        contexts: list[dict],
        current_speaker: str,
        current_text: str,
        rag_hits: Optional[list[dict]] = None,
        is_at: bool = False,
    ) -> list[dict]:
        """共享上下文模式：前缀原样 + 末尾一条判定指令（S4）。

        前缀 = RP 的 `_assemble_base()` 输出（**不加工、不改写**）——
        逐字节相同才能让网关前缀缓存被两次调用复用。

        末尾那条 user 消息承担三件事：
          1. 给出本轮候选（说话人 + 本条消息）
          2. @ 提示（@ 了通常应回，但冲突除外）
          3. **判定维度的显式约束**（见 GATE_JUDGE_INSTRUCTION）
        """
        # 🔴 S10（2026-09-14 用户观察）：判定指令**放到最前面**，成为缓存前缀的一部分。
        #
        # 它本身约 600 字符（≈450 token），原先拼在**末尾那条 user 消息**里 ——
        # 位置在 8 万 token 的 session 之后，而后面还有 `【现在要判断的这一条】`
        # 这种逐轮变化的量跟在它后面，**每次都要重算**。
        # 移到最前 + 内容恒定 ⇒ 它进入稳定前缀，**一次付清**。
        msgs: list[dict] = [dict(m) for m in contexts if isinstance(m, dict)]
        # 🔴 判定指令的位置**不能离候选太远**（S13 实测回归，2026-09-15）
        #
        # S10 我为了"让判定指令进缓存前缀、省 token"，把它从末尾 user 消息里
        # 挪到**最前面的 system 消息**。结果**模型不再认为自己是裁判** ——
        # 它看到的是一条 system 指令 + 满屏群聊上下文，于是开始**参与聊天**：
        #     gate_degraded: "parse_failed: '笑什么呢成员亥，说出来让我也乐一乐~'"
        #     gate_degraded: "parse_failed: '湛江的话虾很新鲜吧，成员亥有口福了'"
        # 实测影响：解析失败率从 **0% 飙到 40~60%**（起点正是 S10 上线时刻
        # 09-14 14:00 UTC），244 条决策退化为"保守静默" → **直接压制发言频率**。
        #
        # 修法：仍独立成一条 system 消息（内容恒定 → 与 S4 一样能进缓存前缀），
        # 但**紧贴候选消息之前**，让"你现在的任务是判断"这件事在位置上成立。
        tail: list[str] = []
        tail.append(f"【现在要判断的这一条】{current_speaker}：{current_text}")
        if is_at:
            tail.append("（本条 @ 了机器人：通常应当回复；但若正发生冲突，reply 必须为 false）")
        if rag_hits:
            hits = []
            for h in rag_hits[: self._max_rag_hits]:
                txt = (h.get("document") or h.get("text") or h.get("content") or "").strip()
                if txt:
                    sc = h.get("score", "")
                    hits.append(f"- [{sc:.2f}] {txt[:120]}" if isinstance(sc, float)
                                else f"- {txt[:120]}")
            if hits:
                tail.append("风格参考片段（机器人风格源的相似历史发言）：\n" + "\n".join(hits))
        # 结构：共享前缀 → [system: 判定指令] → [user: 本轮候选]
        # 判定指令**恒定**，所以它自己那一段仍可被网关前缀缓存复用；
        # 而它紧贴候选，"裁判"角色在位置上成立。
        msgs.append({"role": "system", "content": GATE_JUDGE_INSTRUCTION})
        msgs.append({"role": "user", "content": "\n\n".join(tail)})
        return msgs

    # ---- parsing ----

    @staticmethod
    def _parse(text: str) -> Optional[GateDecision]:
        """Strict JSON parse -> GateDecision; None on any malformation.

        Accepts {reply, conflict?, reason}; conflict 缺省 false（向后兼容旧格式）。
        安全规则：conflict=true 时 reply 强制 false（冲突中绝不发言）。
        """
        try:
            obj = json.loads(text)
        except Exception:
            return None
        if not isinstance(obj, dict):
            return None
        reply_raw = obj.get("reply")
        if reply_raw is None:
            return None
        reply = bool(reply_raw) if isinstance(reply_raw, bool) else None
        if reply is None:
            # tolerate "true"/"false" strings / 0/1
            if isinstance(reply_raw, str):
                low = reply_raw.strip().lower()
                reply = True if low == "true" else (False if low == "false" else None)
            elif isinstance(reply_raw, int) and reply_raw in (0, 1):
                reply = bool(reply_raw)
            if reply is None:
                return None
        # conflict 解析（缺省 false；容错同 reply）
        conflict = False
        conflict_raw = obj.get("conflict")
        if conflict_raw is not None:
            if isinstance(conflict_raw, bool):
                conflict = conflict_raw
            elif isinstance(conflict_raw, str):
                low = conflict_raw.strip().lower()
                if low in ("true", "1"):
                    conflict = True
                elif low in ("false", "0"):
                    conflict = False
            elif isinstance(conflict_raw, int) and conflict_raw in (0, 1):
                conflict = bool(conflict_raw)
        # 安全规则：冲突中强制不发言
        if conflict:
            reply = False
        reason = str(obj.get("reason", "") or "").strip()
        return GateDecision(
            reply=reply,
            conflict=conflict,
            reason=reason or ("接" if reply else "不接"),
            ts=time.time(),
        )

    # ---- public ----

    async def decide(
        self,
        group_id: str,
        recent_msgs: list[dict],
        current_speaker: str,
        current_text: str,
        rag_hits: Optional[list[dict]] = None,
        is_at: bool = False,
        contexts: Optional[list[dict]] = None,
    ) -> GateDecision:
        """Return a decision; never raises (conservative silent on failure).

        Reuses the per-group cached decision within the cooldown window, so
        consecutive messages don't each trigger an LLM call.
        A7④: 缓存键含 is_at——@ 与非 @ 语境不同，不共享窗口结果（@ 时若命中
        非 @ 的 no-reply 缓存会误拦 @ 回复）。

        S4（2026-09-13）：``contexts`` 提供时走**共享上下文**模式 ——
        Gate 与 RP 看到逐字节相同的前缀（人格 + 示例 + session + KG），
        只在末尾追加本轮候选与判定指令。两个好处：
          ① 判断依据与 RP 同级（此前只有 740 字符小 prompt + 15 条窗口）
          ② 网关前缀缓存被 RP/Gate 两次调用复用（否则每次全价重发 2.8 万 token）
        ``contexts`` 为 None 时退回旧的 `_build_prompt` 单条模式（离线测试台兼容）。
        """
        import asyncio

        now = self._now()
        cache_key = f"{group_id}:{'@' if is_at else '-'}"
        with self._lock:
            hit = self._cache.get(cache_key)
            if hit and now - hit[0] < self._cooldown:
                d = hit[1]
                d.cached = True
                d.ts = now
                self.last_error = None
                self.stats["cached"] += 1
                return d
        self.last_error = None
        try:
            if contexts is not None:
                messages = self._build_shared_messages(
                    contexts, current_speaker, current_text, rag_hits, is_at=is_at)
                raw = await asyncio.wait_for(
                    self._llm_fn(None, messages=messages,
                                 system_prompt=self._shared_system_prompt),
                    timeout=self._timeout)
            else:
                prompt = self._build_prompt(recent_msgs, current_speaker, current_text,
                                            rag_hits, is_at=is_at)
                raw = await asyncio.wait_for(self._llm_fn(prompt), timeout=self._timeout)
            d = self._parse((raw or "").strip())
            if d is None:
                d = GateDecision(reply=False, reason="gate parse failed", fallback=True)
                self.last_error = f"parse_failed: {((raw or '').strip())[:120]!r}"
                self.stats["parse_fail"] += 1
            else:
                self.stats["ok"] += 1
        except Exception as e:
            d = GateDecision(reply=False, reason=f"gate error: {type(e).__name__}", fallback=True)
            # S0 观测：降级必须可见。否则「超时导致全体静默」与「模型判不该回」
            # 在 gate_log 里同样是 reply=false —— 开 gate.enabled=1 时会静默哑掉。
            self.last_error = f"{type(e).__name__}: {e}"
            self.stats["timeout" if isinstance(e, asyncio.TimeoutError) else "error"] += 1
        d.ts = now
        with self._lock:
            self._cache[cache_key] = (now, d)
        return d

    def clear_cache(self, group_id: Optional[str] = None) -> None:
        """Clear cached decisions. group_id 为 None 全清；否则清该群所有
        is_at 变体（缓存键为 "<group>:<@|->"）。"""
        with self._lock:
            if group_id is None:
                self._cache.clear()
                return
            prefix = f"{group_id}:"
            for k in [k for k in self._cache if k.startswith(prefix)]:
                del self._cache[k]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cached_groups": list(self._cache.keys()),
                "cooldown_sec": self._cooldown,
                "recent_n": self._recent_n,
                "stats": dict(self.stats),
                "last_error": self.last_error,
            }
