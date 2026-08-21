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
from .services.emotion import EmotionProvider, DefaultEmotionProvider, EmotionState
from .services.memory_store import MemoryStore, MemoryEvent
from .services.dream_job import DreamJob
from .services.conflict_detector import ConflictDetector

_RE_QUOTE_BLOCK = re.compile(r'\[引用消息\(.+?\)\]', re.DOTALL)
_RE_AT_MARKER = re.compile(r'\[At:\d+\]')

_RE_EMOJI = re.compile(
    "["
    "\U0001F300-\U0001F9FF"   # Misc Symbols, Pictographs, Emoticons, Supplemental
    "\U0001FA00-\U0001FAFF"   # Symbols and Pictographs Extended-A
    "\U00002600-\U000027BF"   # Misc Symbols + Dingbats
    "\U0000FE0F\U0000200D"    # Variation Selector + ZWJ
    "\U0001F1E0-\U0001F1FF"   # Regional Indicator Symbols
    "\U00002B50\U00002764"    # ⭐ ❤
    "]"
)
_RE_AT_USER = re.compile(r'(?<!\w)@\S+')
_RE_PAREN_META = re.compile(
    r'[（(]\s*'
    r'(?:\d{5,}'                   # QQ number (5+ digits)
    r'|day\s*\d+'                 # day counter
    r'|第?\d+\s*天'              # 第N天 / N天
    r'|群地位[↑↓]+'              # status tracker
    r'|\d+/\d+'                   # fraction
    r'|\b\d{2,4}\b'              # standalone number 2-4 digits
    r')'
    r'\s*[）)]'
)

_KOUPI_LIST = ("啃啃", "搓搓", "呜嘿", "bakabaka", "钨钼钨钼", "嗷呜", "捏猫猫的")
_KOUPI_MAX_TOTAL = 2


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

        self.session_mgr = SessionManager(
            data_dir=str(self.data_dir),
            max_messages=int(cb_cfg.get("session_max_messages", 300)),
        )
        self._memory_store = MemoryStore(str(self.data_dir))
        self.kg_provider = MultiSignalKGProvider(
            rag=self.rag,
            store=self._memory_store,
            k_retrieve=int(rag_cfg.get("k_retrieve", 8)),
            top_n_final=int(rag_cfg.get("top_n_final", 3)),
            max_chars=int(rag_cfg.get("max_example_chars", 400)),
        )
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

        logger.info(
            f"[persona_agent] ready: target_group={self.target_group_id} "
            f"test_mode={self.test_mode} test_group={self.test_group_id} "
            f"bot={self.bot_qq} style_src={self.style_source_qq} "
            f"reply_on_at={self.config.get('reply_on_at')} "
            f"active_interjection={self.config.get('active_interjection')}"
        )

    async def terminate(self) -> None:
        logger.info("[persona_agent] terminating")

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
        t = _RE_QUOTE_BLOCK.sub("", text)
        t = _RE_AT_MARKER.sub("", t)
        return t.strip()

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
        if int((self.config.get("dream") or {}).get("enabled", 0)) != 1:
            yield event.plain_result("dream.enabled=0，已禁用做梦。")
            return
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

        alias = self.style.preferred_alias(sender_uin) or f"群友{sender_uin}"
        self.session_mgr.append(group_id, "user", text, name=alias)

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
            return

        live_ctx = self.buffer.format_recent(max_lines=20) if self.buffer else ""

        top_score = 0.0
        hits: list[dict] = []
        rag_cfg = self.config.get("rag", {}) or {}
        try:
            hits = self.rag.query(
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
            if kg_result and kg_result.content:
                contexts.append({"role": "system", "content": kg_result.content})

            try:
                reply_text = await self._generate_reply(event, text, contexts, emotion_state)
            except Exception as e:
                logger.exception(f"[persona_agent] LLM generation failed: {e}")
                return

            reply_text = self._postprocess(reply_text)
            if not reply_text:
                logger.info("[persona_agent] empty reply after postprocess; skipping send")
                return

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
    ) -> str:
        local_hour = self._local_hour()
        sys_prompt = self.style.system_prompt(local_hour=local_hour) if self.style else ""
        if emotion and emotion.current_mood:
            sys_prompt = f"{sys_prompt}\n\n{emotion.current_mood}"

        provider_id = (self.config.get("llm") or {}).get("provider_id", "") or None
        if not provider_id:
            try:
                provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
            except Exception:
                provider_id = None

        if not provider_id:
            logger.warning("[persona_agent] no LLM provider available")
            return ""

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=None,
                system_prompt=sys_prompt,
                contexts=contexts,
            )
        except Exception as e:
            logger.exception(f"[persona_agent] llm_generate raised: {e}")
            return ""

        self._log_llm_probe(event, contexts, sys_prompt, provider_id, resp, local_hour)

        text = (getattr(resp, "completion_text", "") or "").strip()
        if self._is_error_response(text):
            logger.warning(f"[persona_agent] llm returned error response, suppressed ({len(text)} chars)")
            return ""
        return text

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
        return _RE_AT_USER.sub("", text)

    @staticmethod
    def _strip_meta_parens(text: str) -> str:
        return _RE_PAREN_META.sub("", text)

    @staticmethod
    def _strip_emoji(text: str) -> str:
        return _RE_EMOJI.sub("", text)

    @staticmethod
    def _cap_koupi(text: str) -> str:
        occurrences: list[tuple[int, int]] = []
        for phrase in _KOUPI_LIST:
            idx = 0
            while True:
                pos = text.find(phrase, idx)
                if pos == -1:
                    break
                occurrences.append((pos, len(phrase)))
                idx = pos + len(phrase)
        if len(occurrences) <= _KOUPI_MAX_TOTAL:
            return text
        occurrences.sort(key=lambda x: x[0])
        parts: list[str] = []
        prev_end = 0
        for i, (pos, length) in enumerate(occurrences):
            parts.append(text[prev_end:pos])
            if i < _KOUPI_MAX_TOTAL:
                parts.append(text[pos:pos + length])
            prev_end = pos + length
        parts.append(text[prev_end:])
        return "".join(parts)

    @staticmethod
    def _postprocess(text: str) -> str:
        if not text:
            return ""
        bad = [
            "作为一个AI", "作为AI", "作为一名AI", "作为人工智能", "我是AI", "我是一个AI",
            "作为助手", "作为大模型", "作为语言模型", "我是一个大模型", "我是语言模型",
        ]
        out = text.strip()
        for b in bad:
            out = out.replace(b, "")
        out = PersonaAgent._strip_at_mentions(out)
        out = PersonaAgent._strip_meta_parens(out)
        out = re.sub(r"(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])", "", out)
        out = PersonaAgent._cap_koupi(out)
        out = PersonaAgent._strip_emoji(out)
        lines = out.splitlines()
        if len(lines) > 8:
            lines = lines[:8]
        out = "\n".join(lines).strip()
        if len(out) > 400:
            out = out[:400].rstrip()
        return out
