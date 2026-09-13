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

    # ------------------------------------------------------------------ run

    async def run(self, inp: PipelineInput) -> SendIntent:
        """执行一轮主链路，返回 SendIntent（含 trace）。绝不抛出：
        任何内部失败都收敛为 silent + trace 记录 fallback。"""
        import asyncio

        _now_utc = self._now()
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

        if decision.action == ACTION_SILENT:
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

        # ---- contexts assembly (session + examples + KG + speaker) ----
        contexts = (
            self.session_mgr.get_contexts(group_id)
            if self.session_mgr is not None
            else []
        )
        ex_block = self._examples_block() if self._examples_block is not None else ""
        if ex_block:
            contexts.append({"role": "system", "content": ex_block})
        if kg_content:
            contexts.append({"role": "system", "content": kg_content})

        trace["session"] = {
            "size": len(contexts),
            "chars": sum(len(c.get("content", "")) for c in contexts),
        }
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
            trace=trace,
        )
