"""astrbot_plugin_persona_agent — main entry.

v0.1 skeleton: registers commands and event handlers, wires the three
sub-agent C services (StyleProfile / RagService / InterjectionManager),
and implements the @ reply path. Active interjection, poke, dream, and
topic_bank are gated by their `*.enabled` (int 0/1) config and stay
silent at default.

See IMPLEMENTATION_PLAN.md (sections 1, 2, 8, 9) and DEPLOYMENT_GUIDE.md.
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools
import astrbot.api.message_components as Comp

from .services.text_style import (
    RE_QUOTE_BLOCK,
    RE_AT_MARKER,
    RE_EMOJI,
    RE_AT_USER,
    RE_PAREN_META,
    RE_REPLY_MARKER,
)
from .services import text_style
from .services.llm_params import (
    reasoning_value, resolve_provider_id, extract_reasoning,
)
from .services.style_profile import StyleProfile, plan_relations_delta
from .services.rag_service import RagService
from .services.interjection import (
    InterjectionManager,
    TRIGGER_AT,
    TRIGGER_RAG,
    TRIGGER_COLD,
    ACTION_TOPIC,
    Decision,
)
from .services.topic_bank import TopicBank
from .services.summary import SummaryService, build_prompt, append_summary
from .services.json_store import JsonStore, find_jsonl_record
from .services.context_buffer import ContextBuffer
from .services.session_manager import SessionManager
from .services.kg_provider import KGProvider, MultiSignalKGProvider
from .services.emotion import EmotionProvider, DefaultEmotionProvider, EmotionState, LLMEmotionProvider, EMOTION_SYSTEM_PROMPT
from .services.gate import GateService, GATE_SYSTEM_PROMPT
from .services.pipeline import PersonaPipeline, PipelineInput, SendIntent
from .services.vision import VisionService, face_name
from .services.poke import PokeService
from .services import protocol_compat
from .services.examples import load_examples_block, ExamplesState
from .services.memory_store import MemoryStore, MemoryEvent
from .services.dream import DreamMaker, persist_dream
from .services.familiarity import (
    CLOSENESS_CN,
    PROPOSAL_SYSTEM,
    ProposalStore,
    build_proposal_prompt,
    decide as decide_proposal,
    parse_indices,
    parse_proposals,
)
from .services.conflict_detector import ConflictDetector

class PersonaAgent(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.data_dir: Path = StarTools.get_data_dir()
        self.store = JsonStore(self.data_dir)

        # IDs (string, OneBot uses string-typed user_id in raw events)
        self.target_group_id: str = str(config.get("target_group_id", ""))
        self.test_mode: int = int(config.get("test_mode", 0))
        self.test_group_id: str = str(config.get("test_group_id", ""))
        self.bot_qq: str = str(config.get("bot_qq", ""))
        self.style_source_qq: str = str(config.get("style_source_qq", ""))
        self.privileged_qq: str = str(config.get("privileged_qq", ""))

        # Services (lazy heavy deps; created in initialize())
        self.style: Optional[StyleProfile] = None
        self.rag: Optional[RagService] = None
        self.interjection: Optional[InterjectionManager] = None
        self.buffer: Optional[ContextBuffer] = None
        self.session_mgr: Optional[SessionManager] = None
        self.kg_provider: Optional[KGProvider] = None
        self._emotion: Optional[EmotionProvider] = None
        self._gate: Optional[GateService] = None
        self._pipeline: Optional[PersonaPipeline] = None
        self._memory_store: Optional[MemoryStore] = None
        self._poke: Optional[PokeService] = None
        self._topic_bank: Optional[TopicBank] = None
        self._summary: Optional[SummaryService] = None
        self._conflict_detector: Optional[ConflictDetector] = None
        self._generating: dict[str, bool] = {}
        # A7④: 冲突通知冷却（30min，防刷屏；仅限发言闸之外的附加通知）
        self._conflict_notify_ts: float = 0.0
        # S0: provider 解析缓存 + pipeline 主链路（无 event）落 cache probe 的群号。
        # ⚠️ 必须在 __init__ 里就存在 —— initialize() 末尾会 create_task 预热，
        # 而 initialize 后半段才给这两个属性赋值，异步任务可能先跑到（实测
        # 2026-09-13 启动时报 'PersonaAgent' object has no attribute
        # '_last_provider_id'）。
        self._last_provider_id: Optional[str] = None
        self._probe_group_id: str = ""
        # S2: 「现在要回应的」块所需的逐轮状态
        self._pipeline_has_turn_block: bool = False
        self._turn_block_emotion = None
        # S3: 贴纸服务（懒建；复用 RagService 已加载的 BGE）
        self._sticker = None


    # ----------------------------------------------------------------- lifecycle

    async def initialize(self) -> None:
        logger.info(f"[persona_agent] initializing, data_dir={self.data_dir}")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._housekeeping()

        self.style = StyleProfile(self.data_dir)

        rag_cfg = self.config.get("rag", {}) or {}
        self.rag = RagService(self.data_dir)

        ij_cfg = self.config.get("interjection", {}) or {}
        topic_cfg = self.config.get("topic_bank", {}) or {}
        self.interjection = InterjectionManager(
            self.style,
            data_dir=str(self.data_dir),
            active_interjection=int(self.config.get("active_interjection", 0)),
            reply_on_at=int(self.config.get("reply_on_at", 1)),
            topic_bank_enabled=int(topic_cfg.get("enabled", 0)),
            rag_score_threshold=float(rag_cfg.get("score_threshold", 0.55)),
            cold_start_threshold_sec=float(ij_cfg.get("cold_start_threshold_sec", 600)),
            min_gap_sec=float(ij_cfg.get("min_gap_sec", 25)),
            at_cooldown_sec=float(ij_cfg.get("at_cooldown_sec", 8)),
            silence_cap_sec=float(ij_cfg.get("silence_cap_sec", 120)),
            local_tz_offset_hours=int(ij_cfg.get("local_tz_offset_hours", 8)),
        )

        cb_cfg = self.config.get("context_buffer", {}) or {}
        self.buffer = ContextBuffer(
            self.data_dir,
            max_messages=int(cb_cfg.get("max_messages", 200)),
            max_age_sec=int(cb_cfg.get("max_age_sec", 3600)),
            persist_jsonl=int(cb_cfg.get("persist_jsonl", 1)) == 1,
        )

        # v3: per-day sessions, unlimited message cap (daily rotation bounds it).
        # max_messages <= 0 means unbounded; config key kept for backward compat.
        _sess_cap = int(cb_cfg.get("session_max_messages", 0) or 0)
        self.session_mgr = SessionManager(
            data_dir=str(self.data_dir),
            max_messages=(_sess_cap if _sess_cap > 0 else None),
            rotation_hour=2,
            tz_offset_hours=8,
        )
        restored = self.session_mgr.load_all()
        if restored:
            logger.info(f"[persona_agent] restored sessions: {restored}")
        # B-014: KG 入库质量门（只出现一次的话题不建边；1 = 关闭门槛退回旧行为）
        mem_cfg = self.config.get("memory", {}) or {}
        self._memory_store = MemoryStore(
            str(self.data_dir),
            topic_min_occurrences=int(mem_cfg.get("topic_min_occurrences", 2)),
        )
        # A7③: dense_enabled 与 rag.enabled 同源（reload 时重建 KG 生效）
        rag_on = int(rag_cfg.get("enabled", 1)) == 1
        self.kg_provider = MultiSignalKGProvider(
            rag=self.rag,
            store=self._memory_store,
            k_retrieve=int(rag_cfg.get("k_retrieve", 8)),
            top_n_final=int(rag_cfg.get("top_n_final", 3)),
            max_chars=int(rag_cfg.get("max_example_chars", 400)),
            dense_enabled=rag_on,
        )
        emotion_cfg = self.config.get("emotion", {}) or {}
        if int(emotion_cfg.get("enabled", 1)) == 1:
            self._emotion = LLMEmotionProvider(
                self._emotion_llm,
                timeout=float(emotion_cfg.get("timeout_sec", 3)),
                cache_ttl=float(emotion_cfg.get("cache_ttl_sec", 30)),
            )
            logger.info("[persona_agent] LLM emotion provider enabled (v1, G10)")
        else:
            self._emotion = DefaultEmotionProvider()

        # A7: GateLLM decision layer (independent "should we reply" judge).
        # Default off (conservative); enabled only when gate.enabled=1.
        gate_cfg = self.config.get("gate", {}) or {}
        if int(gate_cfg.get("enabled", 0)) == 1:
            self._gate = GateService(
                self._gate_llm,
                timeout=float(gate_cfg.get("timeout_sec", 3.0)),
                decide_cooldown_sec=float(gate_cfg.get("decide_cooldown_sec", 8.0)),
                recent_n=int(gate_cfg.get("recent_n", 15)),
                max_rag_hits=int(gate_cfg.get("max_rag_hits", 3)),
                # 🔴 S14 修正 S4 的错误：Gate 的 system 必须是**裁判身份**，
                # 不能是 RP 人格。实测把人格当 system 会让模型"参与聊天而不是
                # 判断"（解析失败率 0%→45%，244 条决策退化为保守静默）。
                # 不传 shared_system_prompt → GateService 自动用 GATE_SYSTEM_PROMPT。
            )
            logger.info("[persona_agent] GateLLM decision layer enabled (A7/S4 共享上下文)")
        else:
            logger.info("[persona_agent] GateLLM decision layer disabled (gate.enabled=0)")

        # S0: 启动即解析一次 provider —— 让 02:05 的日记/摘要 cron 不再依赖
        # 「当天先有人触发过一轮回复」。异步、失败仅告警，不阻塞加载。
        # 放在 initialize 末尾：确保 _last_provider_id 等实例属性已就绪。
        try:
            asyncio.get_running_loop().create_task(self._warm_provider_id())
        except RuntimeError:
            pass

        # S11/S13: 做梦与"熟悉度汇报"**彻底分离**。
        # `DreamJob`（services/dream_job.py）自 S11 起已是死代码 —— cron 改挂
        # `_dream_job_runner`，而 DreamJob.run 只写不推、且它的"关系变更建议"
        # 已由 `services/familiarity.py` 取代（用户："DreamJob 是做梦，不应该
        # 负责处理熟悉度汇报相关内容"）。故整个类与实例一并移除。
        self._dream_maker = DreamMaker(str(self.data_dir),
                                       anchor_fn=self._dream_anchor_llm,
                                       dream_fn=self._dream_llm)

        dream_cfg = self.config.get("dream", {}) or {}
        if int(dream_cfg.get("enabled", 0)) == 1:
            if self.context.cron_manager is not None:
                await self.context.cron_manager.add_basic_job(
                    name="persona_dream_job",
                    cron_expression="0 3 * * 1",
                    # ⚠️ 不挂 DreamJob.run（它只写文件、从不推送）—— 挂 runner
                    handler=self._dream_job_runner,
                    description="Weekly persona style drift report via MemoryStore analysis",
                    timezone="Asia/Shanghai",
                    enabled=True,
                    persistent=False,
                )
                logger.info("[persona_agent] dream cron registered (weekly Mon 03:00 CST)")
            else:
                logger.warning("[persona_agent] cron_manager not available, dream cron NOT registered")
        else:
            logger.info("[persona_agent] dream.enabled=0, cron NOT registered")

        self._conflict_detector = ConflictDetector(
            keywords_dir=str(self.data_dir),
        )

        poke_cfg = self.config.get("poke", {}) or {}
        self._poke = PokeService(
            str(self.data_dir),
            bot_qq=self.bot_qq,
            cooldown_sec=float(poke_cfg.get("cooldown_sec", 300)),
            # ⚠️ 原为硬编码 4 —— WebUI 里调 `poke.hourly_cap` 完全不生效（死配置，
            # 与 B-010 同类）。改为读配置。
            hourly_cap=int(poke_cfg.get("hourly_cap", 4)),
            proactive_hourly_cap=int(poke_cfg.get("proactive_hourly_cap", 5)),
        )
        self._poke.proactive_enabled = int(poke_cfg.get("proactive_enabled", 0)) == 1
        self._topic_bank = TopicBank(str(self.data_dir))
        self._summary = SummaryService(str(self.data_dir))

        # v3: sleep window + diary (rotation at 02:00 inside the sleep window)
        sleep_cfg = self.config.get("sleep", {}) or {}
        self._sleep_start = int(sleep_cfg.get("start_hour", 2))
        self._sleep_end = int(sleep_cfg.get("end_hour", 7))
        diary_cfg = self.config.get("diary", {}) or {}
        self._diary_enabled = int(diary_cfg.get("enabled", 1)) == 1
        # _last_provider_id / _probe_group_id 已在 __init__ 里初始化（见那里的
        # 注释：initialize 末尾的预热任务可能早于本行执行），此处不重复赋值。
        # Test-time sleep override: "awake" / "sleep" / None(window applies);
        # in-memory only, resets on restart.
        # (类型, 小时数) 或 None；旧代码曾存字符串，故保留向后兼容的宽松判定
        self._sleep_override = None

        vision_cfg = self.config.get("vision", {}) or {}
        self._vision_enabled = int(vision_cfg.get("enabled", 1)) == 1
        self._vision_max_images = int(vision_cfg.get("max_images", 2))
        self._vision: Optional[VisionService] = None
        self._vision_resolving = False
        self._examples_state = ExamplesState()

        if self._diary_enabled and self.context.cron_manager is not None:
            await self.context.cron_manager.add_basic_job(
                name="persona_daily_rotation",
                cron_expression="5 2 * * *",
                handler=self._daily_rotation_job,
                description="Daily session rotation + diary summary (02:05 CST)",
                timezone="Asia/Shanghai",
                enabled=True,
                persistent=False,
            )
            logger.info("[persona_agent] daily rotation cron registered (02:05 CST)")
        else:
            logger.warning("[persona_agent] diary.disabled or cron_manager missing; rotation cron NOT registered")

        # G13: weekly/monthly summary pyramid over the daily diaries (default off)
        summary_cfg = self.config.get("summary", {}) or {}
        if self.context.cron_manager is not None:
            if int(summary_cfg.get("weekly_enabled", 0)) == 1:
                await self.context.cron_manager.add_basic_job(
                    name="persona_weekly_summary",
                    cron_expression="10 2 * * 1",
                    handler=self._weekly_summary_job,
                    description="Weekly summary of daily diaries + raw sampling (G13)",
                    timezone="Asia/Shanghai",
                    enabled=True,
                    persistent=False,
                )
                logger.info("[persona_agent] weekly summary cron registered (Mon 02:10 CST)")
            if int(summary_cfg.get("monthly_enabled", 0)) == 1:
                await self.context.cron_manager.add_basic_job(
                    name="persona_monthly_summary",
                    cron_expression="15 2 1 * *",
                    handler=self._monthly_summary_job,
                    description="Monthly summary of daily diaries + raw sampling (G13)",
                    timezone="Asia/Shanghai",
                    enabled=True,
                    persistent=False,
                )
                logger.info("[persona_agent] monthly summary cron registered (1st 02:15 CST)")
            if int(summary_cfg.get("yearly_enabled", 0)) == 1:
                await self.context.cron_manager.add_basic_job(
                    name="persona_yearly_summary",
                    cron_expression="20 2 1 1 *",
                    handler=self._yearly_summary_job,
                    description="Yearly summary over the 12 monthly summaries (S12)",
                    timezone="Asia/Shanghai",
                    enabled=True,
                    persistent=False,
                )
                logger.info("[persona_agent] yearly summary cron registered (Jan 1 02:20 CST)")
        else:
            logger.warning("[persona_agent] cron_manager not available; summary crons NOT registered")

        # Pre-warm RAG off the event loop so the first group message never
        # stalls on BGE/chroma lazy init (2026-08-22 watchdog incident).
        warmed = await asyncio.to_thread(self.rag.warmup)
        logger.info(f"[persona_agent] RAG warmup {'OK' if warmed else 'FAILED (will retry lazily)'}")

        # A7: assemble the shared decision/generation pipeline (main + testbed
        # run the same code path). Generate callback wraps the LLM call with
        # per-event provider resolution done in main.
        self._build_pipeline()
        logger.info("[persona_agent] pipeline ready (shared decision/generation path)")

        # S5: 启动自检 —— 专门拦"配额类参数静默失效"（本轮 hourly_budget 时区
        # 错位就是这么藏了两周：预算恒为 0.34，行为看起来只是"它不主动说话"）。
        self._startup_selfcheck()

        logger.info(
            f"[persona_agent] ready: target_group={self.target_group_id} "
            f"test_mode={self.test_mode} test_group={self.test_group_id} "
            f"bot={self.bot_qq} style_src={self.style_source_qq} "
            f"reply_on_at={self.config.get('reply_on_at')} "
            f"active_interjection={self.config.get('active_interjection')}"
        )

    async def terminate(self) -> None:
        logger.info("[persona_agent] terminating")
        # S2: 落盘任何挂起的会话条目（进程退出前的兜底）
        if self._pipeline is not None and self.session_mgr is not None:
            try:
                for gid in list(getattr(self._pipeline, "_pending_append", {}) or {}):
                    self._pipeline.flush_session_append(gid)
            except Exception as e:
                logger.warning(f"[persona_agent] pending session flush failed: {e}")
        if self.session_mgr is not None:
            try:
                self.session_mgr.save_all()
            except Exception as e:
                logger.warning(f"[persona_agent] session save on terminate failed: {e}")
        # A7③: flush vision 持久哈希缓存（脏数据落盘）
        if self._vision is not None:
            try:
                self._vision.flush()
            except Exception as e:
                logger.warning(f"[persona_agent] vision persist flush failed: {e}")

    # ----------------------------------------------------------------- helpers

    def _build_pipeline(self) -> None:
        """Assemble (or rebuild on /reload_persona_config) the shared pipeline.

        A7③: reads rag.enabled — 0 时 pipeline 不查 RAG、KG 走退化分支。
        Reload 时重建让开关即时生效（与 initialize 同一构造逻辑，防漂移）。
        """
        rag_cfg = self.config.get("rag", {}) or {}
        gate_cfg = self.config.get("gate", {}) or {}
        self._pipeline = PersonaPipeline(
            style=self.style,
            rag=self.rag,
            interjection=self.interjection,
            emotion=self._emotion,
            gate=self._gate,
            session_mgr=self.session_mgr,
            kg_provider=self.kg_provider,
            buffer=self.buffer,
            generate=self._pipeline_generate,
            examples_block=self._examples_block,
            tool_syntax_block=self._tool_syntax_block,
            relations_block=(self.style.relations_block if self.style is not None else None),
            system_prompt=(self.style.system_prompt if self.style is not None else None),
            relations_delta=self._relations_delta_block,
            postprocess=self._postprocess_plain,
            temperature_for=self._temperature_for,
            turn_block=self._build_turn_block,
            session_append=self._session_append,
            rag_k=int(rag_cfg.get("k_retrieve", 8)),
            rag_top_n=int(rag_cfg.get("top_n_final", 3)),
            gate_recent_n=int(gate_cfg.get("recent_n", 15)),
            debounce_sec=0.5,
            rag_enabled=int(rag_cfg.get("enabled", 1)) == 1,
        )
        # S2: 标记 turn_block 已接线 —— _generate_reply 据此不再重复追加
        # 说话人行与 volatile 行（它们已并入同一块）
        self._pipeline_has_turn_block = True

    def _is_target_group(self, event: AstrMessageEvent) -> bool:
        gid = event.get_group_id()
        if gid is None:
            return False
        if self.test_mode == 1:
            return str(gid) == self.test_group_id
        return str(gid) == self.target_group_id

    @staticmethod
    def _is_notice_event(event: AstrMessageEvent) -> bool:
        """True 表示这是一条 notice/request 事件而非真正的消息。

        AstrBot v4.27.4 的 aiocqhttp 适配器把带 group_id 的通知归为 GROUP_MESSAGE，
        于是群戳等通知会同时匹配 on_group_message；此处用于让消息处理器**让路**
        （不 stop_event，交给 on_notice），否则派发循环会因 is_stopped() 提前 break。
        """
        raw = getattr(event.message_obj, "raw_message", None)
        getter = getattr(raw, "get", None)
        if not callable(getter):
            return False
        post_type = getter("post_type")
        return bool(post_type) and post_type != "message"

    def _is_at_bot(self, event: AstrMessageEvent) -> bool:
        self_id = str(event.get_self_id() or self.bot_qq)
        for seg in event.get_messages():
            if isinstance(seg, Comp.At) and str(getattr(seg, "qq", "")) == self_id:
                return True
        return False

    @staticmethod
    def _clean_message_text(text: str) -> str:
        """Strip [引用消息(...)] and [At:QQ] blocks from raw message text."""
        return text_style.clean_message_text(text)

    def _log_decision(self, payload: dict) -> None:
        try:
            gid = str(payload.get("group_id") or "")
            # A7 2b: per-group log dir (logs/<group_id>/); legacy root file kept
            name = f"logs/{gid}/decision_log.jsonl" if gid else "decision_log.jsonl"
            self.store.append_jsonl(name, payload)
        except Exception as e:  # never let logging crash the handler
            logger.warning(f"[persona_agent] decision log write failed: {e}")

    def _trace_enabled(self) -> bool:
        """A7: trace_log 写开关（默认开——trace 是评估 RAG 价值的依据）。"""
        try:
            return int((self.config.get("trace") or {}).get("enabled", 1)) == 1
        except Exception:
            return True

    async def _notify_admin(self, context_summary: str, group_id: str, speaker: str) -> None:
        # A7④: 30min 冷却防刷屏（沿用旧 conflict_detector._COOLDOWN 语义；仅通知侧）
        now = time.time()
        if now - self._conflict_notify_ts < 1800:
            logger.info("[persona_agent] conflict notify suppressed by cooldown (30min)")
            return
        binding = self.store.load_json("admin_binding.json", {})
        umo = binding.get("unified_msg_origin")
        if not umo:
            logger.warning("[persona_agent] conflict detected but no admin binding; notification skipped")
            return
        msg = (
            f"[冲突警告] 群 {group_id}\n"
            f"触发者: {speaker}\n"
            f"上下文: {context_summary[:200]}"
        )
        try:
            from astrbot.api.event import MessageChain
            chain = MessageChain().message(msg)
            await self.context.send_message(umo, chain)
            self._conflict_notify_ts = now
            logger.info(f"[persona_agent] conflict notification sent to admin")
        except Exception as e:
            logger.warning(f"[persona_agent] failed to send admin notification: {e}")

    def _record_inbound(self, event: AstrMessageEvent) -> None:
        if self.buffer is None:
            return
        try:
            self.buffer.add(
                ts=time.time(),
                group_id=str(event.get_group_id() or ""),
                sender_id=str(event.get_sender_id() or ""),
                sender_name=event.get_sender_name() or "",
                text=event.message_str or "",
                message_id=str(getattr(event.message_obj, "message_id", "")),
                message_type="group",
            )
        except Exception as e:
            logger.warning(f"[persona_agent] buffer add failed: {e}")

    # ----------------------------------------------------------------- commands

    def _apply_live_config(self) -> None:
        """把配置开关应用到运行中的对象（原 `/reload_persona_config` 的逻辑）。

        ⚠️ 这个方法的**定义**曾在 S12 重构 `/admin` 时被我误删（只留了调用点）
        —— 静态检查查不出 `self.xxx` 这类属性缺失，只有真跑 `/admin reload`
        才会 AttributeError。教训：删旧方法前先 grep 它的**调用点**。

        7 个可编辑 JSON 靠 mtime 热重载，不需要在这里处理；
        这里管的是 **interjection 开关**与 **rag.enabled**（后者要重建 pipeline）。
        """
        if self.interjection is None:
            raise RuntimeError("插件尚未完成初始化")
        topic_cfg = self.config.get("topic_bank", {}) or {}
        self.interjection.update_toggles(
            active_interjection=int(self.config.get("active_interjection", 0)),
            reply_on_at=int(self.config.get("reply_on_at", 1)),
            topic_bank_enabled=int(topic_cfg.get("enabled", 0)),
        )
        try:
            rag_on = int((self.config.get("rag") or {}).get("enabled", 1)) == 1
            if self.kg_provider is not None:
                self.kg_provider.dense_enabled = rag_on
            self._build_pipeline()
        except Exception as e:
            logger.warning(f"[persona_agent] reload pipeline rebuild failed: {e}")

    def _admin_binding(self) -> dict:
        """管理员绑定（S12：**推送目标与权限来源合并为一份**）。

        用户 2026-09-14 拍板："三者是同一个 QQ，命令就可以收窄了…
        同时绑定私聊推送和管理员权限"。合并的好处：不会出现"能收到推送但
        没权限操作"的错位。

        兼容：`admin_binding.json` 缺失时回退 `dream_binding.json`
        （历史部署只绑过 dream）。

        ⚠️ 这个方法曾在 S12 重构中**整段丢失**：我删旧命令时用行号切掉了它，
        而后续补丁的锚点正是被切掉的那一行 → `str.replace` 静默无操作，
        但我看到"patched"以为成功。**结果它从未被提交**，直到用户跑 `/admin`
        才暴露 `'PersonaAgent' object has no attribute '_admin_binding'`。
        教训：补丁必须 **assert 锚点存在**（现在这么做了）；行号切割后要复查。
        """
        b = self.store.load_json("admin_binding.json", {}) or {}
        if b.get("unified_msg_origin"):
            return b
        legacy = self.store.load_json("dream_binding.json", {}) or {}
        if legacy.get("unified_msg_origin"):
            return legacy
        return b

    def _is_privileged(self, event: AstrMessageEvent) -> bool:
        """权限判定：`privileged_qq`（bootstrap）**或** admin_binding 的会话。

        bootstrap 的意义：第一个 `/admin bind` 需要有人有权执行 ——
        在那之前只认配置里的 `privileged_qq`。绑定之后，绑定会话本身即可操作。
        """
        sid = str(event.get_sender_id() or "")
        if self.privileged_qq and sid == self.privileged_qq:
            return True
        b = self._admin_binding()
        if not b.get("unified_msg_origin"):
            return False
        return str(event.unified_msg_origin or "") == str(b["unified_msg_origin"])

    # --------------------------------------------------------- /admin（S12 收窄）

    @filter.command("admin")
    async def cmd_admin(self, event: AstrMessageEvent, sub: str = "", arg: str = ""):
        """统一管理入口（用户要求：把散落的 7 个命令收窄成一个好记的入口）。

        子命令：status / sleep / wake / reload / bind / dream / relations
        权限：`privileged_qq`（bootstrap）**或** admin_binding 记录的会话。
        绑定私聊后，推送与权限都走 `admin_binding.json`（用户要求合并）。
        """
        if not self._is_privileged(event):
            yield event.plain_result("无权限。仅管理员私聊可用。")
            return
        sub = (sub or "").strip().lower()
        arg = (arg or "").strip()
        if not sub:
            yield event.plain_result(self._admin_help_text())
            return
        handler = {
            "status": self._admin_status,
            "sleep": self._admin_sleep,
            "wake": self._admin_wake,
            "reload": self._admin_reload,
            "bind": self._admin_bind,
            "dream": self._admin_dream,
            "relations": self._admin_relations,
        }.get(sub)
        if handler is None:
            yield event.plain_result(
                f"未知子命令 `{sub}`。可用：status / sleep / wake / reload / "
                f"bind / dream / relations"
            )
            return
        async for _r in handler(event, arg):
            yield _r

    def _admin_help_text(self) -> str:
        """子命令一览 —— 除绑定状态外都是静态文案（便于随时记起用法）。"""
        b = self._admin_binding()
        bound = b.get("unified_msg_origin") or "（未绑定）"
        return (
            "=== persona_agent /admin ===\n"
            "  /admin status              状态概览\n"
            "  /admin sleep [小时]        临时睡眠（不带参数=直到醒来）\n"
            "  /admin wake                恢复睡眠窗规则\n"
            "  /admin reload              重载配置（开关即时生效）\n"
            "  /admin bind                把当前私聊绑定为推送+管理会话\n"
            "  /admin dream               立即做一次梦\n"
            "  /admin relations           列出待批的关系提案\n"
            "  /admin relations apply 1 3 批准（支持多个序号，不支持区间）\n"
            "  /admin relations reject 2  驳回\n"
            f"\n当前绑定: {bound}"
        )

    # ---- 子命令实现 ----

    async def _admin_status(self, event, arg):
        cfg = self.config
        lines = [
            "=== persona_agent status ===",
            f"target_group     : {self.target_group_id}",
            f"bot_qq           : {self.bot_qq}",
            f"style_source_qq  : {self.style_source_qq}",
            f"reply_on_at      : {cfg.get('reply_on_at')}",
            f"active_interjection : {cfg.get('active_interjection')}",
            f"rag.score_threshold : {(cfg.get('rag') or {}).get('score_threshold')}",
            f"interjection.min_gap: {(cfg.get('interjection') or {}).get('min_gap_sec')}",
            f"poke.enabled        : {(cfg.get('poke') or {}).get('enabled', 0)}"
            f"  proactive={(cfg.get('poke') or {}).get('proactive_enabled', 0)}",
            f"sticker.enabled     : {(cfg.get('sticker') or {}).get('enabled', 0)}"
            f"  teach={(cfg.get('sticker') or {}).get('teach', 0)}",
            f"gate.enabled        : {(cfg.get('gate') or {}).get('enabled', 0)}",
            f"dream.enabled       : {(cfg.get('dream') or {}).get('enabled', 0)}",
            f"summary             : weekly={(cfg.get('summary') or {}).get('weekly_enabled', 0)}"
            f" monthly={(cfg.get('summary') or {}).get('monthly_enabled', 0)}"
            f" yearly={(cfg.get('summary') or {}).get('yearly_enabled', 0)}",
        ]
        if self.interjection is not None:
            # B-023：展示**目标群**的用量（无参分支是"多群峰值"，不适合这里；
            # 且它此前不含这两个键 → 本命令必抛 KeyError）。
            snap = self.interjection.snapshot(self.target_group_id)
            lines.append(f"hourly_used[{self.target_group_id}] : "
                         f"{snap['hourly_used']:.2f}  hour={snap['current_hour']}")
        if self.session_mgr is not None:
            for gid, sz in (self.session_mgr.snapshot() or {}).items():
                lines.append(f"session[{gid}]  : {sz} msgs")
        yield event.plain_result("\n".join(lines))

    async def _admin_sleep(self, event, arg):
        try:
            hours = float(arg) if arg else None
        except ValueError:
            yield event.plain_result(f"「{arg}」不是小时数。用法：/admin sleep 2")
            return
        # 统一表达：("sleep", 到期时间戳或 None)
        expire = (time.time() + float(hours) * 3600.0) if hours else None
        self._sleep_override = ("sleep", expire)
        logger.info(f"[persona_agent] privileged sleep override hours={hours}")
        if hours:
            until = time.strftime("%H:%M", time.localtime(expire))
            yield event.plain_result(f"已进入临时睡眠 {hours} 小时（到 {until} 自动恢复）。")
        else:
            yield event.plain_result("已进入临时睡眠（直到 /admin wake）。")

    async def _admin_wake(self, event, arg):
        self._sleep_override = None
        logger.info("[persona_agent] sleep override cleared")
        yield event.plain_result("已恢复睡眠窗规则。")

    async def _admin_reload(self, event, arg):
        try:
            self._apply_live_config()
            yield event.plain_result(
                "配置已重载："
                f"active_interjection={self.config.get('active_interjection')} "
                f"reply_on_at={self.config.get('reply_on_at')} "
                f"rag.score_threshold={(self.config.get('rag') or {}).get('score_threshold')}"
            )
        except Exception as e:
            yield event.plain_result(f"重载失败: {e}")

    async def _admin_bind(self, event, arg):
        """把**当前会话**绑定为推送 + 管理会话（合并，用户要求）。"""
        umo = str(event.unified_msg_origin or "")
        sid = str(event.get_sender_id() or "")
        self.store.save_json("admin_binding.json", {
            "unified_msg_origin": umo,
            "bound_at": int(time.time()),
            "sender_id": sid,
        })
        logger.info(f"[persona_agent] admin_binding → {umo} (sender={sid})")
        yield event.plain_result(
            f"已绑定：\n  会话 {umo}\n  身份 {sid}\n"
            f"此后**推送**（周报/月报/年报/做梦/关系提案）与**管理权限**都走这个会话。"
        )

    async def _admin_dream(self, event, arg):
        if int((self.config.get("dream") or {}).get("enabled", 0)) != 1:
            yield event.plain_result("dream.enabled=0，已禁用做梦。")
            return
        yield event.plain_result("开始做梦…（约 30 秒）")
        try:
            await self._dream_job_runner()
            yield event.plain_result("做梦完成，已推送（若绑定正常）。")
        except Exception as e:
            logger.exception(f"[persona_agent] /admin dream failed: {e}")
            yield event.plain_result(f"做梦失败: {e}")

    async def _admin_relations(self, event, arg):
        """关系提案：list / apply N [N…] / reject N [N…]"""
        store = ProposalStore(self.data_dir)
        batch = store.load()
        parts = (arg or "").split(maxsplit=1)
        action = (parts[0] if parts else "").strip().lower()
        rest = (parts[1] if len(parts) > 1 else "").strip()

        if action in ("", "list"):
            yield event.plain_result(self._format_proposals(batch))
            return
        if action not in ("apply", "reject"):
            yield event.plain_result(
                f"未知操作 `{action}`。可用：list / apply N / reject N")
            return
        if not batch.proposals:
            yield event.plain_result("当前没有提案。")
            return
        idxs, err = parse_indices(rest, len(batch.proposals))
        if err:
            yield event.plain_result(err)
            return
        current = self._current_closeness()
        lines: list[str] = []
        changed = False
        for n in idxs:
            ok, msg, p = decide_proposal(batch, n, "approve" if action == "apply" else "reject",
                                current)
            if not ok:
                lines.append(f"✗ {msg}")
                continue
            if action == "apply" and p.status == "approved":
                # 真正落到配置里（只升不降；写前再判一次）
                if self.style is not None and self.style.set_closeness(
                        p.uin, p.to_closeness):
                    current[p.uin] = p.to_closeness
                    lines.append(
                        f"✓ #{n} {p.alias or p.uin} "
                        f"{CLOSENESS_CN.get(p.from_closeness, p.from_closeness)}→"
                        f"{CLOSENESS_CN.get(p.to_closeness, p.to_closeness)}"
                        f"（{p.reason}）")
                else:
                    lines.append(f"✗ #{n} 写入成员表失败（可能该 uin 已不存在）")
                changed = True
            else:
                lines.append(f"✓ {msg or f'#{n} 已{action}'}")
                changed = True
        if changed:
            store.save(batch)
        yield event.plain_result("\n".join(lines) or "无变化")

    def _format_proposals(self, batch) -> str:
        if not batch.proposals:
            return "当前没有待批的关系提案。"
        lines = [f"=== 关系提案（{batch.period or '?'}）==="]
        for p in batch.proposals:
            mark = {"pending": "◻", "approved": "✔", "rejected": "✘"}.get(p.status, "?")
            lines.append(
                f"  {mark} #{p.index} {p.alias or p.uin} "
                f"{CLOSENESS_CN.get(p.from_closeness, p.from_closeness)}→"
                f"{CLOSENESS_CN.get(p.to_closeness, p.to_closeness)}"
                f"  {p.reason}")
        lines.append("\n/admin relations apply 1 3   /admin relations reject 2")
        return "\n".join(lines)

    def _current_closeness(self) -> dict[str, str]:
        """当前等级快照 ``{uin: closeness}``（apply 的二次校验用）。"""
        if self.style is None:
            return {}
        try:
            return {u: v for u, v in
                    ((str(m.get("uin") or ""), str(m.get("closeness") or ""))
                     for m in self.style._iter_members()) if u}
        except Exception:
            return {}

    # ----------------------------------------------------------------- events

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        # AstrBot v4.27.4 起 _convert_handle_notice_event() 把**带 group_id 的通知**
        # 归为 GROUP_MESSAGE（不再走 OTHER_MESSAGE），于是群戳/群撤回等通知会落到这里；
        # 它们的 message_str 为空，会撞上下面的媒体过滤器并被 stop_event() 吞掉。
        # 而 StarRequestSubStage 的派发循环是 `for handler in activated_handlers:
        # if event.is_stopped(): break`，且 handler 顺序 = 装饰器注册顺序（本方法定义在
        # on_notice 之前）→ 一旦在这里 stop_event，on_notice 永远收不到 poke。
        # 故：非 message 类事件（notice/request）一律**不拦不吞**，原样交给 on_notice。
        if self._is_notice_event(event):
            return

        if not self._is_target_group(event):
            return

        self._record_inbound(event)

        if self.style is None or self.rag is None or self.interjection is None:
            return
        if self.session_mgr is None or self.kg_provider is None:
            return

        is_at = self._is_at_bot(event)
        sender_uin = str(event.get_sender_id() or "")
        text = self._clean_message_text(event.message_str or "")
        group_id = str(event.get_group_id() or "")

        # G15: describe images/gif stickers via vision model; QQ Face ids are
        # mapped locally. Descriptions join the message text before the
        # media filter, so captioned media enters the session as text.
        if self._vision_enabled:
            text = await self._augment_with_vision(event, text)

        # v3: media-only messages still excluded when vision failed or none
        # (recorded by the buffer for stats; forwards stay filtered).
        if not text.strip():
            event.stop_event()
            return

        # v3: daily rotation fallback (primary trigger is the 02:05 cron).
        # S2: 同样先落盘挂起条目（否则会写进新一天的会话）
        if self._pipeline is not None:
            try:
                self._pipeline.flush_all_pending_appends()
            except Exception:
                pass
        old_msgs = self.session_mgr.rotate_if_day_changed(group_id)
        if old_msgs and self._diary_enabled:
            asyncio.create_task(self._generate_diary(group_id, old_msgs))

        alias = self.style.preferred_alias(sender_uin) or f"群友{sender_uin}"
        if alias.startswith("群友") and sender_uin and sender_uin != self.bot_qq:
            # G9: unknown caller -> append a 'new' member entry (async, safe)
            sender_name = str(event.get_sender_name() or "")
            asyncio.create_task(asyncio.to_thread(self._auto_add_member, str(sender_uin), sender_name))
        # S2: 本条**推迟**入会话 —— 由 pipeline 在硬闸决策后、任何 early
        # return 之前落盘。三个理由：
        #   ① LLM 上下文与引用编号基都必须在"本条尚未入会话"的状态下构建，
        #      否则当前消息会被说两遍（session 一次 + 「现在要回应的」块一次）；
        #   ② 静默/睡眠的消息仍必须记录（v3 设计意图），交给 pipeline 统一保证；
        #   ③ `_generating` 早退（同群正在生成）时本条会**留到下一轮一起落盘**，
        #      比旧行为（直接丢弃）更完整。
        # 元数据（message_id / sender_uin）随条目落盘 —— B-001 的编号基对齐
        # 依赖它（此前 session 无 id，解析只能对实时 buffer 求值 → 双错位）。
        _mid = str(getattr(event.message_obj, "message_id", "") or "")
        if self._pipeline is not None:
            self._pipeline.defer_session_append(
                group_id, text, name=alias, message_id=_mid,
                sender_uin=str(sender_uin or ""),
            )
        else:
            # 兜底：pipeline 未就绪时直接写（保持旧行为）
            self.session_mgr.append(
                group_id, "user", text, name=alias,
                message_id=_mid, sender_uin=str(sender_uin or ""),
            )

        # v3: sleep window — bot stays silent (mimics human rest) but the
        # message has already joined the session/memory for the new day.
        if self._is_sleeping():
            self._log_decision({
                "action": "silent",
                "trigger": "sleep",
                "reason": "sleep window (no participation)",
                "score": 0.0,
                "hour": self._local_hour(),
                "hourly_budget": 0.0,
                "hourly_used": 0.0,
                "silence_sec": 0.0,
                "cooldown_left_sec": 0.0,
                "extra": {},
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "sender_uin": sender_uin,
                "group_id": group_id,
            })
            event.stop_event()
            return

        if self._memory_store is not None:
            asyncio.create_task(asyncio.to_thread(
                self._memory_store.ingest,
                MemoryEvent(speaker_alias=alias, text=text, group_id=group_id),
            ))

        # A7④: 冲突安全阀路径分流——
        #   gate.enabled=1: conflict 判定由 GateLLM 承担（pipeline 内，覆盖 @/topic；
        #     每次候选都判，无 keyword 漏检）；此处不跑旧 detector。
        #   gate.enabled=0: 保留旧 conflict_detector（keyword+burst+verify）作兜底，
        #     避免关 Gate 连带失去全部冲突保护。
        if self._conflict_detector is not None and self._gate is None:
            conflict_ctx = self._conflict_detector.feed(time.time(), alias, text)
            if conflict_ctx:
                is_conflict = await self._conflict_detector.verify_with_llm(
                    self.context, conflict_ctx
                )
                if is_conflict:
                    event.stop_event()
                    await self._notify_admin(conflict_ctx, group_id, alias)
                    return

        if self._generating.get(group_id):
            event.stop_event()
            return

        # A7 (2a): 主决策/生成链路交给共享 pipeline（线上 main 与离线测试台
        # 同一份代码）。pipeline 内部完成 RAG→emotion→硬闸→GateLLM→KG→生成
        # →postprocess→quote 解析，返回 SendIntent + 全链路 trace。
        # 锁语义：_generating 保护"decide→生成→发送"整段；topic/silent 分支
        # 在 finally 释放锁后处理（_send_topic 内部自设锁，避免嵌套释放）。
        if self._pipeline is None:
            event.stop_event()
            return
        send_intent: Optional[SendIntent] = None
        self._generating[group_id] = True
        try:
            send_intent = await self._pipeline.run(PipelineInput(
                group_id=group_id,
                text=text,
                is_at=is_at,
                sender_uin=sender_uin,
                sender_alias=alias,
                umo=str(event.unified_msg_origin or ""),
            ))
        finally:
            self._generating[group_id] = False

        if send_intent is None:
            event.stop_event()
            return
        trace = send_intent.trace or {}

        # decision log (back-compat: keep decision_log.jsonl emitting)
        dlog = {
            "action": "silent" if send_intent.action == "silent" else send_intent.action,
            "trigger": (trace.get("hard_gate") or {}).get("trigger", ""),
            "reason": send_intent.silent_reason
            or (trace.get("hard_gate") or {}).get("reason", ""),
            "score": (trace.get("hard_gate") or {}).get("score", 0.0),
            "hour": self._local_hour(),
            "hourly_budget": 0.0,
            "hourly_used": 0.0,
            "silence_sec": 0.0,
            "cooldown_left_sec": (trace.get("hard_gate") or {}).get("cooldown_left_sec", 0.0),
            "extra": {},
            "ts": trace.get("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
            "sender_uin": sender_uin,
            "group_id": group_id,
        }
        rag_hits = trace.get("rag") or []
        dlog["extra"]["top_rag_score"] = rag_hits[0].get("score", 0.0) if rag_hits else 0.0
        dlog["extra"]["emotion_multiplier"] = (trace.get("emotion") or {}).get("willingness", 1.0)
        if send_intent.action == "silent":
            dlog["extra"]["silent_reason"] = send_intent.silent_reason
        if "gate" in trace:
            dlog["extra"]["gate"] = trace["gate"]
            # gate_log.jsonl (per-group dir): independent gate decision record
            gate_entry = dict(trace["gate"])
            gate_entry["decision_action"] = dlog["action"]
            gate_entry["decision_trigger"] = dlog["trigger"]
            gate_entry["group_id"] = group_id
            try:
                self.store.append_jsonl(
                    f"logs/{group_id}/gate_log.jsonl", gate_entry)
            except Exception:
                pass
        self._log_decision(dlog)

        # trace log (A7: full-chain record, caller-side persist, per-group dir)
        if self._trace_enabled():
            try:
                self.store.append_jsonl(f"logs/{group_id}/trace_log.jsonl", trace)
            except Exception:
                pass

        if send_intent.action == "silent":
            # A7④: Gate 判 conflict → 旁路通知管理员（30min 冷却在 _notify_admin
            # 内；通知为附加功能，发言闸已由 pipeline 拦截——冲突中绝不发言）。
            gconf = (trace.get("gate") or {}).get("conflict")
            if gconf:
                try:
                    ctx_lines = []
                    recent = self.session_mgr.recent(group_id, n=15) if self.session_mgr else []
                    for m in recent:
                        nm = m.get("name") or m.get("role", "")
                        ct = (m.get("content") or "").strip()
                        if ct:
                            ctx_lines.append(f"{nm}: {ct}")
                    await self._notify_admin(
                        "\n".join(ctx_lines[-10:]), group_id, alias
                    )
                except Exception as e:
                    logger.warning(f"[persona_agent] conflict notify failed: {e}")
            event.stop_event()
            return

        if send_intent.action == "topic":
            # Cold-start topic: pipeline flagged the slot; send via the
            # existing main-side _send_topic (needs event for active send).
            # Rebuild a light Decision from trace.hard_gate for it.
            hg = trace.get("hard_gate") or {}
            topic_decision = Decision(
                action=ACTION_TOPIC,
                trigger=str(hg.get("trigger", TRIGGER_COLD)),
                reason=str(hg.get("reason", "cold_start")),
                score=float(hg.get("score", 0.0) or 0.0),
                silence_sec=float(hg.get("silence_sec", 0.0) or 0.0),
            )
            live_ctx = self.buffer.format_recent(max_lines=20) if self.buffer else ""
            await self._send_topic(event, group_id, topic_decision, live_ctx, sender_uin)
            event.stop_event()
            return

        # ---- reply: send per SendIntent ----
        reply_text = send_intent.text
        if not reply_text:
            event.stop_event()
            return
        if send_intent.quote_id:
            yield event.chain_result([Comp.Reply(id=send_intent.quote_id), Comp.Plain(reply_text)])
        else:
            yield event.plain_result(reply_text)

        # ---- 动作链路（S3③）：[emote:意图] → 贴纸库选图 → 随正文同发 ----
        # 设计取舍（docs/specs/s3-action-channel.md §4.3）：
        #   · 与正文**同一条消息**发出（"一句话 + 一张表情"），不额外多一条消息
        #   · 选不中/库为空/文件缺失/**超配额** → 静默跳过，正文照发（绝不兜底文生图）
        #   · 每次尝试都落 sticker_log.jsonl（S0 教训：降级必须可见）
        if send_intent.emote:
            # 异步生成器 → async for（每个 yield 是一条独立出站消息）
            async for _sticker_result in self._send_sticker(event, group_id, send_intent):
                yield _sticker_result

        # ---- S7 主动戳人：[poke:名字] → 校验 → group_poke action ----
        # 与贴纸**不同**：不 yield 消息段，而是直接调 action（段通道在协议端会被丢弃）。
        # 也不需要 stop_event（那是被动回戳为阻止内置 LLM 兜底才要的）。
        if send_intent.poke:
            await self._send_proactive_poke(event, group_id, send_intent, trace)

        if send_intent.sticker_prompt:
            # 旧的 emotion.sticker → 文生图通道：保留但不再是贴纸主路径
            try:
                yield event.chain_result([Comp.Image.fromText(send_intent.sticker_prompt)])
            except Exception:
                pass

        # register reply + persist session/buffer (main-side side effects)
        trigger = (trace.get("hard_gate") or {}).get("trigger", "rag_hit")
        self.interjection.register_reply(
            group_id=group_id,
            now_utc=time.time(),
            trigger=trigger,
            sender_uin=sender_uin,
        )
        # S16: 思维链**存进 session**（供人工查看），但 `_reasoning` 是内部键
        # → 被 `_public` 剥离 → **不进 LLM 上下文**（避免信噪比退化）。
        self.session_mgr.append(
            group_id, "assistant", reply_text,
            reasoning=str((trace or {}).get("reasoning") or ""),
        )
        if self.buffer is not None:
            self.buffer.add(
                ts=time.time(),
                group_id=str(event.get_group_id() or ""),
                sender_id=self.bot_qq,
                sender_name="<bot>",
                text=reply_text,
                message_id="",
                message_type="bot",
            )

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_notice(self, event: AstrMessageEvent):
        """G11: poke notice -> PokeService decision -> poke back.

        （原名 on_other；改名以反映它处理通知事件而非「其它消息」。）

        过滤器用 ALL 而非 OTHER_MESSAGE：AstrBot v4.27.4 起
        `_convert_handle_notice_event()` 会把**带 group_id 的通知**归为
        GROUP_MESSAGE，`OTHER_MESSAGE` 在该路径上不可达 —— 旧写法（OTHER_MESSAGE）
        在群戳场景下**永不触发**（且无任何报错）。判定改为在插件侧做（见
        services/protocol_compat.normalize_poke），不依赖宿主的事件类型映射。

        Group/production routing mirrors _is_target_group() on the raw
        group_id (notice events may not carry a resolved group context in
        get_group_id()). poke.enabled=0 leaves this a complete no-op.
        """
        if self._poke is None:
            return
        raw = getattr(event.message_obj, "raw_message", None)
        notice = protocol_compat.normalize_poke(raw)
        if notice is None:
            return
        # 不是戳向本机器人的通知（例如戳别人）→ 不是本插件的事，不认领
        if notice.target != self.bot_qq:
            return

        # --- 以下均为「戳了机器人」→ 本插件认领该事件 ---------------------------
        # 认领的含义：走完决策后必定 stop_event()，防 AstrBot 内置 LLM 对一条空消息
        # 作答（私聊通知会置 is_at_or_wake_command=True，不拦就会触发内置 LLM）。
        # ⚠️ 唯一的例外是段通道——它必须 yield 结果给 AstrBot 发送，而调度器在
        # `async for _ in agen: if event.is_stopped(): break` 处**先判停止再跑后续阶段**，
        # 因此「先 stop_event 再 yield」会导致消息发不出去（见 _poke_back）。

        # 戳一戳撤回不是一次真戳（也不应触发任何回复）
        if notice.is_recall:
            event.stop_event()
            return
        # 既定策略：只回群戳；私聊戳认领但不回
        if not notice.is_group:
            event.stop_event()
            return

        group_id = notice.group_id
        if self.test_mode == 1:
            if group_id != self.test_group_id:
                return
        elif group_id != self.target_group_id:
            return

        poker = notice.poker
        poke_cfg = self.config.get("poke", {}) or {}
        if int(poke_cfg.get("enabled", 0)) != 1:
            event.stop_event()
            return

        self._poke.configure(
            cooldown_sec=float(poke_cfg.get("cooldown_sec", 300)),
            hourly_cap=int(poke_cfg.get("hourly_cap", 4)),
            proactive_enabled=int(poke_cfg.get("proactive_enabled", 0)) == 1,
            proactive_hourly_cap=int(poke_cfg.get("proactive_hourly_cap", 5)),
        )
        alias = ""
        if self.style is not None:
            try:
                alias = self.style.preferred_alias(poker)
            except Exception:
                alias = ""
        recent_text = ""
        if self.buffer is not None:
            try:
                recent_text = self.buffer.format_recent(max_lines=3, max_chars=400)
            except Exception:
                recent_text = ""
        respond, reason = self._poke.decide(
            now_utc=time.time(),
            poker=poker,
            group_id=group_id,
            known_member=bool(alias),
            recent_text=recent_text,
        )
        self._poke.record(
            now_utc=time.time(),
            poker=poker,
            group_id=group_id,
            responded=respond,
            reason=reason,
        )
        if not respond:
            logger.info(f"[persona_agent] poke ignored: poker={poker} reason={reason}")
            event.stop_event()
            return
        # _poke_back 是异步生成器（段通道需要 yield 结果给 AstrBot 发送）
        async for result in self._poke_back(event, notice, alias):
            yield result

    async def _poke_back(
        self, event: AstrMessageEvent, notice: protocol_compat.PokeNotice, alias: str
    ):
        """按协议端能力回戳（action 优先，消息段兜底）。

        为什么不能只用 `Comp.Poke`：LLBot v8 的出站消息段转换表
        （src/onebot11/transform/message/outgoing.ts）**没有 poke 分支也没有
        default** → `{"type":"poke",...}` 被**静默丢弃**（无日志、无报错）；
        NapCat 的 poke 段转换器同样是空实现桩。→ 正确通道是 action
        `group_poke`（LLBot src/onebot11/action/llbot/group/GroupPoke.ts / NapCat 同名），
        它直接把戳发给协议端，不经过消息段转换。

        调度器语义（决定了这里的结构）：handler yield 结果后，调度器**先**检查
        `event.is_stopped()`，**再**递归执行后续阶段（ResultDecorate → Respond 发送）。
        因此段通道**不能先 stop_event 再 yield**，否则消息根本不会发出；而 action
        通道不经过 pipeline，成功后才 stop_event() 以阻止内置 LLM 兜底。

        任何失败都只记 warning，不抛出：poke 失败绝不影响后续消息处理。
        """
        caps = protocol_compat.capabilities_for(
            str((self.config.get("poke", {}) or {}).get("protocol", "") or "")
        )
        call = protocol_compat.poke_action_call(notice)
        bot = getattr(event, "bot", None)  # AiocqhttpMessageEvent.bot（非该适配器则无）
        for channel in protocol_compat.poke_channels(caps):
            if channel == protocol_compat.CHANNEL_ACTION:
                if call is None or bot is None or not hasattr(bot, "call_action"):
                    continue
                action, payload = call
                try:
                    await bot.call_action(action, **payload)
                except Exception as ex:
                    logger.warning(f"[persona_agent] poke action {action} failed: {ex}")
                    continue
                logger.info(
                    f"[persona_agent] poke back to {notice.poker} via {action} "
                    f"(alias={alias or '?'})"
                )
                event.stop_event()  # 已由 action 直发，无需 pipeline 发送
                return
            if channel == protocol_compat.CHANNEL_SEGMENT:
                # 先 yield 让调度器送出；不可在此前 stop_event（会阻断发送）
                try:
                    yield event.chain_result([Comp.Poke(id=notice.poker)])
                except Exception as ex:
                    logger.warning(f"[persona_agent] poke segment failed: {ex}")
                    continue
                logger.info(
                    f"[persona_agent] poke back to {notice.poker} via poke segment "
                    f"(alias={alias or '?'})"
                )
                event.stop_event()
                return
        logger.warning(
            f"[persona_agent] poke back to {notice.poker} exhausted all channels"
        )
        event.stop_event()

    async def _send_topic(
        self,
        event: AstrMessageEvent,
        group_id: str,
        decision: Decision,
        live_ctx: str,
        sender_uin: str,
    ) -> None:
        """G12: cold-start topic utterance via TopicBank + LLM rewrite.

        Sends actively to the group (no reply chain); archives the topic on
        success; stays fully silent when there is no eligible topic, no
        provider, or the LLM returns nothing usable. topic_bank.enabled=0
        never reaches here (interjection master gate).
        """
        if self._topic_bank is None or self.style is None or self.session_mgr is None:
            return
        topic = self._topic_bank.pick(
            now=time.time(),
            silence_sec=decision.silence_sec,
            live_text=live_ctx or "",
            group_id=group_id,
        )
        log = decision.to_log(time.time(), sender_uin)
        if topic is None:
            log["extra"] = {"topic_id": None, "sent": False}
            self._log_decision(log)
            logger.info("[persona_agent] topic action: no eligible topic, staying silent")
            return
        self._generating[group_id] = True
        try:
            await asyncio.sleep(0.5)
            user_block = (
                "（群里冷场了。现在需要你以本人的语气主动说一句开启新话题，"
                "不要提“冷场/没人说话”，不要机械照抄素材，一两句自然的话即可。）\n"
                f"【话题素材】{topic.content}\n"
                f"【分类】{topic.category or '日常'}\n"
                f"【最近聊天】{(live_ctx or '（无）').strip()}"
            )
            contexts = [{"role": "user", "content": user_block}]
            text = await self._generate_reply(
                event,
                "",
                contexts,
                None,
                temperature=self._temperature_for(TRIGGER_COLD),
            )
            clean_text, _ = text_style.extract_quote(text)
            reply_text = self._postprocess(clean_text)
            if not reply_text:
                log["extra"] = {"topic_id": topic.id, "sent": False, "reason": "empty"}
                self._log_decision(log)
                logger.info("[persona_agent] topic: empty LLM output, staying silent")
                return
            chain = MessageChain().message(reply_text)
            await self.context.send_message(event.unified_msg_origin, chain)
            self._topic_bank.mark_sent(topic, reason="cold_start", group_id=group_id)
            # S16: 冷场话题回复同样留存思维链（该路径现走的不是 pipeline，无 trace）
            self.session_mgr.append(group_id, "assistant", reply_text)
            if self.buffer is not None:
                self.buffer.add(
                    ts=time.time(),
                    group_id=group_id,
                    sender_id=self.bot_qq,
                    sender_name="<bot>",
                    text=reply_text,
                    message_id="",
                    message_type="bot",
                )
            self.interjection.register_reply(
                group_id=group_id, now_utc=time.time(), trigger=TRIGGER_COLD
            )
            log["extra"] = {"topic_id": topic.id, "sent": True}
            self._log_decision(log)
            logger.info(f"[persona_agent] topic sent: id={topic.id} category={topic.category}")
        except Exception as ex:
            logger.warning(f"[persona_agent] topic send failed: {ex}")
        finally:
            self._generating[group_id] = False

    # ----------------------------------------------------------------- LLM

    def _session_append(
        self, group_id: str, text: str, name: str, message_id: str, sender_uin: str
    ) -> None:
        """S2: pipeline 在决策后回调此处，把本条写入会话。

        签名固定为位置参数（pipeline 不感知 SessionManager 的 kwargs）。
        """
        self.session_mgr.append(
            group_id, "user", text, name=name or None,
            message_id=message_id or "", sender_uin=sender_uin or "",
        )

    # ---- S3: 工具语法（恒定块，进缓存前缀）----

    def _startup_selfcheck(self) -> None:
        """启动自检：把"静默失效"变成启动日志里的一行。

        本轮实测的教训（hourly_budget 时区错位）：**配额类参数失效时不会报错**，
        只会让功能"看起来像没触发"。这里对最容易出问题的地方逐项体检：

          1. 当前小时的可用预算 —— < 1.0 即"结构性静音"（每条消耗 1.0）
          2. 预算表是否整体异常（全天都 < 1.0 = 表错了，如时区错位）
          3. 贴纸库/索引就绪度（开关开了但库空 = 静默不发表情）
          4. 关系图谱与人格块体积（暴涨会拖垮缓存前缀）
        **只告警、不改行为**（自检绝不动配置）。
        """
        try:
            hour = self._local_hour()
            budget = float(self.style.hourly_budget(hour)) if self.style else 0.0
            logger.info(f"[selfcheck] 本地 {hour:02d} 时：本小时预算 {budget:.2f} 条")
            if budget < 1.0:
                logger.warning(
                    f"[selfcheck] ⚠️ 本小时预算 {budget:.2f} < 1.0 → 主动插话**结构性静音**"
                    f"（每条消耗 1.0）。检查 my_hourly_distribution.json 是否为本地时索引"
                    f"（历史文件可能是 UTC，见 StyleProfile._hourly_local）"
                )
            if self.style is not None:
                low = [h for h in range(24) if float(self.style.hourly_budget(h)) < 1.0]
                if len(low) >= 20:
                    logger.warning(
                        f"[selfcheck] ⚠️ 全天 {len(low)}/24 小时预算 < 1.0 → 预算表整体异常"
                        f"（多半是时区/量纲错位），主动插话几乎不可能触发"
                    )
            # 贴纸
            scfg = self.config.get("sticker", {}) or {}
            if int(scfg.get("enabled", 0)) == 1:
                self._ensure_sticker()
                if self._sticker is None:
                    logger.warning("[selfcheck] ⚠️ sticker.enabled=1 但服务不可用 → 表情永远发不出")
                else:
                    snap = self._sticker.snapshot()
                    logger.info(f"[selfcheck] 贴纸库 {snap['size']} 条 min_score={snap['min_score']}")
                    if snap["size"] == 0:
                        logger.warning(
                            f"[selfcheck] ⚠️ sticker.enabled=1 但库为空"
                            f"（{snap.get('load_error') or '索引未建'}）→ 静默不发表情"
                        )
                    if int(scfg.get("teach", 0)) == 1 and snap["size"] == 0:
                        logger.warning("[selfcheck] ⚠️ 已教 [emote:] 语法但库为空 → 白教")
            # 人格块体积（缓存前缀的核心成本）
            if self.style is not None:
                # S9：人格与关系图谱已拆分，两者**都要**看体积 ——
                # 人格恒定（变了说明人工改了人设，会作废一次全量缓存）；
                # 关系图谱缓慢增长（新成员入列），但改成了"只在尾部追加"，
                # 所以增长本身不再击穿它之后的前缀。
                sp_len = len(self.style.system_prompt())
                rel_len = len(self.style.relations_block())
                logger.info(
                    f"[selfcheck] 人格提示词 {sp_len} 字符（恒定块）| "
                    f"关系图谱 {rel_len} 字符（追加式，进缓存前缀）"
                )
                if sp_len > 20000:
                    logger.warning(
                        f"[selfcheck] ⚠️ 人格提示词 {sp_len} 字符偏大 → 每轮进前缀，"
                        f"检查 system_prompt_fragments.json 是否失控"
                    )
                if rel_len > 30000:
                    logger.warning(
                        f"[selfcheck] ⚠️ 关系图谱 {rel_len} 字符偏大 → 虽为追加式，"
                        f"但每轮都要传；考虑把 new 段压缩或按需注入"
                    )
        except Exception as e:
            logger.warning(f"[selfcheck] 自检本身失败（不影响运行）: {e}")

    # ---- S10: 关系图谱增量（追加式）----

    _REL_STATE = "relations_block_state.json"

    def _active_group_id(self) -> str:
        """当前生效的群（test_mode=1 时是测试群）—— 会话级状态都按它取。"""
        return self.test_group_id if int(self.test_mode or 0) == 1 else self.target_group_id

    def _frozen_relations_block_text(self) -> str:
        """取会话里冻结的关系图谱块（无则空串；绝不抛）。"""
        try:
            if self.session_mgr is not None and hasattr(
                    self.session_mgr, "frozen_relations_block"):
                return self.session_mgr.frozen_relations_block(self._active_group_id())
        except Exception:
            pass
        return ""

    def _align_frozen_relations_block(self) -> None:
        """B-032 修复态：增量状态缺失时把头部冻结块重冻成当前图谱。

        为什么必须做：`_relations_delta_block` 的"首次运行"会把 known 设成当前全部行，
        而头部块可能还停在更早的冻结值 → 两者之间的差异**永远不会被追加**，
        对 LLM 永久不可见（静默）。重冻会破一次前缀缓存，故留一条 warning。
        """
        if self.style is None or self.session_mgr is None:
            return
        if not hasattr(self.session_mgr, "freeze_relations_block"):
            return
        try:
            before = self._frozen_relations_block_text()
            after = self.session_mgr.freeze_relations_block(
                self._active_group_id(), self.style.relations_block(), force=True)
            if before and before != after:
                logger.warning(
                    "[persona_agent] 关系图谱增量状态缺失 → 头部冻结块已重冻为当前图谱"
                    "（B-032 修复态；本次会破一次前缀缓存）"
                )
        except Exception as e:
            logger.warning(f"[persona_agent] 冻结块对齐失败（不影响运行）: {e}")

    def _relations_delta_block(self) -> str:
        """算出需要追加的"群友识别更新"文本；无变化返回空串。

        ## 设计（用户 2026-09-14 拍板）

        关系图谱**整块**进前缀时，它一变就让其后全部内容（示例块 + session
        8 万 token）前缀失效。改为：**块不动，增量以一条 system 消息追加到
        session 尾部** —— 前面逐字节不变。

        接受"更新那一次必然 miss"，因为替代方案（更新不 miss）意味着
        **全量群聊上下文 + LLM 思维链 + RAG 示例文段全部 miss**。

        状态（``relations_block_state.json``）= ``{uin: 该行文本}``。
        行文本变化也算增量 —— 否则**人工调整亲疏对 LLM 永远不可见**
        （与"熟悉度只升不降 + 人工批准"的体系冲突）。

        首次运行：把当前全部行写入状态并**返回空**（初始块已在前缀里，
        不该在第一次就灌几百行历史）。
        """
        if self.style is None:
            return ""
        try:
            lines = self.style.relations_lines()
        except Exception:
            return ""
        if not lines:
            return ""
        state = self.store.load_json(self._REL_STATE, {}) or {}
        known = state.get("known") or {}
        # 🔴 B-032：决策整体下沉到纯函数（`plan_relations_delta`，可被测试锁住）——
        # 头部冻结块里已写着的行不再往尾部讲一遍；"有变化但头部已讲过"时
        # 仍要推进 known，否则每轮重算同一差集。
        plan = plan_relations_delta(self.style, known,
                                    self._frozen_relations_block_text())
        if plan.first_run:
            # 首次：登记全部，不追加（初始块已在恒定前缀里）
            # 🔴 B-032（2026-09-17 独立核验）：状态被删/重置时，"头部冻结块"可能
            # 与即将写入的 known 不一致 —— 那段差异对 LLM **永久不可见**（静默）。
            # 修复态：把头部重冻成当前图谱，让两边对齐（会破一次前缀缓存，留痕）。
            self._align_frozen_relations_block()
            self.store.save_json(self._REL_STATE,
                                 {"known": plan.known, "initialized_at": time.time()})
            return ""
        if not plan.text:
            if plan.known != known:
                # 只在真有差异时写盘（避免每轮一次无谓的文件写）
                self.store.save_json(
                    self._REL_STATE,
                    {"known": plan.known, "updated_at": time.time(),
                     "added": 0, "changed": 0, "aligned_with_frozen": True})
            return ""
        # 落盘新状态（原子写；失败也不影响本轮，下轮会重算同样的增量）
        try:
            self.store.save_json(
                self._REL_STATE,
                {"known": plan.known, "updated_at": time.time(),
                 "added": plan.added, "changed": plan.changed})
        except Exception as e:
            logger.warning(f"[persona_agent] 关系增量状态落盘失败: {e}")
        logger.info(
            f"[persona_agent] 群友识别更新：新增 {plan.added} 人、"
            f"变化 {plan.changed} 人 → 追加到会话尾部"
        )
        return plan.text

    def _tool_syntax_block(self) -> str:
        """声明可用的动作语法。**内容恒定**（按开关拼一次），进缓存前缀。

        为什么单独一条常量消息：标记语法必须**在提示词里教**模型才会用，
        但内容恒定 → 放在前缀里"一次付清"，不逐轮付费。
        两个开关各自控制（库空时不该教 `[emote:]` —— 写出来也没图可发）。
        """
        lines: list[str] = []
        if int((self.config.get("sticker", {}) or {}).get("teach", 0)) == 1:
            lines.append(
                "如果你想在回复后配一张表情包，就在回复**末尾**写："
                "`[emote:意图短语]`（如 `[emote:无奈地摇头]`、`[emote:害羞比心]`）。"
                "短语描述你想表达的情绪或动作；选不中就不发，正文照常。"
                "没有合适的表情时**不要**硬写这个标记。"
            )
        if int((self.config.get("poke", {}) or {}).get("teach", 0)) == 1:
            lines.append(
                "如果你想戳一下某人（QQ 的拍一拍），在回复里写 `[poke:对方的名字]`，"
                "名字就用你在群里叫他的那个称呼（如 `[poke:虾鱼丸]`）。"
                "**只对你熟悉的人用**（关系不熟的人不要戳），"
                "且只在确实想引起对方注意时用。"
                "名字必须与群里使用的称呼一致，否则这一戳会被丢弃。"
            )
        if not lines:
            return ""
        return "［可用的表达标记］\n" + "\n".join(f"- {l}" for l in lines)

    def _build_turn_block(self, turn_lines: list[str], ctx: dict) -> str:
        """S2：构造「现在要回应的」块（pipeline 的 turn_block 回调）。

        拼成**一条** system 消息，让模型一眼看到"该回哪句、对谁、什么状态"：

            【现在要回应的】本条消息 @ 了你，通常应当回应。
            发话人：焦糖(337934842)
            焦糖：daishuki！
            ［图片］一只橘猫趴在键盘上，表情嫌弃
            【当下】现在本地时间 16 时。当前心情：轻松调侃

        设计依据（实测）：
          - 决策窗口 96 条的文本 **59% 已在 session 里** → 不再重复注入窗口，
            改为把"当前这一轮"显式抬出来（零新增内容，只是重新划界）
          - 图片/表情描述此前拼在正文里（`"daishuki！ （配图：一只猫）"`），
            RP 分不清"用户说的"与"系统给的"；现拆成 `［图片］` 行 ——
            语义上等价于模型自己看图（dsh read_image 的直投性质）
          - 时间/心情并进同一块，易变量仍集中在上下文末尾（缓存序不变）
        """
        uin = str(ctx.get("sender_uin") or "")
        alias = ctx.get("sender_alias") or (f"群友{uin}" if uin else "群友")
        head = "【现在要回应的】"
        if ctx.get("is_at"):
            head += "本条消息 @ 了你，通常应当回应。"
        # 只写别名：QQ 号对"怎么回这句话"没有帮助（别名已是唯一标识），
        # 且与下一行的「别名：内容」重复，白占 token。
        lines = [head, f"发话人：{alias}"]
        lines.extend(turn_lines)
        # 情绪由 _pipeline_generate 在调用生成前写入（pipeline 的 turn_block
        # 回调签名只带 turn_lines/ctx，情绪在 generate 回调那侧拿得到）
        mood = getattr(self._turn_block_emotion, "current_mood", "") or ""
        vol = (
            self.style.volatile_line(local_hour=self._local_hour(), mood=mood)
            if self.style else ""
        )
        if vol:
            lines.append("【当下】" + vol.replace("\n", " "))
        return "\n".join(lines)

    async def _generate_reply(
        self,
        event: AstrMessageEvent,
        user_text: str,
        contexts: list[dict],
        emotion: Optional[EmotionState] = None,
        temperature: Optional[float] = None,
        *,
        speaker_uin: Optional[str] = None,
        umo: Optional[str] = None,
        meta: Optional[dict] = None,
    ) -> str:
        """Generate a reply via LLM.

        `speaker_uin`/`umo` override event-derived values (used by pipeline,
        where no event object exists). When omitted, falls back to event.
        """
        local_hour = self._local_hour()
        sys_prompt = self.style.system_prompt() if self.style else ""

        if speaker_uin is None:
            speaker_uin = str(event.get_sender_id() or "")
        alias_txt = ""
        if self.style is not None:
            try:
                alias_txt = self.style.preferred_alias(speaker_uin) or ""
            except Exception:
                alias_txt = ""
        if not alias_txt:
            alias_txt = f"群友{speaker_uin}"

        mood = emotion.current_mood if emotion else ""
        if self._pipeline_has_turn_block:
            # S2：说话人 / 本条内容 / 图片 / 时间 / 心情 全部由 pipeline 的
            # `_build_turn_block` 拼成**一条**「现在要回应的」消息 ——
            # 模型的注意力集中在一处，而不是散在三条独立 system 消息里。
            # 这里不重复追加。
            pass
        else:
            # 旧调用路径（离线测试台 / 未接线）：保持原行为
            is_src = bool(self.style_source_qq) and speaker_uin == str(self.style_source_qq)
            speaker_line = (
                f"【当前说话人】与本消息对应的发话人：QQ {speaker_uin}，群内别名「{alias_txt}」"
                f"{'（风格源 QQ）' if is_src else ''}。"
                "请始终用该别名称呼 TA；无法确认时不要臆造其他群友的别名。"
            )
            if contexts and isinstance(contexts[-1], dict) and contexts[-1].get("role") == "system":
                contexts.insert(-1, {"role": "system", "content": speaker_line})
            else:
                contexts.append({"role": "system", "content": speaker_line})
            # 逐轮易变信息（时间 + 心情）—— 放在**上下文末尾**，不再拼进 system
            # prompt。system prompt 是请求的第一个 token 位置，它一变后面整段
            # 会话前缀全部 miss（原设计每小时废一次全量缓存；心情有值时 30s 一次）。
            vol = self.style.volatile_line(local_hour=local_hour, mood=mood) if self.style else ""
            if vol:
                if contexts and isinstance(contexts[-1], dict) and contexts[-1].get("role") == "system":
                    contexts.insert(-1, {"role": "system", "content": vol})
                else:
                    contexts.append({"role": "system", "content": vol})

        provider_id = await self._resolve_provider_id(
            umo if umo is not None else event.unified_msg_origin
        )
        if not provider_id:
            logger.warning("[persona_agent] no LLM provider available")
            # B-032（独立核验反例 4）：这条路径**根本没调 LLM**，
            # 不能与"模型返回空"混为一谈 —— 否则 trace 把尝试/失败都记高、成因记错
            if meta is not None:
                meta["error"] = "no provider available (LLM not called)"
                # 独立核验：这条路径没调 LLM → 让 pipeline 别把它算成"生成尝试"
                meta["llm_not_called"] = True
            return ""

        gen_kwargs = {}
        if temperature is not None:
            gen_kwargs["temperature"] = temperature
        # A7④: RP 思考强度（llm.reasoning_effort）。网关只认 low/high/max 等档位，
        # "off"/"none" 会 HTTP 400 → reasoning_value 返回 None 表示不发送该参数
        # （2026-09-10 实测：旧映射 off→"none" 导致每次调用 400 → 空回复静默）。
        _rv = reasoning_value(
            (self.config.get("llm") or {}).get("reasoning_effort", "low")
        )
        if _rv:
            gen_kwargs["reasoning_effort"] = _rv
        # S0 修复：``llm.max_tokens`` 此前只在 schema 里存在，**生成路径从未读取**
        # 它（只有离线测试台用）—— 配置项与实际行为不符。这里接上，使
        # 「512 才能容纳思考开销」这条实测结论真正生效。
        _mt = int((self.config.get("llm") or {}).get("max_tokens", 512) or 0)
        if _mt > 0:
            gen_kwargs["max_tokens"] = _mt
        try:
            # S14：**不再传 system_prompt** —— 它已在 contexts 第一条
            # （会话状态）。传了会与首条重复，且每次热加载会破缓存。
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=None,
                contexts=contexts,
                **gen_kwargs,
            )
        except Exception as e:
            logger.exception(f"[persona_agent] llm_generate raised: {e}")
            # B-028: 把成因回传给 pipeline（参数名 meta），落进 trace.llm_error ——
            # 否则"失败"在 trace 里与"没走到生成"完全不可区分。
            # ⚠️ 这个变量名第一次写成了 pipeline 侧的 llm_meta，
            # tests/test_static_checks.py 的未定义名检查当场拦下（main 不被导入，
            # 只有静态检查能兜住这类错）。
            if meta is not None:
                meta["error"] = f"{type(e).__name__}: {e}"
            return ""

        if int((self.config.get("llm") or {}).get("cache_probe_enabled", 1)) == 1:
            # S0 修复：探针此前要求 event 对象，而 pipeline 主链路走的是
            # standalone（event=None）→ **线上回复从未落过探针**（实测 105 行
            # 全停在 2026-08-27，且只有 topic 路径写入），缓存命中率长期无数据。
            # 这里同时接受显式 group_id，使主链路也能观测。
            gid = (
                str(event.get_group_id() or "")
                if event is not None
                else str(self._probe_group_id or "")
            )
            if gid:
                self._log_llm_probe(event, contexts, sys_prompt, provider_id, resp,
                                    local_hour, group_id=gid, kind="rp")

        # S16: 思维链回传（**不进上下文**，只供落 session + 人工查看）
        # B-029: 走 extract_reasoning —— 直接读 reasoning_content 永远拿到空串
        # （AstrBot 的 openai 源从不给它赋值，网关实际回的是 reasoning 字段）
        if meta is not None:
            try:
                meta["reasoning"] = extract_reasoning(resp)
            except Exception:
                pass
        text = (getattr(resp, "completion_text", "") or "").strip()
        if self._is_error_response(text):
            logger.warning(f"[persona_agent] llm returned error response, suppressed ({len(text)} chars)")
            # B-032：与"空生成"分开记 —— 这是模型**回了错误文本**后被抑制
            if meta is not None:
                meta["error"] = f"llm error response suppressed: {text[:80]}"
            return ""
        return text

    async def _send_proactive_poke(self, event: AstrMessageEvent, group_id: str,
                                   intent, trace: dict) -> bool:
        """执行 ``[poke:名字]``（S7 主动戳人）。**绝不抛出**。

        设计（用户 2026-09-14 拍板）：
          · 接口用**名字**而非 QQ 号 —— 模型背不出 185 人的号码，但能准确叫出昵称；
            戳错人是对外可见的社交事故，所以宁可戳不出去
          · 名称解析**严格**：精确匹配 alias → other_names → 否则跳过（不猜）
          · 候选池 = `close`（30 人）；对不熟的人戳一戳是冒犯
          · 复用被动回戳的**同人冷却计时器**（用户指定），并单独计主动小时配额
          · 任一校验不过 → **静默跳过戳、正文照发**
        """
        result: dict = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "raw_name": intent.poke, "done": False, "reason": "",
                        "target": "", "resolved_via": ""}
        if self._poke is None or self.style is None:
            return False
        try:
            pcfg = self.config.get("poke", {}) or {}
            if int(pcfg.get("proactive_enabled", 0)) != 1:
                result["reason"] = "proactive_disabled"
            else:
                uin, via = self.style.resolve_member_name(intent.poke)
                result["resolved_via"] = via
                if not uin:
                    result["reason"] = via          # unknown_name / ambiguous_*
                else:
                    result["target"] = uin
                    closeness = self.style.member_closeness(uin)
                    conflict = bool((trace.get("gate") or {}).get("conflict"))
                    ok, why = self._poke.decide_proactive(
                        target_uin=uin, closeness=closeness,
                        conflict=conflict,
                        recent_text=str((trace.get("turn_block") or {}) and "") or "",
                    )
                    result["reason"] = why
                    if ok:
                        bot = getattr(event, "bot", None)
                        if bot is None or not hasattr(bot, "call_action"):
                            result["reason"] = "no_action_channel"
                        else:
                            await bot.call_action(
                                "group_poke",
                                group_id=protocol_compat._as_int_or_str(group_id),
                                user_id=protocol_compat._as_int_or_str(uin),
                            )
                            result["done"] = True
                            alias = self.style.preferred_alias(uin)
                            logger.info(
                                f"[persona_agent] proactive poke → {uin}"
                                f"({alias or '?'}) via {result['raw_name']!r}"
                            )
        except Exception as e:
            result["reason"] = f"{type(e).__name__}: {e}"
        finally:
            try:
                self._poke.record_proactive(
                    target_uin=result["target"], group_id=group_id,
                    done=result["done"], reason=result["reason"],
                    raw_name=result["raw_name"], resolved_via=result["resolved_via"],
                )
            except Exception:
                pass
            if not result["done"] and result["reason"] not in ("proactive_disabled", ""):
                logger.info(
                    f"[persona_agent] proactive poke skipped: {result['reason']}"
                    f" name={result['raw_name']!r}"
                )
        return bool(result["done"])

    # ---------------------------------------------------------- S3 动作链路

    def _sticker_enabled(self) -> bool:
        return int((self.config.get("sticker", {}) or {}).get("enabled", 0)) == 1

    def _ensure_sticker(self) -> None:
        """懒建 StickerService（复用已加载的 BGE，不新增模型）。"""
        if self._sticker is not None:
            return
        scfg = self.config.get("sticker", {}) or {}
        data_dir = str(self.data_dir)
        index_path = str(scfg.get("index_path") or f"{data_dir}/sticker_index.json")
        lib_dir = str(scfg.get("library_dir") or f"{data_dir}/sticker_library")
        try:
            from .services.sticker import StickerService, embed_via_rag
            self._sticker = StickerService(
                index_path,
                embed_via_rag(self.rag),
                library_dir=lib_dir,
                top_k=int(scfg.get("top_k", 5)),
                min_score=float(scfg.get("min_score", 0.65)),
                margin=float(scfg.get("margin", 0.0)),
                high_confidence=float(scfg.get("high_confidence", 0.70)),
            )
            logger.info(
                f"[persona_agent] sticker service ready: {self._sticker.size} 条"
                f" | min_score={self._sticker.snapshot()['min_score']}"
                f" load_error={self._sticker.snapshot().get('load_error') or 'none'}"
            )
        except Exception as e:
            logger.warning(f"[persona_agent] sticker init failed: {e}")
            self._sticker = None

    async def _send_sticker(self, event: AstrMessageEvent, group_id: str, intent):
        """执行 [emote:意图]：选图并**作为一条独立消息**发出。**绝不抛出**。

        为什么用独立消息而非 chain_result：调用方（`on_group_message`）已经
        `yield event.plain_result(...)` 发过正文了 —— 同一个 handler 里再 yield
        一条即"正文一句 + 表情一张"的形态（spec §4.3 的"一句话 + 一张表情"）。
        失败语义：选不中/库空/文件缺失/开关关 → **静默跳过，正文照发**。
        """
        result: dict = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "intent": intent.emote,
            "sent": False,
            "reason": "",
        }
        try:
            if not self._sticker_enabled():
                result["reason"] = "sticker.enabled=0"
            else:
                self._ensure_sticker()
                if self._sticker is None:
                    result["reason"] = "service_unavailable"
                else:
                    pick = await self._sticker.pick(intent.emote)
                    result["top_k"] = pick.top_k[:5]
                    result["via"] = pick.via
                    if pick.hit is None:
                        result["reason"] = pick.reason or "no_hit"
                    else:
                        result["id"] = pick.hit.id
                        result["score"] = pick.hit.score
                        path = pick.hit.path
                        if not os.path.exists(path):
                            result["reason"] = "file_missing"
                        else:
                            # ✅ 宿主源码确认（astrbot/core/message/components.py）：
                            #    fromFileSystem 内部 `Path(path).resolve().as_uri()`
                            #    并带 path= 字段 —— 原生支持本地文件，无需 base64 兜底。
                            result["sent"] = True
                            yield event.chain_result([Comp.Image.fromFileSystem(path)])
        except Exception as e:
            result["reason"] = f"{type(e).__name__}: {e}"
        finally:
            try:
                self.store.append_jsonl(f"logs/{group_id}/sticker_log.jsonl", result)
            except Exception:
                pass
            # 降级必须可见（S0 教训）：只在真的尝试过且没发成时告警，
            # 开关关闭属正常静默、不刷日志。
            if (not result["sent"]) and result["reason"] not in ("sticker.enabled=0", ""):
                logger.info(
                    f"[persona_agent] sticker skipped: {result['reason']}"
                    f" intent={(result.get('intent') or '')[:20]!r}"
                )

    async def _pipeline_generate(
        self,
        user_text: str,
        contexts: list[dict],
        emotion: Optional[EmotionState],
        temperature: Optional[float],
        sender_uin: str,
        umo: Optional[str],
        meta: Optional[dict] = None,
    ) -> str:
        """Pipeline generate callback: standalone LLM call (no event object).

        ``meta``（S16）：可变字典，用于把**思维链**回传给 pipeline。
        思维链**不进 LLM 上下文**，只存进 session 供人工查看（用户要求）。

        Reuses _generate_reply in standalone mode (event=None + explicit
        speaker_uin/umo).
        S0: 同时把 group_id 从 umo 解出来，供 cache probe 落盘 —— 此前
        standalone 模式直接跳过探针，导致线上主回复链路的缓存命中率无数据。
        """
        if self.style is None:
            return ""
        self._probe_group_id = self._group_id_from_umo(umo)
        # S2: 让 _build_turn_block 拿到本轮情绪（回调签名不带 emotion）
        self._turn_block_emotion = emotion
        try:
            return await self._generate_reply(
                None,  # type: ignore[arg-type]  # standalone mode
                user_text,
                contexts,
                emotion,
                temperature,
                speaker_uin=sender_uin,
                umo=umo,
                meta=meta,
            )
        except Exception as e:
            logger.exception(f"[persona_agent] pipeline generate failed: {e}")
            return ""

    @staticmethod
    def _group_id_from_umo(umo: Optional[str]) -> str:
        """从 unified_msg_origin 里取群号：'aiocqhttp:GroupMessage:881438753'。

        PrivateMessage 形如 'aiocqhttp:FriendMessage:337934842' → 返回空字符串
        （私聊不属目标群，不落探针）。
        """
        parts = str(umo or "").split(":")
        if len(parts) >= 3 and parts[1] == "GroupMessage":
            return parts[2]
        return ""

    @staticmethod
    def _postprocess_plain(text: str) -> str:
        """Pipeline postprocess callback (pure text; no state needed)."""
        return text_style.postprocess(text)

    async def _augment_with_vision(self, event: AstrMessageEvent, text: str) -> str:
        """G15: append vision descriptions / face names to the message text."""
        try:
            segs = list(event.get_messages())
            imgs = [seg for seg in segs if isinstance(seg, Comp.Image)][: self._vision_max_images]
            faces = [seg for seg in segs if isinstance(seg, Comp.Face)]
            if not imgs and not faces:
                return text
            extra = ""
            if faces:
                extra += "".join(f"（表情：{face_name(getattr(f, 'id', 0))}）" for f in faces)
            if imgs:
                if self._vision is None:
                    await self._ensure_vision(event)
                if self._vision is not None:
                    descs = []
                    diags: list[dict] = []
                    for seg in imgs:
                        d: dict = {}
                        diags.append(d)
                        try:
                            descs.append(await self._vision.describe_image(seg, diag=d) or "")
                        except Exception as e:
                            d["vision_fail"] = f"{type(e).__name__}: {e}"
                            descs.append("")
                    non_empty = [d for d in descs if d]
                    if non_empty:
                        if len(non_empty) == 1:
                            extra += f"（配图：{non_empty[0]}）"
                        else:
                            extra += "（配图：" + "；".join(
                                f"{i + 1}:{d}" for i, d in enumerate(non_empty)) + "）"
                    else:
                        # Descriptions failed: say so honestly so the reply LLM
                        # never hallucinates content about an unseen image.
                        extra += "（配图：无法识别）"
                        # ★ S0 观测：把"哪一层失败"写进日志。调用方只看到
                        #   「无法识别」这一个结果，底层却有 4 种成因（取不到图 /
                        #   模型空返回 / 异常 / 超时）—— 2026-09-13 实测 5/14 次
                        #   失败，正是因为没有这行日志而只能猜。
                        logger.warning(
                            "[persona_agent] vision failed for all %d image(s): %s"
                            % (len(imgs), json.dumps(diags, ensure_ascii=False)[:400])
                        )
            if not extra:
                return text
            return f"{text} {extra}".strip() if text.strip() else extra
        except Exception as e:
            logger.warning(f"[persona_agent] vision augment failed: {e}")
            return text

    async def _ensure_vision(self, event: AstrMessageEvent) -> None:
        """Lazily resolve api_base/key from the same chat provider source and
        build the VisionService (secrets stay in provider config, never here)."""
        if self._vision is not None or self._vision_resolving:
            return
        self._vision_resolving = True
        try:
            provider_id = await self._resolve_provider_id(event.unified_msg_origin)
            if not provider_id:
                logger.warning("[persona_agent] vision init skipped: no provider id")
                return
            prov = await self.context.provider_manager.get_provider_by_id(provider_id)
            cfg = getattr(prov, "provider_config", None) or {}
            api_base = str(cfg.get("api_base", "") or "")
            keys = cfg.get("key") or []
            if not api_base or not keys:
                logger.warning("[persona_agent] vision init skipped: provider has no api_base/key")
                return
            vcfg = self.config.get("vision", {}) or {}
            # A7③: 持久哈希缓存（跨重启复用图片描述；cache_persist=0 则仅内存 TTL）
            persist_path = None
            if int(vcfg.get("cache_persist", 1)) == 1:
                persist_path = str(self.data_dir / "image_desc_cache.json")
            self._vision = VisionService(
                api_base=api_base,
                api_key=str(keys[0]),
                model=str(vcfg.get("model", "deepseek-v4-flash-vision-exp")),
                timeout=float(vcfg.get("timeout_sec", 15)),
                cache_ttl=float(vcfg.get("cache_ttl_sec", 30)),
                desc_max_chars=int(vcfg.get("desc_max_chars", 120)),
                reasoning_effort=str(vcfg.get("reasoning_effort", "low")),
                persist_path=persist_path,
                persist_max=int(vcfg.get("cache_persist_max", 2000)),
            )
            logger.info(f"[persona_agent] vision service ready (model={self._vision._model})")
        except Exception as e:
            logger.warning(f"[persona_agent] vision init failed: {e}")
        finally:
            self._vision_resolving = False

    def _examples_block(self) -> str:
        """G14: hot-reloadable example-dialog block (A/B = rename the file)."""
        try:
            cfg = self.config.get("examples", {}) or {}
            if int(cfg.get("enabled", 1)) != 1:
                self._examples_state = ExamplesState()
                return ""
            block, state = load_examples_block(
                self.data_dir / "example_dialogs.json",
                max_entries=int(cfg.get("max_entries", 12)),
                prev=self._examples_state,
            )
            self._examples_state = state
            return block
        except Exception:
            return ""

    def _temperature_for(self, trigger: str) -> Optional[float]:
        """G7: per-trigger temperature tiers (v0.2 4.5; dream tier deferred
        until dream has an LLM step)."""
        t = (self.config.get("llm") or {}).get("temperature") or {}
        if trigger == TRIGGER_AT:
            return float(t.get("at_reply", 0.8))
        if trigger == TRIGGER_RAG:
            return float(t.get("active_interjection", 1.0))
        if trigger == TRIGGER_COLD:
            return float(t.get("cold_start", 1.1))
        return None

    def _auto_add_member(self, uin: str, name: str) -> None:
        """G9: append unknown callers to member_relations (new tier)."""
        try:
            if self.style is not None and self.style.add_new_member(uin, name):
                logger.info(f"[persona_agent] auto-added member uin={uin} alias={name or f'群友{uin}'}")
        except Exception as e:
            logger.warning(f"[persona_agent] auto-add member failed: {e}")

    async def _warm_provider_id(self) -> None:
        """S0: 启动时预热 provider 解析（cron 任务不再依赖「先有人 @ 过」）。

        必须**等一下再查**：实测 AstrBot 在插件 ``initialize`` 完成之后才注册
        provider 实例（插件 15:11:07 加载 → provider 15:11:14.678 进 inst_map）。
        过早校验会把"还没加载"误判成"配置不存在"（首版即误伤）。
        故：``verify=False`` + 重试几次；查到就缓存，查不到下轮消息再解析。
        """
        last = "unknown"
        for attempt in range(5):
            await asyncio.sleep(3.0 if attempt == 0 else 5.0)
            try:
                pid = await self._resolve_provider_id(verify=False)
            except Exception as e:
                last = f"error: {e}"
                continue
            if pid:
                ok = await self._provider_exists(pid)
                logger.info(
                    f"[persona_agent] provider resolved at startup: {pid} "
                    f"(exists={ok}; attempts={attempt + 1})"
                )
                return
            last = "unresolved"
        logger.warning(f"[persona_agent] provider warmup gave up ({last}); will resolve lazily")

    async def _provider_exists(self, provider_id: str) -> Optional[bool]:
        """校验 provider_id 在 AstrBot 里真实存在。

        返回 True/False；**无法判定时返回 None**（当作"未知，放行"）。

        两个必须区分的情况（2026-09-13 实测踩到）：
          - **真写错**：id 拼错 → ``llm_generate`` 抛 ``ProviderNotFoundError``
            被 except 吞成空回复 → 静默哑掉。这是要拦的。
          - **AstrBot 还没加载完 provider**：实测插件在 ``15:11:07`` 被加载，
            而 provider 到 ``15:11:14.678`` 才注册进 ``inst_map``。此时查不到
            **不代表配置错** —— 判成"不存在"会误伤（首版就误伤了）。

        判据：``inst_map`` 为空 = 尚未初始化 → None；不为空再查。
        """
        pm = getattr(self.context, "provider_manager", None)
        if pm is None or not hasattr(pm, "get_provider_by_id"):
            return None
        inst_map = getattr(pm, "inst_map", None)
        if isinstance(inst_map, dict) and not inst_map:
            # 一条 provider 都还没注册 → 处于启动早期，无从判断
            return None
        try:
            prov = await pm.get_provider_by_id(provider_id)
        except Exception as e:
            logger.debug(f"[persona_agent] provider check failed for {provider_id}: {e}")
            return None
        return prov is not None

    async def _resolve_provider_id(
        self, umo: Optional[str] = None, *, verify: bool = True
    ) -> Optional[str]:
        """单一 provider 解析收口（S0）。

        此前四处各自读 ``llm.provider_id or self._last_provider_id``，而后者
        **只在真的生成过回复后**才被赋值 → 02:05 的日记 cron 若当天无人 @ 过
        就永远拿不到 provider（实测日志 ``diary skipped: no provider id known
        yet``）。这里统一为：配置值 → 本进程已知值 → AstrBot 当前会话 provider。

        成功解析后缓存到 ``_last_provider_id``，使 cron/异步任务不再依赖
        「先有人触发过一轮回复」。

        S0 加固：``verify=True`` 时**先校验配置值存在性**（写错 id 会静默哑掉）；
        启动预热传 ``verify=False`` —— 那时 provider 可能还没注册完，
        校验只会误判（见 ``_provider_exists`` 的三个返回值）。
        """
        cfg_pid = (self.config.get("llm") or {}).get("provider_id", "") or ""
        exists: Optional[bool] = None
        if cfg_pid and verify:
            exists = await self._provider_exists(cfg_pid)
            if exists is False:
                logger.warning(
                    f"[persona_agent] configured llm.provider_id={cfg_pid!r} "
                    "NOT found in AstrBot; falling back to session provider"
                )
        # 需要回退时才去问 AstrBot 的会话默认 provider（一次异步查询）
        session_default = None
        need_fallback = (not cfg_pid) or (exists is False)
        if need_fallback and not self._last_provider_id:
            try:
                session_default = await self.context.get_current_chat_provider_id(umo or "")
            except Exception as e:
                logger.debug(f"[persona_agent] provider resolve failed: {e}")
                session_default = None
        pid = resolve_provider_id(
            configured=cfg_pid,
            known=self._last_provider_id,
            session_default=session_default,
            configured_exists=exists if verify else None,
        )
        if pid:
            self._last_provider_id = pid
        return pid

    async def _emotion_llm(self, prompt: str) -> str:
        """G10: emotion analysis call (3s timeout enforced by the provider).

        A7④: 结构化 JSON 任务 → 低温(0.2) + 思考 off（配置可调），输出稳定。
        """
        provider = await self._resolve_provider_id()
        if not provider:
            raise RuntimeError("no LLM provider available for emotion")
        ecfg = self.config.get("emotion", {}) or {}
        _erv = reasoning_value(ecfg.get("reasoning_effort", "low"))
        resp = await self.context.llm_generate(
            chat_provider_id=provider,
            prompt=prompt,
            system_prompt=EMOTION_SYSTEM_PROMPT,
            temperature=float(ecfg.get("temperature", 0.2)),
            **({"reasoning_effort": _erv} if _erv else {}),
        )
        return (getattr(resp, "completion_text", "") or "").strip()

    async def _dream_anchor_llm(self, system_prompt: str, prompt: str) -> str:
        """做梦阶段①：锚点抽取（**低温**，结构化任务）。"""
        provider = await self._resolve_provider_id()
        if not provider:
            raise RuntimeError("no LLM provider available for dream anchor")
        dcfg = self.config.get("dream", {}) or {}
        _rv = reasoning_value(dcfg.get("reasoning_effort", "low"))
        resp = await self.context.llm_generate(
            chat_provider_id=provider,
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=float(dcfg.get("anchor_temperature", 0.3)),
            # 🔴 预算给足（2026-09-14 用户指出）：思考 token 是**重尾随机变量**
            # （同一 prompt 实测 556 或 2048），预算紧则被截断 → content 空。
            # `max_tokens` 是**上限而非消耗**，给大没有代价。实测 8192/16384 稳定。
            max_tokens=int(dcfg.get("anchor_max_tokens", 8192)),
            **({"reasoning_effort": _rv} if _rv else {}),
        )
        return (getattr(resp, "completion_text", "") or "").strip()

    async def _dream_llm(self, system_prompt: str, prompt: str) -> str:
        """做梦阶段②：写梦境（**温度 1.3**、思考 low —— 用户 2026-09-14 指定）。

        为什么温度这么高：用户要的是"逻辑较为跳跃的梦境语段"，而梦里不该有
        现实的因果链条。温度低会把梦写成日记的摘要。

        ⚠️ `max_tokens` **给足**：400–700 字正文 + low 档思考，而思考是重尾随机的
        （实测同一 prompt 888 也可能顶到 2048）。`max_tokens` 是上限不是消耗，
        给大没有代价 —— 给紧只会在运气差时截断成空 content（B-019 同形）。
        """
        provider = await self._resolve_provider_id()
        if not provider:
            raise RuntimeError("no LLM provider available for dream")
        dcfg = self.config.get("dream", {}) or {}
        _rv = reasoning_value(dcfg.get("reasoning_effort", "low"))
        resp = await self.context.llm_generate(
            chat_provider_id=provider,
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=float(dcfg.get("temperature", 1.3)),
            max_tokens=int(dcfg.get("max_tokens", 8192)),
            **({"reasoning_effort": _rv} if _rv else {}),
        )
        return (getattr(resp, "completion_text", "") or "").strip()

    async def _gate_llm(
        self, prompt: Optional[str] = None, *, messages=None, system_prompt: Optional[str] = None
    ) -> str:
        """A7/S4: GateLLM 调用。两种形态：

          - 旧（单条）：``_gate_llm(prompt)`` → system=GATE_SYSTEM_PROMPT
          - S4（共享上下文）：``_gate_llm(None, messages=[...], system_prompt=人格提示词)``
            → 与 RP 走**同一份 system prompt 与上下文前缀**，让网关前缀缓存
            被两次调用复用，同时让 Gate 拥有与 RP 同级的判断依据。

        失败以异常上抛给 GateService，由其转为保守静默。
        """
        provider = await self._resolve_provider_id()
        if not provider:
            raise RuntimeError("no LLM provider available for gate")
        gcfg = self.config.get("gate", {}) or {}
        _grv = reasoning_value(gcfg.get("reasoning_effort", "low"))
        sys_p = system_prompt if system_prompt else GATE_SYSTEM_PROMPT
        kwargs = {}
        if messages is not None:
            kwargs["contexts"] = messages
            kwargs["prompt"] = None
        else:
            kwargs["prompt"] = prompt
        resp = await self.context.llm_generate(
            chat_provider_id=provider,
            system_prompt=sys_p,
            temperature=float(gcfg.get("temperature", 0.2)),
            **({"reasoning_effort": _grv} if _grv else {}),
            **kwargs,
        )
        # S15: Gate 也落探针（kind="gate"）—— 否则 Gate 的缓存命中完全不可见，
        # 而 RP/Gate 现在是两条独立前缀，必须分别观测（用户要求"区分开"）。
        if int((self.config.get("llm") or {}).get("cache_probe_enabled", 1)) == 1:
            try:
                _ctx = kwargs.get("contexts") or []
                _gid = str(self._probe_group_id or "")
                if _gid and _ctx:
                    self._log_llm_probe(None, _ctx, sys_p, provider, resp,
                                        self._local_hour(), group_id=_gid,
                                        kind="gate")
            except Exception as _e:
                logger.warning(f"[persona_agent] gate probe log failed: {_e}")
        return (getattr(resp, "completion_text", "") or "").strip()

    def _log_llm_probe(
        self,
        event: Optional[AstrMessageEvent],
        contexts: list[dict],
        sys_prompt: str,
        provider_id: str,
        resp: object,
        local_hour: int,
        group_id: str = "",
        kind: str = "rp",
    ) -> None:
        """Observability probe: session continuity + provider KV/prefix-cache usage.

        S15：``kind`` 区分 LLM 用途（``rp``/``gate``）—— 用户要求"LLM 日志内的
        session ID 也区分开"。RP 与 Gate 现在是**两条不同前缀**（共用历史、
        各用各的 system），混记就无法归属命中/未命中。

        Appends one record to llm_cache_probe.jsonl per generation. Never
        raises; failures only warn. Exists for the 2026-08 evaluation round.

        S0: ``group_id`` 显式传入，使无 event 的 pipeline 主链路也能落探针。
        """
        try:
            gid = group_id or (str(event.get_group_id() or "") if event is not None else "")
            session_size = self.session_mgr.size(gid) if self.session_mgr else -1
            kg_tail_chars = 0
            if contexts and isinstance(contexts[-1], dict) and contexts[-1].get("role") == "system":
                kg_tail_chars = len(str(contexts[-1].get("content", "")))
            total_chars = sum(
                len(str(m.get("content", ""))) if isinstance(m, dict) else 0
                for m in contexts
            )
            usage = getattr(resp, "usage", None)
            usage_d: dict = {}
            if usage is not None:
                usage_d = {
                    "input_other": getattr(usage, "input_other", None),
                    "input_cached": getattr(usage, "input_cached", None),
                    "output": getattr(usage, "output", None),
                }
            raw_usage = None
            raw = getattr(resp, "raw_completion", None)
            if raw is not None:
                try:
                    u = getattr(raw, "usage", None)
                    if u is not None:
                        raw_usage = u.model_dump() if hasattr(u, "model_dump") else u
                except Exception:
                    raw_usage = None
            # S15：精确 token 对账 —— 保存上次同 lineage 请求，算公共前导，
            # 与网关 cached 交叉验证。**唯一无歧义的信号**：前缀没动却 cached=0
            # → 网关侧丢缓存（见 services/token_accounting.py 的说明）。
            try:
                from .services.token_accounting import record_and_compare
                acct = record_and_compare(
                    self.data_dir, kind=kind, group_id=gid, messages=contexts,
                    cached_tokens=(usage_d.get("input_cached")
                                   if isinstance(usage_d.get("input_cached"), int) else None),
                    prompt_tokens=(
                        (usage_d.get("input_cached") or 0)
                        + (usage_d.get("input_other") or 0)
                        if isinstance(usage_d.get("input_cached"), int) else None),
                )
            except Exception as e:
                acct = {"accounting_error": f"{type(e).__name__}: {e}"}

            # S16：**落思维链摘要** —— 用于排查"回复指向错人"（Q1）。
            # 用户判断："可能是突然提出来（和不相关的人名从 2500 条里被捞出来
            # 一样），要进思维链去找原因"。没有它就无法验证该假设。
            # 只存前 1500 字符（reasoning 常是 content 的 3~5 倍，全存会让
            # 探针文件迅速膨胀；排查指向问题前 1500 字足够）。
            # B-029: 同上一处 —— 网关字段名是 reasoning，不是 reasoning_content
            reasoning = extract_reasoning(resp)
            record = {
                "ts": time.time(),
                "reasoning_chars": len(reasoning),
                "reasoning_excerpt": reasoning[:1500],
                "group_id": gid,
                # S15: session ID —— RP/Gate 分开（用户要求）
                "kind": kind,
                "session_id": acct.get("lineage") or f"{kind}:{gid}",
                "accounting": acct,
                "session_size_before": session_size,
                "contexts_len": len(contexts),
                "contexts_chars": total_chars,
                "kg_tail_chars": kg_tail_chars,
                "sys_prompt_len": len(sys_prompt),
                "sys_prompt_hash16": hashlib.sha256(sys_prompt.encode("utf-8")).hexdigest()[:16],
                "local_hour": local_hour,
                "provider_id": provider_id,
                "usage": usage_d,
                "raw_usage": raw_usage,
                "resp_id": getattr(resp, "id", None),
            }
            self.store.append_jsonl(f"logs/{gid}/llm_cache_probe.jsonl", record)
            logger.info(
                f"[persona_agent] cache_probe group={gid} session={session_size} "
                f"ctx={len(contexts)} kg={kg_tail_chars} "
                f"syshash={record['sys_prompt_hash16']} "
                f"input_cached={usage_d.get('input_cached')} raw_usage={'yes' if raw_usage is not None else 'no'}"
            )
        except Exception as e:
            logger.warning(f"[persona_agent] cache probe log failed: {e}")

    # ------------------------------------------------------------ v3 diary/sleep

    def _is_sleeping(self) -> bool:
        """是否处于睡眠（含人工覆盖）。

        🔴 修一个我自己引入的 bug（S12 重构 /admin 时）：`_admin_sleep` 写的是
        **元组** `("sleep", hours)`，而这里比的是**字符串** `"sleep"` ——
        元组永不等于字符串 → **定时睡眠完全不生效**（静默失效，且无报错）。

        统一表达：``_sleep_override`` = ``None`` | ``"awake"`` |
        ``("sleep", None)``（直到醒来）| ``("sleep", 到期时间戳)``。
        """
        ov = self._sleep_override
        if ov == "awake":
            return False
        if isinstance(ov, tuple) and ov and ov[0] == "sleep":
            expire = ov[1] if len(ov) > 1 else None
            if expire is None:
                return True
            if time.time() < float(expire):
                return True
            # 到期 → 自动恢复（下次询问即清除，避免需要手动 /admin wake）
            self._sleep_override = None
            logger.info("[persona_agent] sleep override 已到期，自动恢复睡眠窗规则")
        cfg = self.config.get("sleep", {}) or {}
        if int(cfg.get("enabled", 1)) != 1:
            return False
        start = int(cfg.get("start_hour", self._sleep_start))
        end = int(cfg.get("end_hour", self._sleep_end))
        h = self._local_hour()
        if start <= end:
            return start <= h < end
        return h >= start or h < end

    def _housekeeping(self) -> None:
        """Retention: prune old per-day session files and rotate oversized
        jsonl logs (G3). Best-effort, never raises."""
        try:
            hk = self.config.get("housekeeping", {}) or {}
            keep_days = int(hk.get("session_keep_days", 3))
            max_mb = float(hk.get("jsonl_max_mb", 50))
            today = datetime.datetime.now()  # local wall date for retention
            cutoff = (today - datetime.timedelta(days=keep_days - 1)).strftime("%Y-%m-%d")
            removed = 0
            for f in self.data_dir.glob("session_*_*.json"):
                m = re.search(r"_(\d{4}-\d{2}-\d{2})\.json$", f.name)
                if m and m.group(1) < cutoff:
                    try:
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass
            if removed:
                logger.info(f"[persona_agent] housekeeping removed {removed} old session file(s)")
            for name in ("recent_messages.jsonl", "decision_log.jsonl",
                         "llm_cache_probe.jsonl", "daily_diary.jsonl"):
                path = self.data_dir / name
                try:
                    if path.exists() and path.stat().st_size > max_mb * 1024 * 1024:
                        p1 = Path(str(path) + ".1")
                        p2 = Path(str(path) + ".2")
                        p2.unlink(missing_ok=True)
                        if p1.exists():
                            p1.rename(p2)
                        path.rename(p1)
                        logger.info(f"[persona_agent] housekeeping rotated {name} ({max_mb}MB)")
                except OSError:
                    pass
            # A7 2b: rotate per-group jsonl under logs/<group_id>/
            for path in self.data_dir.glob("logs/*/*.jsonl"):
                try:
                    if path.stat().st_size > max_mb * 1024 * 1024:
                        p1 = Path(str(path) + ".1")
                        p2 = Path(str(path) + ".2")
                        p2.unlink(missing_ok=True)
                        if p1.exists():
                            p1.rename(p2)
                        path.rename(p1)
                        logger.info(f"[persona_agent] housekeeping rotated {path.name} ({max_mb}MB)")
                except OSError:
                    pass
        except Exception as e:
            logger.warning(f"[persona_agent] housekeeping failed: {e}")

    async def _daily_rotation_job(self) -> None:
        """Cron: rotate every group session that crossed the day boundary and
        generate the daily diary from the archived day."""
        if self.session_mgr is None:
            return
        self._housekeeping()
        # S2: 轮转前先落盘所有挂起条目 —— 否则上一轮挂起的消息会被写进
        # **新一天**的会话，出现在错误的日期归档里。
        if self._pipeline is not None:
            try:
                n = self._pipeline.flush_all_pending_appends()
                if n:
                    logger.info(f"[persona_agent] flushed {n} pending session append(s) before rotation")
            except Exception as e:
                logger.warning(f"[persona_agent] pre-rotation flush failed: {e}")
        # S8：日旋转时顺手聚合缓存命中（长期留存产物）
        for _gid in list(self.session_mgr.snapshot().keys()):
            self._update_cache_stats(_gid)
        for gid in list(self.session_mgr.snapshot().keys()):
            old_msgs = self.session_mgr.rotate_if_day_changed(gid)
            if old_msgs:
                logger.info(f"[persona_agent] daily rotation: group={gid} msgs={len(old_msgs)}")
                if self._diary_enabled:
                    asyncio.create_task(self._generate_diary(gid, old_msgs))

    def _update_cache_stats(self, group_id: str) -> None:
        """把前缀缓存探针聚合成**长期留存**的日汇总（用户 2026-09-14 要求）。

        为什么不在每次调用后写：探针是逐次明细且**无轮转**（`trace_log.jsonl`
        已 43 MB），单次 `input_cached` 波动大、看不出趋势。这里在日旋转时
        增量聚合一次，产出 `cache_stats.jsonl`（每次一行，紧凑）与
        `cache_daily.jsonl`（每天一行）——**只增不改、人工与 LLM 都可读**。

        **失败绝不影响主流程**（聚合是旁路观测）。
        """
        try:
            from .tools import cache_stats as _cs
            rc = _cs.main(["--data-dir", str(self.data_dir), "--group", str(group_id)])
            if rc != 0:
                logger.warning(f"[persona_agent] cache_stats 聚合返回 {rc}")
        except Exception as e:
            logger.warning(f"[persona_agent] cache_stats 聚合失败（不影响运行）: {e}")

    async def _generate_diary(self, group_id: str, msgs: list[dict]) -> None:
        """Daily diary summary reusing the archived day session as context.

        Cache-friendly by design (v3 decision): the request prefix
        (fixed persona system prompt + the day's raw messages) is identical to
        the last chat request, so the gateway prefix cache covers it.
        """
        if not msgs:
            return
        try:
            provider = await self._resolve_provider_id()
            if not provider:
                logger.info("[persona_agent] diary skipped: no provider id known yet")
                return
            sys_prompt = self.style.system_prompt() if self.style else ""
            contexts = [dict(m) for m in msgs]
            # 逐轮易变量（时间）同样挪到上下文末尾 —— 保持 system prompt 恒定，
            # 让「归档日会话」这段前缀可被网关缓存复用（v3 设计意图）。
            vol = self.style.volatile_line(local_hour=self._local_hour()) if self.style else ""
            if vol:
                contexts.append({"role": "system", "content": vol})
            contexts.append({
                "role": "user",
                "content": (
                    "请把今天群里发生的事写成一篇简短的日记（100~200字），"
                    "包含主要话题与群友互动，用你的语气和第一人称，不要列条。"
                ),
            })
            resp = await self.context.llm_generate(
                chat_provider_id=provider,
                prompt=None,
                system_prompt=sys_prompt,
                contexts=contexts,
            )
            summary = (getattr(resp, "completion_text", "") or "").strip()
            if not summary or self._is_error_response(summary):
                logger.warning("[persona_agent] diary skipped: empty/error response")
                return
            # 复盘修正（2026-09-13）：day 此前取 ``day_key()``（**轮换后**的新日期），
            # 而 msgs 是**刚被归档的那一天** → 日记日期整体偏移一天。
            # 归档日 = 新日期的前一天（用同一个 day_key 口径回推 24h）。
            record = {
                "day": self.session_mgr.day_key(time.time() - 86400.0),
                "group_id": group_id,
                "summary": summary,
                "n_messages": len(msgs),
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            # 🔴 幂等（2026-09-14 实测事故）：用户在夜间反复重启，**新旧实例交叠**
            # 时同一 cron 被两个进程各跑一次 → 同一天日记写了 2–3 条。
            # 内存标记跨不了进程，判据必须落在**文件**上。
            diary_path = self.store.path(f"logs/{group_id}/daily_diary.jsonl")
            dup = find_jsonl_record(diary_path, {"day": record["day"], "group_id": group_id})
            if dup is not None:
                logger.warning(
                    f"[persona_agent] diary 幂等拦截：{record['day']} 已存在"
                    f"（{len(dup.get('summary') or '')} 字符），本次丢弃"
                    f"（多为多实例/重复 cron 触发）"
                )
                return
            self.store.append_jsonl(f"logs/{group_id}/daily_diary.jsonl", record)
            logger.info(f"[persona_agent] diary written: day={record['day']} n={len(msgs)}")
        except Exception as e:
            logger.warning(f"[persona_agent] diary generation failed: {e}")

    # ----------------------------------------------------------------- G13 summary jobs

    async def _weekly_summary_job(self) -> None:
        """Cron: Monday 02:10 — weekly pyramid summary over daily diaries."""
        await self._period_summary("weekly")

    async def _monthly_summary_job(self) -> None:
        """Cron: 1st 02:15 — monthly summary over daily diaries."""
        await self._period_summary("monthly")

    async def _yearly_summary_job(self) -> None:
        """Cron: 1/1 02:20 — yearly summary over the 12 monthly summaries.

        **月→年可整除**（每年 12 个自然月）故无错位；周报是旁支不进主链
        （周与月不可整除）。理由见 `services/summary.py: yearly_window`。
        """
        await self._period_summary("yearly")

    async def _propose_relations(self, kind: str, gid: str,
                                 diaries: list[dict], period: str) -> None:
        """周报任务内的**关系提升提案**（S12，用户要求嵌在这里）。

        输入 = **旧版群友关系图谱**（用户明确要求）+ 本周日记。
        **不做量化筛选** —— 用户："做成定量有点死板了"，且"日记里出现的人"
        本身就是 LLM 做的定性筛选（"这周谁在我眼里有分量"）。

        产出**整体覆盖**上一批（`ProposalStore.replace`，不拼接）→ 单批内序号稳定，
        `/admin relations apply N` 不需要"序号→稳定 id"翻译层。

        ⚠️ **依赖日记质量**（用户自己指出的耦合）：日记若总只写那几张老面孔，
        新人的熟悉度永远提不上去。提案量长期偏低时应回头查日记，而非怀疑本模块。

        失败只 warning —— 提案是周报的附加产物，不能因它拖垮周报。
        """
        if self.style is None or not diaries:
            return
        try:
            current = self._current_closeness()
            if not current:
                logger.info("[persona_agent] 关系提案跳过：成员表为空")
                return
            provider = await self._resolve_provider_id()
            if not provider:
                logger.warning("[persona_agent] 关系提案跳过：无可用 provider")
                return
            prompt = build_proposal_prompt(self.style.relations_block(),
                                           diaries, current)
            scfg = self.config.get("summary", {}) or {}
            _rv = reasoning_value(scfg.get("reasoning_effort", "low"))
            resp = await self.context.llm_generate(
                chat_provider_id=provider,
                prompt=prompt,
                system_prompt=PROPOSAL_SYSTEM,
                temperature=float(scfg.get("proposal_temperature", 0.3)),
                # 预算给足：思考 token 是重尾随机变量（见 B-019 与 measurements §2b）
                max_tokens=int(scfg.get("proposal_max_tokens", 8192)),
                **({"reasoning_effort": _rv} if _rv else {}),
            )
            raw = (getattr(resp, "completion_text", "") or "").strip()
            proposals = parse_proposals(raw, current)
            batch = ProposalStore(self.data_dir).replace(proposals, period=period)
            logger.info(
                f"[persona_agent] 关系提案已生成：{len(proposals)} 条"
                f"（周期 {period}，替换旧批）"
            )
            if proposals:
                lines = ["\n【关系提案】（需你批准，回复 /admin relations apply 序号）"]
                for p in batch.proposals:
                    lines.append(
                        f"  #{p.index} {p.alias or p.uin} "
                        f"{CLOSENESS_CN.get(p.from_closeness, p.from_closeness)}→"
                        f"{CLOSENESS_CN.get(p.to_closeness, p.to_closeness)}"
                        f"  {p.reason}")
                await self._push_text("".join(lines))
        except Exception as e:
            logger.warning(f"[persona_agent] 关系提案失败（不影响周报）: "
                           f"{type(e).__name__}: {e}")

    async def _push_text(self, text: str) -> bool:
        """把一段文本推送到 admin_binding 会话（失败只 warning）。"""
        try:
            b = self._admin_binding()
            umo = b.get("unified_msg_origin")
            if not umo:
                logger.info("[persona_agent] 未绑定 admin_binding，跳过推送")
                return False
            await self.context.send_message(umo, MessageChain().message(text))
            return True
        except Exception as e:
            logger.warning(f"[persona_agent] 推送失败: {e}")
            return False

    async def _dream_job_runner(self) -> None:
        """做梦 cron 的**真正入口**：生成 → 落盘 → **推送**。

        🔴 修一个"从来没推送过"的缺陷（用户 2026-09-14："上周我没收到做梦内容"）：
        原实现把 `DreamJob.run` 直接挂给 cron，而它**只写
        `style_drift_report.json`、从不推送** —— 所以做梦内容一直没到过用户手上
        （周报/月报有推送，做梦没有）。S13 起 `DreamJob` 已整体删除
        （其"关系变更建议"由 services/familiarity.py 取代）。

        本 runner 负责（S11）：
          1. 从最近 7 个不同 day 的日记（不足按实际）**做梦**
          2. 落 `logs/<gid>/dreams.jsonl`（长期留存 + 供 LLM 消费）
          3. **推送到 dream_binding 的会话**
        另：旧 DreamJob 的"关系变更建议/话题趋势"**不再由做梦承担**
        （用户："DreamJob 是做梦，不应该负责处理熟悉度汇报相关内容"）——
        这里只在日志里记一行，不再作为做梦产物。
        """
        groups = [self.target_group_id] if self.test_mode == 0 else [self.test_group_id]
        for gid in groups:
            if not gid:
                continue
            try:
                res = await self._dream_maker.make(gid)
            except Exception as e:
                logger.warning(f"[persona_agent] dream failed: {type(e).__name__}: {e}")
                continue
            if not res.text:
                logger.info(
                    f"[persona_agent] dream skipped: {res.stats.get('error') or 'empty'}"
                    f" (diaries={res.diaries_used})"
                )
                continue
            try:
                persist_dream(self.data_dir, gid, res)
            except Exception as e:
                logger.warning(f"[persona_agent] dream persist failed: {e}")
            logger.info(
                f"[persona_agent] dream written: days={len(res.days)} "
                f"diaries={res.diaries_used} fragments={res.fragments_used} "
                f"dropped={res.dropped_ratio:.0%} chars={len(res.text)}"
            )
            await self._push_dream(res)

    async def _push_dream(self, res) -> None:
        """把梦推送到绑定会话（失败只 warning）。"""
        try:
            binding = self.store.load_json("dream_binding.json", {}) or {}
            umo = binding.get("unified_msg_origin")
            if not umo:
                logger.info("[persona_agent] dream 未推送：dream_binding 无 unified_msg_origin")
                return
            head = f"【{res.days[0]} ~ {res.days[-1]} 的梦】" if res.days else "【梦】"
            chain = MessageChain().message(f"{head}\n\n{res.text}")
            await self.context.send_message(umo, chain)
            logger.info(f"[persona_agent] dream pushed to {umo}")
        except Exception as e:
            logger.warning(f"[persona_agent] dream push failed: {e}")

    async def _period_summary(self, kind: str) -> None:
        """G13: collect diaries + sampled raw messages -> LLM summary ->
        jsonl append -> push to bind_dream private chat when bound.

        Fully silent when disabled (crons are never registered), when there is
        no data, no provider, or the LLM returns nothing usable.
        """
        if self._summary is None or self.style is None:
            return
        groups = [self.target_group_id] if self.test_mode == 0 else [self.test_group_id]
        for gid in groups:
            if not gid:
                continue
            try:
                collected = self._summary.collect(kind, gid)
            except Exception as ex:
                logger.warning(f"[persona_agent] {kind} summary collect failed: {ex}")
                continue
            if not collected["diaries"] and not collected["samples"]:
                logger.info(f"[persona_agent] {kind} summary: no data for {gid}, skipped")
                continue
            provider = await self._resolve_provider_id()
            if not provider:
                logger.warning(f"[persona_agent] {kind} summary skipped: no provider id")
                continue
            prompt = build_prompt(
                kind, gid, collected["label"], collected["diaries"],
                collected["samples"],
                **({"monthlies": collected.get("monthlies") or []}
                   if kind == "yearly" else {}),
            )
            sys_prompt = self.style.system_prompt()
            try:
                resp = await self.context.llm_generate(
                    chat_provider_id=provider,
                    prompt=None,
                    system_prompt=sys_prompt,
                    contexts=[{"role": "user", "content": prompt}],
                )
            except Exception as ex:
                logger.warning(f"[persona_agent] {kind} summary LLM failed: {ex}")
                continue
            text = (getattr(resp, "completion_text", "") or "").strip()
            if not text or self._is_error_response(text):
                logger.warning(f"[persona_agent] {kind} summary empty/error, skipped")
                continue
            # 🔴 幂等（同上）：实测 `weekly_summary.jsonl` 出现 2 条 2026-W37
            # → 用户私聊**收到两份周报**。同周期已存在则丢弃本次。
            out_path = self._summary.output_path(kind)
            existing = find_jsonl_record(
                out_path, {"kind": kind, "group_id": gid, "period": collected["label"]}
            )
            if existing is not None:
                logger.warning(
                    f"[persona_agent] {kind} 幂等拦截：{collected['label']} 已存在"
                    f"（{len(existing.get('summary') or '')} 字符）→ 本次丢弃，"
                    f"**不重复推送**（多为多实例/重复 cron 触发）"
                )
                continue
            record = append_summary(
                out_path,
                kind,
                gid,
                collected["label"],
                text,
                {"n_diaries": collected["n_diaries"], "n_samples": collected["n_samples"]},
            )
            logger.info(
                f"[persona_agent] {kind} summary written: group={gid} "
                f"period={record['period']} n_diaries={record['n_diaries']} "
                f"n_samples={record['n_samples']}"
            )
            # 推送到**合并后的** admin_binding（S12：推送目标 = 权限来源）
            head = {"weekly": "周记", "monthly": "月记",
                    "yearly": "年记"}.get(kind, kind)
            pushed = await self._push_text(
                f"【{record['period']} {head}】\n{record['summary']}")

            # S12: 周报任务内顺带产出**关系提升提案**（用户要求嵌在这里）。
            # ⚠️ 放在推送之后 —— 提案是附加产物，不能拖慢/拖垮周报本身。
            if kind == "weekly":
                await self._propose_relations(
                    kind, gid, collected.get("diaries") or [], collected["label"])
            self._log_decision({
                "action": f"{kind}_summary",
                "trigger": "cron",
                "reason": "period summary",
                "score": 0.0,
                "hour": self._local_hour(),
                "hourly_budget": 0.0,
                "hourly_used": 0.0,
                "silence_sec": 0.0,
                "cooldown_left_sec": 0.0,
                "extra": {
                    "kind": kind,
                    "group_id": gid,
                    "period": record["period"],
                    "n_diaries": record["n_diaries"],
                    "n_samples": record["n_samples"],
                    "pushed": pushed,
                },
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "sender_uin": "cron",
            })

    # ----------------------------------------------------------------- misc

    def _local_hour(self) -> int:
        offset = int((self.config.get("interjection") or {}).get("local_tz_offset_hours", 8))
        return int(((time.time() / 3600.0) + offset) % 24)

    @staticmethod
    def _is_error_response(text: str) -> bool:
        t = text.strip()
        if not t:
            return True
        if t.startswith("{"):
            try:
                obj = json.loads(t)
                if isinstance(obj, dict):
                    if {"error_code", "error_name", "cloudflare_error", "error_category"} & set(obj.keys()):
                        return True
                    if "type" in obj and "error" in str(obj["type"]).lower():
                        return True
                    if isinstance(obj.get("status"), int) and obj["status"] >= 400:
                        return True
            except json.JSONDecodeError:
                pass
        return False

    @staticmethod
    def _strip_at_mentions(text: str) -> str:
        return text_style.strip_at_mentions(text)

    @staticmethod
    def _strip_meta_parens(text: str) -> str:
        return text_style.strip_meta_parens(text)

    @staticmethod
    def _strip_emoji(text: str) -> str:
        return text_style.strip_emoji(text)

    @staticmethod
    def _cap_koupi(text: str) -> str:
        return text_style.cap_koupi(text)

    @staticmethod
    def _extract_quote(text: str) -> tuple[str, Optional[int]]:
        return text_style.extract_quote(text)

    @staticmethod
    def _postprocess(text: str) -> str:
        return text_style.postprocess(text)
