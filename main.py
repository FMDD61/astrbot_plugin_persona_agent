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
from .services.style_profile import StyleProfile
from .services.rag_service import RagService
from .services.interjection import (
    InterjectionManager,
    TRIGGER_AT,
    TRIGGER_RAG,
    ACTION_REPLY,
    ACTION_TOPIC,
    ACTION_SILENT,
)
from .services.json_store import JsonStore
from .services.context_buffer import ContextBuffer
from .services.session_manager import SessionManager
from .services.kg_provider import KGProvider, MultiSignalKGProvider, KGContext
from .services.emotion import EmotionProvider, DefaultEmotionProvider, EmotionState, LLMEmotionProvider, EMOTION_SYSTEM_PROMPT
from .services.vision import VisionService, face_name
from .services.examples import load_examples_block, ExamplesState
from .services.memory_store import MemoryStore, MemoryEvent
from .services.dream_job import DreamJob
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
        self._memory_store: Optional[MemoryStore] = None
        self._dream_job: Optional[DreamJob] = None
        self._conflict_detector: Optional[ConflictDetector] = None
        self._generating: dict[str, bool] = {}

        self._decision_log_path = self.data_dir / "decision_log.jsonl"

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
        self._memory_store = MemoryStore(str(self.data_dir))
        self.kg_provider = MultiSignalKGProvider(
            rag=self.rag,
            store=self._memory_store,
            k_retrieve=int(rag_cfg.get("k_retrieve", 8)),
            top_n_final=int(rag_cfg.get("top_n_final", 3)),
            max_chars=int(rag_cfg.get("max_example_chars", 400)),
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
        self._dream_job = DreamJob(self._memory_store, str(self.data_dir))

        dream_cfg = self.config.get("dream", {}) or {}
        if int(dream_cfg.get("enabled", 0)) == 1:
            if self.context.cron_manager is not None:
                await self.context.cron_manager.add_basic_job(
                    name="persona_dream_job",
                    cron_expression="0 3 * * 1",
                    handler=self._dream_job.run,
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

        # v3: sleep window + diary (rotation at 02:00 inside the sleep window)
        sleep_cfg = self.config.get("sleep", {}) or {}
        self._sleep_enabled = int(sleep_cfg.get("enabled", 1)) == 1
        self._sleep_start = int(sleep_cfg.get("start_hour", 2))
        self._sleep_end = int(sleep_cfg.get("end_hour", 7))
        diary_cfg = self.config.get("diary", {}) or {}
        self._diary_enabled = int(diary_cfg.get("enabled", 1)) == 1
        self._last_provider_id: Optional[str] = None

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

        # Pre-warm RAG off the event loop so the first group message never
        # stalls on BGE/chroma lazy init (2026-08-22 watchdog incident).
        warmed = await asyncio.to_thread(self.rag.warmup)
        logger.info(f"[persona_agent] RAG warmup {'OK' if warmed else 'FAILED (will retry lazily)'}")

        logger.info(
            f"[persona_agent] ready: target_group={self.target_group_id} "
            f"test_mode={self.test_mode} test_group={self.test_group_id} "
            f"bot={self.bot_qq} style_src={self.style_source_qq} "
            f"reply_on_at={self.config.get('reply_on_at')} "
            f"active_interjection={self.config.get('active_interjection')}"
        )

    async def terminate(self) -> None:
        logger.info("[persona_agent] terminating")
        if self.session_mgr is not None:
            try:
                self.session_mgr.save_all()
            except Exception as e:
                logger.warning(f"[persona_agent] session save on terminate failed: {e}")

    # ----------------------------------------------------------------- helpers

    def _is_target_group(self, event: AstrMessageEvent) -> bool:
        gid = event.get_group_id()
        if gid is None:
            return False
        if self.test_mode == 1:
            return str(gid) == self.test_group_id
        return str(gid) == self.target_group_id

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
            self.store.append_jsonl("decision_log.jsonl", payload)
        except Exception as e:  # never let logging crash the handler
            logger.warning(f"[persona_agent] decision log write failed: {e}")

    async def _notify_admin(self, context_summary: str, group_id: str, speaker: str) -> None:
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

    @filter.command("persona_status")
    async def cmd_status(self, event: AstrMessageEvent):
        cfg = self.config
        lines = [
            "=== persona_agent status ===",
            f"target_group     : {self.target_group_id}",
            f"bot_qq           : {self.bot_qq}",
            f"style_source_qq  : {self.style_source_qq}",
            f"reply_on_at      : {cfg.get('reply_on_at')}",
            f"active_interjection : {cfg.get('active_interjection')}",
            f"topic_bank.enabled  : {(cfg.get('topic_bank') or {}).get('enabled', 0)}",
            f"poke.enabled        : {(cfg.get('poke') or {}).get('enabled', 0)}",
            f"dream.enabled       : {(cfg.get('dream') or {}).get('enabled', 0)}",
            f"data_dir         : {self.data_dir}",
        ]
        if self.interjection is not None:
            snap = self.interjection.snapshot()
            lines.append(f"hourly_used      : {snap['hourly_used']:.2f}  hour={snap['current_hour']}")
        if self.session_mgr is not None:
            snap = self.session_mgr.snapshot()
            for gid, sz in snap.items():
                lines.append(f"session[{gid}]  : {sz} msgs")
        yield event.plain_result("\n".join(lines))

    @filter.command("reload_persona_config")
    async def cmd_reload(self, event: AstrMessageEvent):
        """Apply config toggles to the live InterjectionManager.

        The 7 editable JSON files (style profile etc.) auto-hot-reload via
        mtime; this command only rebinds the interjection toggles.
        """
        if self.interjection is None:
            yield event.plain_result("插件尚未完成初始化。")
            return
        topic_cfg = self.config.get("topic_bank", {}) or {}
        self.interjection.update_toggles(
            active_interjection=int(self.config.get("active_interjection", 0)),
            reply_on_at=int(self.config.get("reply_on_at", 1)),
            topic_bank_enabled=int(topic_cfg.get("enabled", 0)),
        )
        yield event.plain_result(
            f"已重载: reply_on_at={self.config.get('reply_on_at')} "
            f"active_interjection={self.config.get('active_interjection')} "
            f"topic_bank.enabled={topic_cfg.get('enabled', 0)}"
        )

    @filter.command("bind_dream")
    async def cmd_bind_dream(self, event: AstrMessageEvent):
        """Bind the current private-chat UMO for weekly dream delivery."""
        gid = event.get_group_id()
        if gid:
            yield event.plain_result("请在私聊中执行 /bind_dream。")
            return
        umo = event.unified_msg_origin
        self.store.save_json("dream_binding.json", {
            "unified_msg_origin": umo,
            "bound_at": int(time.time()),
            "sender_id": str(event.get_sender_id() or ""),
        })
        yield event.plain_result(f"已绑定私聊会话: {umo}")

    @filter.command("bind_admin")
    async def cmd_bind_admin(self, event: AstrMessageEvent):
        """Bind the current private-chat UMO for admin conflict notifications."""
        gid = event.get_group_id()
        if gid:
            yield event.plain_result("请在私聊中执行 /bind_admin。")
            return
        umo = event.unified_msg_origin
        self.store.save_json("admin_binding.json", {
            "unified_msg_origin": umo,
            "bound_at": int(time.time()),
            "sender_id": str(event.get_sender_id() or ""),
        })
        yield event.plain_result(f"已绑定管理员通知会话: {umo}")

    @filter.command("dream_now")
    async def cmd_dream_now(self, event: AstrMessageEvent):
        sender_uin = str(event.get_sender_id() or "")
        privileged = bool(self.privileged_qq) and sender_uin == self.privileged_qq
        if int((self.config.get("dream") or {}).get("enabled", 0)) != 1 and not privileged:
            yield event.plain_result("dream.enabled=0，已禁用做梦。")
            return
        if privileged:
            logger.info(f"[persona_agent] privileged /dream_now from {sender_uin}")
        if self._dream_job is None:
            yield event.plain_result("DreamJob 尚未初始化。")
            return
        try:
            report = await asyncio.to_thread(self._dream_job.run)
            yield event.plain_result(
                f"DreamJob 完成: 升级建议 {len(report.suggested_upgrades)} 人, "
                f"降级建议 {len(report.suggested_downgrades)} 人, "
                f"话题趋势 {len(report.topic_trends)} 个, "
                f"分析 {report.stats.get('members_analyzed', 0)} 人."
            )
        except Exception as e:
            logger.exception(f"[persona_agent] DreamJob.run() failed: {e}")
            yield event.plain_result(f"DreamJob 失败: {e}")

    # ----------------------------------------------------------------- events

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
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
        old_msgs = self.session_mgr.rotate_if_day_changed(group_id)
        if old_msgs and self._diary_enabled:
            asyncio.create_task(self._generate_diary(group_id, old_msgs))

        alias = self.style.preferred_alias(sender_uin) or f"群友{sender_uin}"
        if alias.startswith("群友") and sender_uin and sender_uin != self.bot_qq:
            # G9: unknown caller -> append a 'new' member entry (async, safe)
            sender_name = str(event.get_sender_name() or "")
            asyncio.create_task(asyncio.to_thread(self._auto_add_member, str(sender_uin), sender_name))
        self.session_mgr.append(group_id, "user", text, name=alias)

        # v3: sleep window — bot stays silent (mimics human rest) but the
        # message has already joined the session/memory for the new day.
        if self._sleep_enabled and self._is_sleeping():
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
            })
            event.stop_event()
            return

        if self._memory_store is not None:
            asyncio.create_task(asyncio.to_thread(
                self._memory_store.ingest,
                MemoryEvent(speaker_alias=alias, text=text, group_id=group_id),
            ))

        if self._conflict_detector is not None:
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

        live_ctx = self.buffer.format_recent(max_lines=20) if self.buffer else ""

        top_score = 0.0
        hits: list[dict] = []
        rag_cfg = self.config.get("rag", {}) or {}
        try:
            # G1: BGE embed + chroma search are CPU-bound; keep them off the loop.
            hits = await asyncio.to_thread(
                self.rag.query,
                live_ctx + ("\n" + text if text else ""),
                k=int(rag_cfg.get("k_retrieve", 8)),
                top_n_final=int(rag_cfg.get("top_n_final", 3)),
            )
            if hits:
                top_score = float(hits[0].get("score", 0.0))
        except Exception as e:
            logger.warning(f"[persona_agent] RAG query failed: {e}")

        last_msg_ts = self.buffer.last_ts() if self.buffer else time.time()
        emotion_state = await self._emotion.query(
            group_id,
            self.session_mgr.recent(group_id, n=20),
            kg_ctx=KGContext(
                recent_messages=self.session_mgr.recent(group_id, n=20),
                current_speaker=alias,
                current_text=text,
                group_id=group_id,
            ),
        ) if self._emotion else EmotionState.neutral()
        decision = self.interjection.decide(
            now_utc=time.time(),
            is_at_me=is_at,
            sender_uin=sender_uin,
            last_group_msg_ts=last_msg_ts,
            top_rag_score=top_score,
            emotion_multiplier=emotion_state.global_willingness,
        )
        self._log_decision(decision.to_log(time.time(), sender_uin))

        if decision.action == ACTION_SILENT:
            event.stop_event()
            return

        if decision.action == ACTION_TOPIC:
            logger.info("[persona_agent] topic action requested, but topic_bank not implemented yet")
            event.stop_event()
            return

        self._generating[group_id] = True
        try:
            await asyncio.sleep(0.5)

            kg_result = None
            try:
                recent = self.session_mgr.recent(group_id, n=20)
                kg_result = await self.kg_provider.query(KGContext(
                    recent_messages=recent,
                    current_speaker=alias,
                    current_text=text,
                    group_id=group_id,
                ))
            except Exception as e:
                logger.warning(f"[persona_agent] KG query failed: {e}")

            contexts = self.session_mgr.get_contexts(group_id)
            # G14: fixed example dialogs between session and KG tail
            # (constant content -> prefix cache stays stable).
            ex_block = self._examples_block()
            if ex_block:
                contexts.append({"role": "system", "content": ex_block})
            if kg_result and kg_result.content:
                contexts.append({"role": "system", "content": kg_result.content})

            try:
                reply_text = await self._generate_reply(
                    event, text, contexts, emotion_state,
                    temperature=self._temperature_for(decision.trigger),
                )
            except Exception as e:
                logger.exception(f"[persona_agent] LLM generation failed: {e}")
                event.stop_event()
                return

            clean_text, quote_n = text_style.extract_quote(reply_text)
            reply_text = self._postprocess(clean_text)
            if not reply_text:
                logger.info("[persona_agent] empty reply after postprocess; skipping send")
                event.stop_event()
                return

            # G2: [r:-N] -> OneBot reply chain (needs a real group message id)
            qid = None
            if quote_n is not None and self.buffer is not None:
                qid = self.buffer.quote_target(quote_n)
            if qid:
                yield event.chain_result([Comp.Reply(id=qid), Comp.Plain(reply_text)])
            else:
                yield event.plain_result(reply_text)

            if emotion_state.sticker_prompt:
                try:
                    yield event.chain_result([Comp.Image.fromText(emotion_state.sticker_prompt)])
                except Exception:
                    pass

            self.interjection.register_reply(
                now_utc=time.time(),
                trigger=decision.trigger,
                sender_uin=sender_uin,
            )

            self.session_mgr.append(group_id, "assistant", reply_text)

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
        finally:
            self._generating[group_id] = False

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.OTHER_MESSAGE)
    async def on_other(self, event: AstrMessageEvent):
        """Poke notice handler (skeleton — sub-agent D will fill)."""
        if int((self.config.get("poke") or {}).get("enabled", 0)) != 1:
            return
        raw = getattr(event.message_obj, "raw_message", None)
        if not raw:
            return
        try:
            if (raw.get("notice_type") == "notify"
                    and raw.get("sub_type") == "poke"
                    and str(raw.get("target_id")) == self.bot_qq):
                poker = str(raw.get("user_id"))
                logger.info(f"[persona_agent] poked by {poker} (handler not implemented)")
        except AttributeError:
            return

    # ----------------------------------------------------------------- LLM

    async def _generate_reply(
        self,
        event: AstrMessageEvent,
        user_text: str,
        contexts: list[dict],
        emotion: Optional[EmotionState] = None,
        temperature: Optional[float] = None,
    ) -> str:
        local_hour = self._local_hour()
        sys_prompt = self.style.system_prompt(local_hour=local_hour) if self.style else ""
        if emotion and emotion.current_mood:
            sys_prompt = f"{sys_prompt}\n\n{emotion.current_mood}"

        # Dynamic current-speaker hint (2026-08-23 fix): inserted BEFORE the
        # KG tail (KG stays the last message -> cache prefix untouched; line is
        # per-speaker constant so it is stable across consecutive messages).
        speaker_uin = str(event.get_sender_id() or "")
        alias_txt = ""
        if self.style is not None:
            try:
                alias_txt = self.style.preferred_alias(speaker_uin) or ""
            except Exception:
                alias_txt = ""
        if not alias_txt:
            alias_txt = f"群友{speaker_uin}"
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

        provider_id = (self.config.get("llm") or {}).get("provider_id", "") or None
        if not provider_id:
            try:
                provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
            except Exception:
                provider_id = None

        if not provider_id:
            logger.warning("[persona_agent] no LLM provider available")
            return ""
        self._last_provider_id = provider_id

        gen_kwargs = {}
        if temperature is not None:
            gen_kwargs["temperature"] = temperature
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=None,
                system_prompt=sys_prompt,
                contexts=contexts,
                **gen_kwargs,
            )
        except Exception as e:
            logger.exception(f"[persona_agent] llm_generate raised: {e}")
            return ""

        if int((self.config.get("llm") or {}).get("cache_probe_enabled", 1)) == 1:
            self._log_llm_probe(event, contexts, sys_prompt, provider_id, resp, local_hour)

        text = (getattr(resp, "completion_text", "") or "").strip()
        if self._is_error_response(text):
            logger.warning(f"[persona_agent] llm returned error response, suppressed ({len(text)} chars)")
            return ""
        return text

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
                    for seg in imgs:
                        try:
                            descs.append(await self._vision.describe_image(seg) or "")
                        except Exception:
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
            provider_id = (self.config.get("llm") or {}).get("provider_id", "") or self._last_provider_id
            if not provider_id:
                try:
                    provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
                except Exception:
                    provider_id = None
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
            self._vision = VisionService(
                api_base=api_base,
                api_key=str(keys[0]),
                model=str(vcfg.get("model", "deepseek-v4-flash-vision-exp")),
                timeout=float(vcfg.get("timeout_sec", 15)),
                cache_ttl=float(vcfg.get("cache_ttl_sec", 30)),
                desc_max_chars=int(vcfg.get("desc_max_chars", 120)),
                reasoning_effort=str(vcfg.get("reasoning_effort", "low")),
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

    async def _emotion_llm(self, prompt: str) -> str:
        """G10: emotion analysis call (3s timeout enforced by the provider)."""
        provider = (self.config.get("llm") or {}).get("provider_id", "") or self._last_provider_id
        if not provider:
            raise RuntimeError("no LLM provider available for emotion")
        resp = await self.context.llm_generate(
            chat_provider_id=provider,
            prompt=prompt,
            system_prompt=EMOTION_SYSTEM_PROMPT,
        )
        return (getattr(resp, "completion_text", "") or "").strip()

    def _log_llm_probe(
        self,
        event: AstrMessageEvent,
        contexts: list[dict],
        sys_prompt: str,
        provider_id: str,
        resp: object,
        local_hour: int,
    ) -> None:
        """Observability probe: session continuity + provider KV/prefix-cache usage.

        Appends one record to llm_cache_probe.jsonl per generation. Never
        raises; failures only warn. Exists for the 2026-08 evaluation round.
        """
        try:
            gid = str(event.get_group_id() or "")
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
            record = {
                "ts": time.time(),
                "group_id": gid,
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
            self.store.append_jsonl("llm_cache_probe.jsonl", record)
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
        h = self._local_hour()
        if self._sleep_start <= self._sleep_end:
            return self._sleep_start <= h < self._sleep_end
        return h >= self._sleep_start or h < self._sleep_end

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
        except Exception as e:
            logger.warning(f"[persona_agent] housekeeping failed: {e}")

    async def _daily_rotation_job(self) -> None:
        """Cron: rotate every group session that crossed the day boundary and
        generate the daily diary from the archived day."""
        if self.session_mgr is None:
            return
        self._housekeeping()
        for gid in list(self.session_mgr.snapshot().keys()):
            old_msgs = self.session_mgr.rotate_if_day_changed(gid)
            if old_msgs:
                logger.info(f"[persona_agent] daily rotation: group={gid} msgs={len(old_msgs)}")
                if self._diary_enabled:
                    asyncio.create_task(self._generate_diary(gid, old_msgs))

    async def _generate_diary(self, group_id: str, msgs: list[dict]) -> None:
        """Daily diary summary reusing the archived day session as context.

        Cache-friendly by design (v3 decision): the request prefix
        (fixed persona system prompt + the day's raw messages) is identical to
        the last chat request, so the gateway prefix cache covers it.
        """
        if not msgs:
            return
        try:
            if not self._last_provider_id:
                logger.info("[persona_agent] diary skipped: no provider id known yet")
                return
            sys_prompt = self.style.system_prompt(local_hour=self._local_hour()) if self.style else ""
            contexts = [dict(m) for m in msgs]
            contexts.append({
                "role": "user",
                "content": (
                    "请把今天群里发生的事写成一篇简短的日记（100~200字），"
                    "包含主要话题与群友互动，用你的语气和第一人称，不要列条。"
                ),
            })
            resp = await self.context.llm_generate(
                chat_provider_id=self._last_provider_id,
                prompt=None,
                system_prompt=sys_prompt,
                contexts=contexts,
            )
            summary = (getattr(resp, "completion_text", "") or "").strip()
            if not summary or self._is_error_response(summary):
                logger.warning("[persona_agent] diary skipped: empty/error response")
                return
            record = {
                "day": self.session_mgr.day_key(),
                "group_id": group_id,
                "summary": summary,
                "n_messages": len(msgs),
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            self.store.append_jsonl("daily_diary.jsonl", record)
            logger.info(f"[persona_agent] diary written: day={record['day']} n={len(msgs)}")
        except Exception as e:
            logger.warning(f"[persona_agent] diary generation failed: {e}")

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
