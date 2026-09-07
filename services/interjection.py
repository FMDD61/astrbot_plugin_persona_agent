"""InterjectionManager — decide whether the bot should speak right now.

Spec source: IMPLEMENTATION_PLAN.md §9.

Priorities (highest first):
  1. AT       — the bot was @-mentioned. Gated by `reply_on_at` (0/1).
  2. RAG_HIT  — top RAG score >= rag_score_threshold AND the group context
                is "live" (most recent message younger than `silence_cap_sec`).
  3. COLD     — silence_sec >= cold_start_threshold AND topic_bank enabled.
                We don't pick the topic here; we only flag the slot.
  4. SILENT   — otherwise.

Master gate:
  `active_interjection == 0`  ->  always SILENT (except AT, which is governed
                                  by `reply_on_at` only — see §1/§9).
  `reply_on_at == 0`          ->  AT trigger collapses to SILENT.

Hourly budget:
  Consumes from `StyleProfile.hourly_budget(local_hour)`. We keep a per-hour
  counter `hourly_used[hour]`; when the next decision crosses an hour boundary
  the old slot is reset. AT replies always pass (they are user-driven and
  should never be silently dropped due to budget), but they still register in
  `hourly_used` so heavy @-traffic doesn't also unlock active interjections.

Cooldowns:
  - `min_gap_sec` between any two bot messages.
  - `at_cooldown_sec` between two AT replies to the same user_id.

GROUP ISOLATION (2026-09-07, A7 review):
  All usage state (last reply ts / hourly counter / at-cooldown per user) is
  keyed per group_id — different groups never share cooldown/budget state.
  State persists to `usages/<group_id>.json` via JsonStore (atomic write +
  mtime tracking), hot-reloadable via mtime + `/reload_persona_config`.

  NOTE: poke (PokeService) deliberately stays shared across groups — QQ
  enforces poke cooldown per target user, not per group (2026-09-07 review).

Everything stateful lives in this object. The caller is responsible for
emitting a structured decision log (`decision_log_jsonl`). The `decide()`
return value is the log payload (caller appends; we just build it).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

from .json_store import JsonStore
from .style_profile import StyleProfile


# ---- result types ----

TRIGGER_AT = "at"
TRIGGER_RAG = "rag_hit"
TRIGGER_COLD = "cold_start"
TRIGGER_SILENT = "silent"

ACTION_REPLY = "reply"
ACTION_TOPIC = "topic"
ACTION_SILENT = "silent"


@dataclass
class Decision:
    action: str
    trigger: str
    reason: str
    score: float = 0.0
    hour: int = -1
    hourly_budget: float = 0.0
    hourly_used: float = 0.0
    silence_sec: float = 0.0
    cooldown_left_sec: float = 0.0
    extra: dict = field(default_factory=dict)

    def to_log(self, now_utc: float, sender_uin: str = "") -> dict:
        d = asdict(self)
        d["ts"] = datetime.utcfromtimestamp(now_utc).isoformat(timespec="seconds") + "Z"
        d["sender_uin"] = sender_uin
        return d


# ---- per-group usage state ----

@dataclass
class _GroupUsage:
    """Mutable usage state for one group, persisted to usages/<group_id>.json."""
    last_reply_ts: float = 0.0
    current_hour: int = -1
    hourly_used: float = 0.0
    last_at_reply_by_user: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "last_reply_ts": self.last_reply_ts,
            "current_hour": self.current_hour,
            "hourly_used": self.hourly_used,
            "last_at_reply_by_user": dict(self.last_at_reply_by_user),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "_GroupUsage":
        return cls(
            last_reply_ts=float(d.get("last_reply_ts", 0.0)),
            current_hour=int(d.get("current_hour", -1)),
            hourly_used=float(d.get("hourly_used", 0.0)),
            last_at_reply_by_user=dict(d.get("last_at_reply_by_user") or {}),
        )


# ---- manager ----


class InterjectionManager:
    def __init__(
        self,
        style: StyleProfile,
        *,
        data_dir: Optional[str] = None,
        active_interjection: int = 0,
        reply_on_at: int = 1,
        topic_bank_enabled: int = 0,
        rag_score_threshold: float = 0.55,
        cold_start_threshold_sec: float = 600.0,
        min_gap_sec: float = 25.0,
        at_cooldown_sec: float = 8.0,
        silence_cap_sec: float = 120.0,
        local_tz_offset_hours: int = 8,
    ) -> None:
        self._style = style
        self.active_interjection = int(active_interjection)
        self.reply_on_at = int(reply_on_at)
        self.topic_bank_enabled = int(topic_bank_enabled)
        self.rag_score_threshold = float(rag_score_threshold)
        self.cold_start_threshold_sec = float(cold_start_threshold_sec)
        self.min_gap_sec = float(min_gap_sec)
        self.at_cooldown_sec = float(at_cooldown_sec)
        self.silence_cap_sec = float(silence_cap_sec)
        self.tz_offset = int(local_tz_offset_hours)

        # RLock: decide()/register_reply() hold the lock while calling
        # _get_usage()/_roll_hour() which also acquire it (nested). A plain
        # Lock would deadlock; RLock is reentrant in the same thread.
        self._lock = threading.RLock()
        # per-group usage state: group_id -> _GroupUsage
        self._usage: dict[str, _GroupUsage] = {}
        # optional persistence (JsonStore, atomic + mtime tracked)
        self._store: Optional[JsonStore] = None
        if data_dir:
            self._store = JsonStore(data_dir)

    # ---- config hot-toggle ----
    def update_toggles(
        self,
        *,
        active_interjection: Optional[int] = None,
        reply_on_at: Optional[int] = None,
        topic_bank_enabled: Optional[int] = None,
    ) -> None:
        with self._lock:
            if active_interjection is not None:
                self.active_interjection = int(active_interjection)
            if reply_on_at is not None:
                self.reply_on_at = int(reply_on_at)
            if topic_bank_enabled is not None:
                self.topic_bank_enabled = int(topic_bank_enabled)

    # ---- per-group usage persistence ----

    def _usage_name(self, group_id: str) -> str:
        safe = str(group_id).replace("/", "_").replace("\\", "_")
        return f"usages/{safe}.json"

    def _load_usage(self, group_id: str) -> _GroupUsage:
        """Load persisted state for a group, or fresh if absent/corrupt."""
        g = str(group_id)
        if self._store is None:
            return _GroupUsage()
        data = self._store.load_json(self._usage_name(g))
        try:
            return _GroupUsage.from_dict(data) if data else _GroupUsage()
        except (ValueError, TypeError):
            return _GroupUsage()

    def _save_usage(self, group_id: str, usage: _GroupUsage) -> None:
        """Persist state atomically; never raises on disk hiccups."""
        if self._store is None:
            return
        try:
            # JsonStore.save_json auto-creates parent dirs (usages/).
            self._store.save_json(self._usage_name(group_id), usage.to_dict())
        except OSError:
            pass  # persistence must never crash the handler

    def _maybe_reload_usage(self, group_id: str, usage: _GroupUsage) -> _GroupUsage:
        """Hot-reload if the usage file changed on disk (mtime)."""
        if self._store is None:
            return usage
        try:
            new = self._store.reload_if_changed(self._usage_name(group_id))
        except OSError:
            return usage
        if new is None:
            return usage
        try:
            return _GroupUsage.from_dict(new) if new else usage
        except (ValueError, TypeError):
            return usage

    def _get_usage(self, group_id: str) -> _GroupUsage:
        """Return the live usage state for a group (thread-safe)."""
        g = str(group_id)
        with self._lock:
            usage = self._usage.get(g)
            if usage is None:
                usage = self._load_usage(g)
                self._usage[g] = usage
            else:
                usage = self._maybe_reload_usage(g, usage)
                self._usage[g] = usage
            return usage

    def drop_group(self, group_id: str) -> None:
        """Evict an inactive group's in-memory state (persisted copy stays)."""
        with self._lock:
            self._usage.pop(str(group_id), None)

    # ---- helpers ----
    def _local_hour(self, now_utc: float) -> int:
        return int(((now_utc / 3600.0) + self.tz_offset) % 24)

    def _roll_hour(self, usage: _GroupUsage, now_utc: float) -> int:
        h = self._local_hour(now_utc)
        if h != usage.current_hour:
            usage.current_hour = h
            usage.hourly_used = 0.0
        return h

    def _gap_left(self, usage: _GroupUsage, now_utc: float) -> float:
        return max(0.0, self.min_gap_sec - (now_utc - usage.last_reply_ts))

    def _at_gap_left(self, usage: _GroupUsage, user_uin: str, now_utc: float) -> float:
        last = usage.last_at_reply_by_user.get(user_uin, 0.0)
        return max(0.0, self.at_cooldown_sec - (now_utc - last))

    # ---- public ----
    def decide(
        self,
        *,
        group_id: str = "",
        now_utc: Optional[float] = None,
        is_at_me: bool = False,
        sender_uin: str = "",
        last_group_msg_ts: Optional[float] = None,
        top_rag_score: float = 0.0,
        emotion_multiplier: float = 1.0,
    ) -> Decision:
        """Make a single decision. Pure function over current state + inputs.

        Usage state is per-group: cooldown/budget never leak across groups.
        """
        now_utc = now_utc if now_utc is not None else time.time()
        g = str(group_id)
        with self._lock:
            usage = self._get_usage(g)
            hour = self._roll_hour(usage, now_utc)
            budget = self._style.hourly_budget(hour)
            silence = (now_utc - last_group_msg_ts) if last_group_msg_ts else 0.0

            # ---- 1. AT ----
            if is_at_me:
                if self.reply_on_at == 0:
                    return Decision(
                        action=ACTION_SILENT, trigger=TRIGGER_SILENT,
                        reason="reply_on_at=0 disables @-reply",
                        hour=hour, hourly_budget=budget, hourly_used=usage.hourly_used,
                        silence_sec=silence,
                    )
                cd = self._at_gap_left(usage, sender_uin, now_utc)
                if cd > 0:
                    return Decision(
                        action=ACTION_SILENT, trigger=TRIGGER_AT,
                        reason=f"at_cooldown active for {sender_uin}",
                        hour=hour, hourly_budget=budget, hourly_used=usage.hourly_used,
                        silence_sec=silence, cooldown_left_sec=round(cd, 2),
                    )
                # AT bypasses the active_interjection master gate and the
                # hourly budget cap (user-driven), but still records usage.
                return Decision(
                    action=ACTION_REPLY, trigger=TRIGGER_AT,
                    reason="user @ the bot",
                    hour=hour, hourly_budget=budget, hourly_used=usage.hourly_used,
                    silence_sec=silence,
                )

            # ---- master gate for active branches ----
            if self.active_interjection == 0:
                return Decision(
                    action=ACTION_SILENT, trigger=TRIGGER_SILENT,
                    reason="active_interjection=0",
                    hour=hour, hourly_budget=budget, hourly_used=usage.hourly_used,
                    silence_sec=silence,
                )

            # global min-gap
            gap = self._gap_left(usage, now_utc)
            if gap > 0:
                return Decision(
                    action=ACTION_SILENT, trigger=TRIGGER_SILENT,
                    reason="min_gap_sec not satisfied",
                    hour=hour, hourly_budget=budget, hourly_used=usage.hourly_used,
                    silence_sec=silence, cooldown_left_sec=round(gap, 2),
                )

            # hourly budget
            if usage.hourly_used >= budget:
                return Decision(
                    action=ACTION_SILENT, trigger=TRIGGER_SILENT,
                    reason=f"hourly budget exhausted ({usage.hourly_used:.2f}/{budget:.2f})",
                    hour=hour, hourly_budget=budget, hourly_used=usage.hourly_used,
                    silence_sec=silence,
                )

            # ---- 2. RAG ----
            effective_score = top_rag_score * emotion_multiplier
            if effective_score >= self.rag_score_threshold and silence <= self.silence_cap_sec:
                return Decision(
                    action=ACTION_REPLY, trigger=TRIGGER_RAG,
                    reason=f"top RAG score {top_rag_score:.2f}*{emotion_multiplier:.2f}={effective_score:.2f} >= {self.rag_score_threshold:.2f}",
                    score=round(effective_score, 4),
                    hour=hour, hourly_budget=budget, hourly_used=usage.hourly_used,
                    silence_sec=silence,
                )

            # ---- 3. COLD ----
            if self.topic_bank_enabled == 1 and silence >= self.cold_start_threshold_sec:
                return Decision(
                    action=ACTION_TOPIC, trigger=TRIGGER_COLD,
                    reason=f"silence {silence:.0f}s >= {self.cold_start_threshold_sec:.0f}s",
                    hour=hour, hourly_budget=budget, hourly_used=usage.hourly_used,
                    silence_sec=silence,
                )

            return Decision(
                action=ACTION_SILENT, trigger=TRIGGER_SILENT,
                reason="no trigger fired",
                score=round(top_rag_score, 4),
                hour=hour, hourly_budget=budget, hourly_used=usage.hourly_used,
                silence_sec=silence,
            )

    def register_reply(
        self,
        *,
        group_id: str = "",
        now_utc: Optional[float] = None,
        trigger: str = TRIGGER_RAG,
        sender_uin: str = "",
    ) -> None:
        """Caller MUST call this after the bot actually sent a message."""
        now_utc = now_utc if now_utc is not None else time.time()
        g = str(group_id)
        with self._lock:
            usage = self._get_usage(g)
            self._roll_hour(usage, now_utc)
            usage.last_reply_ts = now_utc
            usage.hourly_used += 1.0
            if trigger == TRIGGER_AT and sender_uin:
                usage.last_at_reply_by_user[sender_uin] = now_utc
            self._save_usage(g, usage)

    def snapshot(self, group_id: str = "") -> dict:
        with self._lock:
            if group_id:
                usage = self._get_usage(str(group_id))
                return {
                    "group_id": str(group_id),
                    "active_interjection": self.active_interjection,
                    "reply_on_at": self.reply_on_at,
                    "topic_bank_enabled": self.topic_bank_enabled,
                    "current_hour": usage.current_hour,
                    "hourly_used": usage.hourly_used,
                    "last_reply_ts": usage.last_reply_ts,
                    "at_cooldowns": dict(usage.last_at_reply_by_user),
                }
            return {
                "active_interjection": self.active_interjection,
                "reply_on_at": self.reply_on_at,
                "topic_bank_enabled": self.topic_bank_enabled,
                "groups": {g: u.to_dict() for g, u in self._usage.items()},
            }
