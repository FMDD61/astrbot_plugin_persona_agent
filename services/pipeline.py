"""PersonaPipeline — 主决策/生成链路（线上 main 与离线测试台共用，防漂移）。

Spec: docs/specs/chatbox-rp-tool-dual-channel.md §4.5 + A7。

边界（2026-09-07 与用户对齐）：
  - 本模块只做"主链路"：RAG → emotion → interjection 硬闸 → GateLLM →
    KG → contexts 组装 → LLM 生成 → postprocess → SendIntent。
  - event / 发送 / 落盘 / 睡眠 / 冲突检测 / 记忆入库 / 新人入列 等副作用
    留在 main（事件适配层）；离线测试台直接调本模块 run()。
  - 纯数据进出：LLM 生成通过注入的 `generate` 回调（main 封装 event/provider），
    本模块不接触 astrbot event / context。
  - trace: 每次 run 返回 SendIntent.trace（dict），由调用方落盘 trace_log.jsonl；
    本模块无 IO（可离线单测）。
  - 时钟注入（A7④ 离线重放）：`now_utc_fn` 可选；缺省 time.time。测试台把
    虚拟时钟推到场景消息时刻，硬闸预算/冷却/RAG recency 按"当时"而非"回放时"
    决策（与线上语义一致）。main 不传 → 行为不变。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from .emotion import EmotionState
from .kg_provider import KGContext
from .interjection import (
    ACTION_REPLY,
    ACTION_SILENT,
    ACTION_TOPIC,
    TRIGGER_AT,
    TRIGGER_RAG,
    Decision,
    InterjectionManager,
)
from .style_profile import StyleProfile
from .rag_service import RagService
from .session_manager import SessionManager
from .context_buffer import ContextBuffer
from .gate import GateService, GateDecision
from .kg_provider import KGProvider
from . import text_style


@dataclass
class PipelineInput:
    """纯数据入站消息（main 从 event 提取；测试台直接构造）。"""
    group_id: str
    text: str                 # 已清洗 + 已识图描述合并
    is_at: bool
    sender_uin: str
    sender_alias: str
    umo: str = ""             # unified_msg_origin（provider 解析用；离线测试可空）


@dataclass
class SendIntent:
    """pipeline 输出——发送意图。main 按字段翻译成 event 结果。"""
    action: str               # reply / silent / topic
    text: str = ""            # 最终正文（已 postprocess、剥标记）
    quote_id: Optional[str] = None   # [r:-N] 解析出的引用消息 id
    sticker_prompt: str = ""         # 旧文生图（过渡保留）
    # 工具意图预留（第④步表情库/拍一拍启用）
    emote: Optional[str] = None      # [emote:意图] 解析结果
    poke: Optional[str] = None       # [poke:QQ] 解析结果
    silent_reason: str = ""
    trace: dict = field(default_factory=dict)


# generate 回调：main 封装 event/provider/llm_generate + probe，pipeline 只传参
# (text, contexts, emotion, temperature, sender_uin, umo_origin)
GenerateFn = Callable[
    [str, list[dict], EmotionState, Optional[float], str, Optional[str]],
    Awaitable[str],
]


class PersonaPipeline:
    """主链路编排。构造注入全部 services + 回调；run() 每消息一次。"""

    def __init__(
        self,
        *,
        style: Optional[StyleProfile] = None,
        rag: Optional[RagService] = None,
        interjection: Optional[InterjectionManager] = None,
        emotion=None,
        gate: Optional[GateService] = None,
        session_mgr: Optional[SessionManager] = None,
        kg_provider: Optional[KGProvider] = None,
        buffer: Optional[ContextBuffer] = None,
        generate: Optional[GenerateFn] = None,
        examples_block: Optional[Callable[[], str]] = None,
        # S3: 工具语法块（恒定；库空/未教学时为空串 → 不进上下文）
        tool_syntax_block: Optional[Callable[[], str]] = None,
        # S9: 关系图谱块（独立于人格 —— 它增长、人格不增长）
        relations_block: Optional[Callable[[], str]] = None,
        # S2: 「现在要回应的」块构造器。pipeline 负责拆出正文/图片/表情，
        # main 负责加说话人/时间/心情等上下文（它才知道这些）。
        turn_block: Optional[Callable[[list[str], dict], str]] = None,
        postprocess: Optional[Callable[[str], str]] = None,
        temperature_for: Optional[Callable[[str], Optional[float]]] = None,
        topic_handler: Optional[Callable[[Decision, str], Awaitable[None]]] = None,
        rag_k: int = 8,
        rag_top_n: int = 3,
        gate_recent_n: int = 15,
        debounce_sec: float = 0.5,
        max_generation_tries: int = 1,
        rag_enabled: bool = True,
        now_utc_fn: Optional[Callable[[], float]] = None,
        # S2: 推迟的会话追加。main 在收到消息时调用 `defer_session_append()`
        # 把本条挂起，pipeline 在**决策完成后、生成前**真正写入 session。
        # 为什么不在 main 里直接写：LLM 的上下文与引用编号基都必须在
        # 「本条尚未入会话」的状态下构建，否则当前消息会被说两遍
        # （session 里一次 + 「现在要回应的」块一次）。
        session_append: Optional[Callable[[str, str, str, str, str], None]] = None,
    ) -> None:
        self.style = style
        self.rag = rag
        self.interjection = interjection
        self.emotion = emotion
        self.gate = gate
        self.session_mgr = session_mgr
        self.kg_provider = kg_provider
        self.buffer = buffer
        self._generate = generate
        self._examples_block = examples_block
        self._tool_syntax_block = tool_syntax_block
        self._relations_block = relations_block
        # S2: 「现在要回应的」块构造器（可选；未接线时退回旧行为）
        self._turn_block = turn_block
        self._session_append = session_append
        # {group_id: (text, name, message_id, sender_uin)}
        self._pending_append: dict[str, tuple[str, str, str, str]] = {}
        self._postprocess = postprocess
        self._temperature_for = temperature_for
        self._topic_handler = topic_handler
        self._rag_k = int(rag_k)
        self._rag_top_n = int(rag_top_n)
        self._gate_recent_n = int(gate_recent_n)
        self._debounce_sec = float(debounce_sec)
        # A7③: RAG/BGE 总开关。0 时完全不查向量库（决策无分数、Gate 无参考、
        # KG 走退化分支）。与 KGProvider.dense_enabled 联动（main 构造时同源）。
        self.rag_enabled = bool(rag_enabled)
        # A7④: 时钟注入（离线重放按场景时刻决策；缺省真实时钟）
        self._now_utc = now_utc_fn or time.time

    def _now(self) -> float:
        return float(self._now_utc())

    # ---- S2: 推迟的会话追加 ----

    def defer_session_append(
        self, group_id: str, text: str, name: str = "",
        message_id: str = "", sender_uin: str = "",
    ) -> None:
        """挂起本条的会话写入（下一个 run() 在决策后落盘）。

        同一群同时只处理一条（main 的 `_generating` 锁），所以覆盖式单槽足够。
        """
        self._pending_append[str(group_id)] = (text, name, message_id, sender_uin)

    def _assemble_base(self, group_id: str, kg_content: str = "") -> list[dict]:
        """装配**共享前缀**（RP 与 Gate 逐字节相同）。

        顺序（2026-09-13 S4 定稿，恒定在前、易变在尾）：

            [system] 固定示例块（G14）        ← 恒定
            [user/assistant] session 全量历史  ← 只追加
            [system] KG 尾注                   ← 每轮变（RP 用；Gate 也带）

        **为什么抽出来**：Gate 此前只看 740 字符的小 prompt + 15 条窗口 ——
        它看不到人格、看不到别名块（而 system_prompt 里**已含全部 163 人的
        别名关系**，实测 157/163 命中），所以判断依据远弱于 RP。
        让两者共享同一份前缀，既提升 Gate 质量，又让网关前缀缓存被两次调用复用。
        """
        contexts = (
            self.session_mgr.get_contexts(group_id)
            if self.session_mgr is not None
            else []
        )
        # 恒定块（都进缓存前缀），按"变化频率从低到高"排：
        #   工具语法（恒定）→ 示例块（恒定）→ **关系图谱（缓慢增长）** → session
        # 🔴 S9：关系图谱（新成员入列会让它 +23 字符/次、一天 8~11 次）必须比
        # session **更靠前**，否则它一变就把 8 万 token 的 session 前缀全部作废。
        head: list[dict] = []
        ts_block = (
            self._tool_syntax_block() if self._tool_syntax_block is not None else ""
        )
        if ts_block:
            head.append({"role": "system", "content": ts_block})
        ex_block = self._examples_block() if self._examples_block is not None else ""
        if ex_block:
            head.append({"role": "system", "content": ex_block})
        rel_block = (
            self._relations_block() if self._relations_block is not None else ""
        )
        if rel_block:
            head.append({"role": "system", "content": rel_block})
        if head:
            contexts = head + contexts
        if kg_content:
            contexts.append({"role": "system", "content": kg_content})
        return contexts

    @staticmethod
    def _finalize(base: list[dict], tail: str = "") -> list[dict]:
        """把「本轮」块接在共享前缀之后（易变量集中在此，缓存序不变）。

        KG 尾注已在 base 末尾 → 本轮块插到它**前面**（保持"稳定在上、易变在下"）。
        """
        out = list(base)
        if tail:
            if out and isinstance(out[-1], dict) and out[-1].get("role") == "system":
                out.insert(-1, {"role": "system", "content": tail})
            else:
                out.append({"role": "system", "content": tail})
        return out

    def shared_context(self, group_id: str) -> list[dict]:
        """给 Gate 用的共享上下文（不含本轮块）。

        Gate 判定的是"这一条该不该接"，所以它看到的应该是**本条之前**的
        世界 —— 与 RP 的 base 完全一致。
        """
        return self._assemble_base(group_id)

    def _ensure_session_append(self, group_id: str, trace: dict) -> bool:
        """幂等落盘：每轮最多写一次。早退路径用它兜底。"""
        if getattr(self, "_run_appended", False):
            return False
        if self.flush_session_append(group_id):
            self._run_appended = True
            trace["session_appended"] = True
            return True
        return False

    def flush_all_pending_appends(self) -> int:
        """落盘**所有**群的挂起条目，返回条数。

        用途：日界轮转（02:05 cron）前必须先落盘 —— 否则上一轮挂起的条目
        会被写进**新一天**的会话，导致它出现在错误的日期归档里。
        """
        n = 0
        for gid in list(self._pending_append.keys()):
            if self.flush_session_append(gid):
                n += 1
        return n

    def flush_session_append(self, group_id: str) -> bool:
        """立即落盘挂起条目（terminate / 异常兜底）。幂等。"""
        item = self._pending_append.pop(str(group_id), None)
        if item is None or self._session_append is None:
            return False
        text, name, mid, uin = item
        try:
            self._session_append(str(group_id), text, name, mid, uin)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ run

    async def run(self, inp: PipelineInput) -> SendIntent:
        """执行一轮主链路，返回 SendIntent（含 trace）。绝不抛出：
        任何内部失败都收敛为 silent + trace 记录 fallback。"""
        import asyncio

        _now_utc = self._now()
        # 每轮重置"本条已落盘"标记 —— 使 early return 处的兜底调用不会重复落盘
        self._run_appended = False
        trace: dict = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_now_utc)),
            "ts_epoch": round(_now_utc, 3),
            "group_id": inp.group_id,
            "input": {
                "text": inp.text,
                "is_at": inp.is_at,
                "sender_uin": inp.sender_uin,
                "sender_alias": inp.sender_alias,
            },
        }

        try:
            return await self._run_inner(inp, trace)
        except Exception as e:
            trace["error"] = f"{type(e).__name__}: {e}"
            # 异常也不能丢消息：把挂起条目落盘
            self.flush_session_append(inp.group_id)
            return SendIntent(action="silent", silent_reason="pipeline error", trace=trace)

    async def _run_inner(self, inp: PipelineInput, trace: dict) -> SendIntent:
        import asyncio

        group_id = inp.group_id
        text = inp.text
        alias = inp.sender_alias or f"群友{inp.sender_uin}"

        # ---- live context (buffer may be None in offline mode) ----
        live_ctx = ""
        if self.buffer is not None:
            live_ctx = self.buffer.format_recent(max_lines=20)

        # ---- RAG（A7③：总开关 + 单次检索全量复用）----
        top_score = 0.0
        hits: list[dict] = []
        hits_all: list[dict] = []
        rag_disabled = not self.rag_enabled or self.rag is None
        if not rag_disabled:
            try:
                # 单次检索：一次查全量（top_n_final=k 拿回全部 k 条），本模块
                # 截取 topN 供决策/Gate，全量 hits_all 传给 KGProvider 做融合
                # （KG 不再自查 rag → 每轮只一次 BGE 编码 + Chroma 查询）。
                hits_all = await asyncio.to_thread(
                    self.rag.query,
                    live_ctx + ("\n" + text if text else ""),
                    k=self._rag_k,
                    now_utc=self._now(),
                    top_n_final=self._rag_k,
                )
                hits = hits_all[: self._rag_top_n]
                if hits:
                    top_score = float(hits[0].get("score", 0.0))
            except Exception as e:
                trace["rag_error"] = f"{type(e).__name__}: {e}"
        # ★ trace: 记全量命中原文（LLM/决策实际所见；A/B 评估 RAG 价值的关键，
        #   不过度截断——用户决策：看到多少记多少，规模可控）
        trace["rag_disabled"] = rag_disabled
        trace["rag"] = [
            {
                "document": (h.get("document") or "")[:300],
                "score": h.get("score"),
            }
            for h in (hits_all or [])
        ]

        # ---- emotion ----
        emotion_state = EmotionState.neutral()
        if self.emotion is not None:
            try:
                recent = (
                    self.session_mgr.recent(group_id, n=20)
                    if self.session_mgr is not None
                    else []
                )
                emotion_state = await self.emotion.query(
                    group_id,
                    recent,
                    kg_ctx=KGContext(
                        recent_messages=recent,
                        current_speaker=alias,
                        current_text=text,
                        group_id=group_id,
                    ),
                )
            except Exception as e:
                trace["emotion_error"] = f"{type(e).__name__}: {e}"
        trace["emotion"] = {
            "willingness": emotion_state.global_willingness,
            "mood": emotion_state.current_mood,
            "sticker": emotion_state.sticker_prompt,
        }
        # S0 观测：情绪 provider **内部**吞掉的失败（超时/解析/网络）。
        # 这些失败会静默回退成中性值，且与「模型正常输出中性值」在 trace 上
        # 完全无法区分 —— 2026-09-13 实测 2262 条 trace 全部 willingness=1.0/
        # mood="" 就是这个盲区藏了十几天的（真因：3s 超时 < 模型 4-8s 耗时）。
        # 故把内部失败原因显式带出来，成为可统计的降级率。
        _einfo = getattr(self.emotion, "last_error", None) if self.emotion is not None else None
        if _einfo:
            trace["emotion_degraded"] = str(_einfo)

        # ---- interjection hard gate ----
        last_msg_ts = self.buffer.last_ts() if self.buffer is not None else self._now()
        decision: Decision
        if self.interjection is not None:
            decision = self.interjection.decide(
                group_id=group_id,
                now_utc=self._now(),
                is_at_me=inp.is_at,
                sender_uin=inp.sender_uin,
                last_group_msg_ts=last_msg_ts,
                top_rag_score=top_score,
                emotion_multiplier=emotion_state.global_willingness,
            )
        else:
            # 无 interjection（离线单测缺省）：@ 必回，否则回
            decision = Decision(
                action=ACTION_REPLY,
                trigger=TRIGGER_AT if inp.is_at else TRIGGER_RAG,
                reason="no interjection (test)",
            )
        trace["hard_gate"] = {
            "action": decision.action,
            "trigger": decision.trigger,
            "reason": decision.reason,
            "score": decision.score,
            "silence_sec": decision.silence_sec,
            "cooldown_left_sec": decision.cooldown_left_sec,
        }

        # S2: 本条入会话 —— 位置是**两段式**，见下方 `_ensure_session_append`：
        #   这里的调用只覆盖"硬闸判静默"等**建上下文之前**的早退；
        #   真正的主落盘点在上下文构建之后（否则当前消息会被说两遍：
        #   session 里一次 + 「现在要回应的」块一次）。
        if decision.action == ACTION_SILENT:
            self._ensure_session_append(group_id, trace)
            return SendIntent(action="silent", silent_reason=decision.reason, trace=trace)

        # ---- GateLLM (A7; A7④ 安全阀化) ----
        # 覆盖所有候选发言（REPLY 含 @、以及 TOPIC 主动话题——冷场可能是
        # 刚吵完的冷场，主动发言易惹事，故 TOPIC 也过 conflict 判定）。
        # @ 也 gate：@ 时 reply 通常 true 但 conflict 仍判（冲突中被 @ 也不煽风）。
        if self.gate is not None:
            gate_d = GateDecision(reply=False, reason="gate error", fallback=True)
            try:
                recent = (
                    self.session_mgr.recent(group_id, n=self._gate_recent_n)
                    if self.session_mgr is not None
                    else []
                )
                gate_d = await self.gate.decide(
                    group_id,
                    recent,
                    alias,
                    text,
                    rag_hits=hits if hits else None,
                    is_at=inp.is_at,
                    # S4：共享上下文 —— Gate 与 RP 看到逐字节相同的前缀
                    # （人格 + 示例 + session + KG）。此前 Gate 只有 740 字符
                    # 小 prompt + 15 条窗口，看不到人格与别名块。
                    contexts=self.shared_context(group_id),
                )
            except Exception as e:
                trace["gate_error"] = f"{type(e).__name__}: {e}"
            trace["gate"] = gate_d.to_log(group_id, inp.sender_uin)
            # S0 观测：gate 内部降级原因（超时/坏 JSON → 保守静默）。
            # gate_log 里 fallback=true 只能说明「降级了」，看不出是超时还是模型
            # 真的判不该回 —— 这里把原因补全，成为可统计的降级率。
            _ginfo = getattr(self.gate, "last_error", None)
            if _ginfo:
                trace["gate_degraded"] = str(_ginfo)
            # 安全阀：conflict=true 强制不发言（无论 reply/action，含 @ 与 topic）
            if gate_d.conflict or not gate_d.reply:
                reason = gate_d.reason
                if gate_d.conflict:
                    reason = f"conflict: {reason}"
                return SendIntent(
                    action="silent",
                    silent_reason=f"gate: {reason}",
                    trace=trace,
                )

        if decision.action == ACTION_TOPIC:
            # TopicBank send is a main-side side effect (needs event/主动发送).
            # When a topic_handler is injected (main), call it; otherwise
            # return a topic SendIntent placeholder (offline testbed).
            if self._topic_handler is not None:
                try:
                    live_ctx = (
                        self.buffer.format_recent(max_lines=20)
                        if self.buffer is not None
                        else ""
                    )
                    await self._topic_handler(decision, live_ctx)
                except Exception as e:
                    trace["topic_error"] = f"{type(e).__name__}: {e}"
                return SendIntent(action="topic", trace=trace)
            return SendIntent(action="topic", trace=trace)

        # ---- KG（A7③：外部传入 dense hits → KG 不自查，单次检索复用）----
        kg_content = ""
        if self.kg_provider is not None:
            try:
                recent = (
                    self.session_mgr.recent(group_id, n=20)
                    if self.session_mgr is not None
                    else []
                )
                kg_result = await self.kg_provider.query(
                    KGContext(
                        recent_messages=recent,
                        current_speaker=alias,
                        current_text=text,
                        group_id=group_id,
                    ),
                    # 全量 hits（若 RAG 开且有结果）；RAG 关/空则 None →
                    # KG 内部自查 fallback 或走退化分支（dense_enabled 由 main 同源设置）。
                    external_dense_hits=hits_all if hits_all else None,
                )
                kg_content = kg_result.content if kg_result else ""
            except Exception as e:
                trace["kg_error"] = f"{type(e).__name__}: {e}"
        trace["kg_tail"] = kg_content[:400]

        # ---- 上下文装配（S4：RP 与 Gate **共用同一份**）----
        base_contexts = self._assemble_base(group_id, kg_content)

        # ---- S2 输入打包重划：把「该回哪句」显式标注出来 ----
        # 实测依据：决策窗口（96 条/1h）的文本有 **59% 已在 session 里**，
        # 按 spec §4.2 再注入一遍是重复付费。所以**不加输入**，改为给已有
        # 内容划界 + 把当前轮单独抬出来：
        #
        #   【现在要回应的】当前说话人 + 本条消息 + 图片/表情（直投式）
        #   【当下】      时间 + 心情（volatile_line，逐轮变）
        #   【可参考旧话】RAG/KG 命中（风格锚定，不是"要回的内容"）
        #
        # KG 尾注本来就在 contexts 末尾（每轮变），这里把"当下"和"当前轮"
        # 都排在它**前面**，保持「稳定在上、易变在下」的缓存序不变。
        body_text, img_descs, faces = text_style.split_media_annotations(text)
        turn_lines: list[str] = []
        # 内容行直接用别名（不带 QQ 号）：块头已有「发话人：X」，
        # 再带一次号码是重复且对"怎么回这句话"没有帮助。
        turn_lines.append(f"{alias}：{body_text}" if body_text.strip()
                          else f"{alias}：（只发了媒体，没有说话）")
        if faces:
            turn_lines.append(f"［表情］{'、'.join(faces)}")
        for d in img_descs:
            turn_lines.append(
                "［图片］看不清内容（识图失败）" if "无法识别" in d else f"［图片］{d}"
            )
        trace["turn_block"] = {
            "body_chars": len(body_text),
            "images": len(img_descs),
            "faces": len(faces),
        }
        ctx_tail = None
        if self._turn_block is not None:
            try:
                ctx_tail = self._turn_block(turn_lines, {
                    "sender_uin": inp.sender_uin,
                    "sender_alias": alias,
                    "is_at": inp.is_at,
                    "body_text": body_text,
                })
            except Exception as e:
                trace["turn_block_error"] = f"{type(e).__name__}: {e}"
        contexts = self._finalize(base_contexts, ctx_tail)

        trace["session"] = {
            "size": len(contexts),
            "chars": sum(len(c.get("content", "")) for c in contexts),
        }
        # ★ S2 主落盘点：上下文已按"本条尚未入会话"构建完毕，现在把本条写进
        #   session。位置受三条约束：
        #     ① 在**上下文构建之后** —— 否则当前消息会被说两遍
        #        （session 里一次 + 「现在要回应的」块一次）；
        #     ② 在**引用快照之前** —— 编号基必须与 LLM 所见严格一致（B-001）；
        #     ③ 在**所有 early return 之前** —— sleep/静默/冲突的消息也要记录
        #        （v3 意图："静默但照常记录"）。
        #   建上下文之前的早退由上方 `_ensure_session_append` 兜底；此处幂等。
        self._ensure_session_append(group_id, trace)
        # B-002 观测：本会话被丢弃的空 content 条目（>0 = 数据曾损坏，已自愈）
        if self.session_mgr is not None and hasattr(self.session_mgr, "dropped_empty"):
            try:
                _de = self.session_mgr.dropped_empty(group_id)
                if _de:
                    trace["session_empty_dropped"] = int(_de)
            except Exception:
                pass

        # B-001: 冻结 ``[r:-N]`` 的编号基 —— 必须在建上下文之后、生成之前。
        # 生成窗口内新到的消息从此与本轮引用解析无关（旧实现对实时 buffer
        # 求值，窗口内每进 1 条就整体偏移 1 位，实测错位率 1/3–1/2）。
        quote_index = None
        if self.session_mgr is not None and hasattr(self.session_mgr, "quote_snapshot"):
            try:
                quote_index = self.session_mgr.quote_snapshot(group_id)
            except Exception as e:
                trace["quote_snapshot_error"] = f"{type(e).__name__}: {e}"

        # ---- debounce + generate ----
        await asyncio.sleep(self._debounce_sec)
        if self._generate is None:
            trace["error"] = "no generate callback"
            return SendIntent(action="silent", silent_reason="no generate", trace=trace)

        temperature = (
            self._temperature_for(decision.trigger)
            if self._temperature_for is not None
            else None
        )
        trace["temperature"] = temperature
        reply_text = await self._generate(
            text, contexts, emotion_state, temperature, inp.sender_uin, inp.umo or None
        )
        if not reply_text:
            return SendIntent(
                action="silent",
                silent_reason="empty generation",
                trace=trace,
            )
        trace["raw_generation"] = reply_text[:500]

        # ---- quote extract (BEFORE postprocess: postprocess strips [r:-N]) ----
        # B-001: 对**生成前冻结的** QuoteIndex 求值（编号基 = LLM 当时所见），
        # 而不是对实时 buffer —— 后者会因生成窗口内新到的消息整体偏移。
        quote_n: Optional[int] = None
        quote_id: Optional[str] = None
        quote_alias = ""
        quote_uin = ""
        quote_basis: Optional[int] = None
        body_text = reply_text
        try:
            body_text_no_q, qn = text_style.extract_quote(reply_text)
            quote_n = qn
            if qn is not None:
                if quote_index is not None:
                    quote_basis = len(quote_index)
                    hit = quote_index.resolve(qn)
                    if hit is not None:
                        quote_id = hit.message_id
                        quote_alias = hit.alias
                        quote_uin = hit.sender_uin
                elif self.buffer is not None:
                    # 退化：session 未接线（离线单测 / 无 session 场景）
                    quote_id = self.buffer.quote_target(qn)
            # ★ 无论是否解析出目标，标记都必须剥离 —— 泄漏到群里就是乱码。
            #   （此前只在 quote_id 非空时剥离，解析失败会把 [r:-N] 原样发出；
            #   实测线上真的发生过，见 data_out/snowluma_quote_chain_verified_with_bug.md）
            if quote_n is not None:
                body_text = body_text_no_q
        except Exception as e:
            trace["quote_error"] = f"{type(e).__name__}: {e}"

        # B-001 审计字段：此前 trace 没有 quote 字段，导致 2026-09-13 那次错位
        # 排查只能靠人工比对 SnowLuma 日志。有了这几列即可直读 + 统计错位率。
        if quote_n is not None:
            trace["quote_n"] = quote_n
            trace["quote_id"] = quote_id
            trace["quote_resolved"] = bool(quote_id)
            trace["quote_basis"] = quote_basis
            if quote_id:
                trace["quote_target_alias"] = quote_alias
                trace["quote_target_uin"] = quote_uin
            else:
                # 解不出目标 → 标记为未解析（标记被剥离、正文照发）
                trace["quote_target_missing"] = True

        # ---- 工具意图（S3：表情 / 戳人）----
        # 必须与 [r:-N] 同理，在 **postprocess 之前** 提取 —— postprocess 会把
        # 工具标记一并剥离（S0 预置的防泄漏规则），之后再取就没了。
        emote_intent: Optional[str] = None
        poke_target: Optional[str] = None
        try:
            body_text, emote_intent, poke_target = text_style.extract_tool_intents(body_text)
        except Exception as e:
            trace["tool_intent_error"] = f"{type(e).__name__}: {e}"
        if emote_intent or poke_target:
            trace["tool_intents"] = {"emote": emote_intent, "poke": poke_target}

        # ---- postprocess on the body (after quote marker removed) ----
        clean_text = body_text
        if self._postprocess is not None:
            clean_text = self._postprocess(body_text)
        trace["final_text"] = clean_text[:500]

        return SendIntent(
            action="reply",
            text=clean_text,
            quote_id=quote_id,
            sticker_prompt=emotion_state.sticker_prompt,
            # S3：动作意图交给 main 旁路执行（pipeline 不做发送）
            emote=emote_intent,
            poke=poke_target,
            trace=trace,
        )
