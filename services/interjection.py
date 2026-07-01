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

Everything stateful lives in this object; no disk writes. The caller is
responsible for emitting a structured decision log (`decision_log_jsonl`).
The `decide()` return value is the log payload (caller appends; we just
build it).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

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


# ---- manager ----


class InterjectionManager:
    def __init__(
        self,
        style: StyleProfile,
        *,
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

        self._lock = threading.Lock()
        self._last_reply_ts: float = 0.0
        self._last_at_reply_ts_by_user: dict[str, float] = {}
        self._current_hour: int = -1
        self._hourly_used: float = 0.0

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

    # ---- helpers ----
    def _local_hour(self, now_utc: float) -> int:
        return int(((now_utc / 3600.0) + self.tz_offset) % 24)

    def _roll_hour(self, now_utc: float) -> int:
        h = self._local_hour(now_utc)
        if h != self._current_hour:
            self._current_hour = h
            self._hourly_used = 0.0
        return h

    def _gap_left(self, now_utc: float) -> float:
        return max(0.0, self.min_gap_sec - (now_utc - self._last_reply_ts))

    def _at_gap_left(self, user_uin: str, now_utc: float) -> float:
        last = self._last_at_reply_ts_by_user.get(user_uin, 0.0)
        return max(0.0, self.at_cooldown_sec - (now_utc - last))

    # ---- public ----
    def decide(
        self,
        *,
        now_utc: Optional[float] = None,
        is_at_me: bool = False,
        sender_uin: str = "",
        last_group_msg_ts: Optional[float] = None,
        top_rag_score: float = 0.0,
    ) -> Decision:
        """Make a single decision. Pure function over current state + inputs."""
        now_utc = now_utc if now_utc is not None else time.time()
        with self._lock:
            hour = self._roll_hour(now_utc)
            budget = self._style.hourly_budget(hour)
            silence = (now_utc - last_group_msg_ts) if last_group_msg_ts else 0.0

            # ---- 1. AT ----
            if is_at_me:
                if self.reply_on_at == 0:
                    return Decision(
                        action=ACTION_SILENT, trigger=TRIGGER_SILENT,
                        reason="reply_on_at=0 disables @-reply",
                        hour=hour, hourly_budget=budget, hourly_used=self._hourly_used,
                        silence_sec=silence,
                    )
                cd = self._at_gap_left(sender_uin, now_utc)
                if cd > 0:
                    return Decision(
                        action=ACTION_SILENT, trigger=TRIGGER_AT,
                        reason=f"at_cooldown active for {sender_uin}",
                        hour=hour, hourly_budget=budget, hourly_used=self._hourly_used,
                        silence_sec=silence, cooldown_left_sec=round(cd, 2),
                    )
                # AT bypasses the active_interjection master gate and the
                # hourly budget cap (user-driven), but still records usage.
                return Decision(
                    action=ACTION_REPLY, trigger=TRIGGER_AT,
                    reason="user @ the bot",
                    hour=hour, hourly_budget=budget, hourly_used=self._hourly_used,
                    silence_sec=silence,
                )

            # ---- master gate for active branches ----
            if self.active_interjection == 0:
                return Decision(
                    action=ACTION_SILENT, trigger=TRIGGER_SILENT,
                    reason="active_interjection=0",
                    hour=hour, hourly_budget=budget, hourly_used=self._hourly_used,
                    silence_sec=silence,
                )

            # global min-gap
            gap = self._gap_left(now_utc)
            if gap > 0:
                return Decision(
                    action=ACTION_SILENT, trigger=TRIGGER_SILENT,
                    reason="min_gap_sec not satisfied",
                    hour=hour, hourly_budget=budget, hourly_used=self._hourly_used,
                    silence_sec=silence, cooldown_left_sec=round(gap, 2),
                )

            # hourly budget
            if self._hourly_used >= budget:
                return Decision(
                    action=ACTION_SILENT, trigger=TRIGGER_SILENT,
                    reason=f"hourly budget exhausted ({self._hourly_used:.2f}/{budget:.2f})",
                    hour=hour, hourly_budget=budget, hourly_used=self._hourly_used,
                    silence_sec=silence,
                )

            # ---- 2. RAG ----
            if top_rag_score >= self.rag_score_threshold and silence <= self.silence_cap_sec:
                return Decision(
                    action=ACTION_REPLY, trigger=TRIGGER_RAG,
                    reason=f"top RAG score {top_rag_score:.2f} >= {self.rag_score_threshold:.2f}",
                    score=round(top_rag_score, 4),
                    hour=hour, hourly_budget=budget, hourly_used=self._hourly_used,
                    silence_sec=silence,
                )

            # ---- 3. COLD ----
            if self.topic_bank_enabled == 1 and silence >= self.cold_start_threshold_sec:
                return Decision(
                    action=ACTION_TOPIC, trigger=TRIGGER_COLD,
                    reason=f"silence {silence:.0f}s >= {self.cold_start_threshold_sec:.0f}s",
                    hour=hour, hourly_budget=budget, hourly_used=self._hourly_used,
                    silence_sec=silence,
                )

            return Decision(
                action=ACTION_SILENT, trigger=TRIGGER_SILENT,
                reason="no trigger fired",
                score=round(top_rag_score, 4),
                hour=hour, hourly_budget=budget, hourly_used=self._hourly_used,
                silence_sec=silence,
            )

    def register_reply(
        self,
        *,
        now_utc: Optional[float] = None,
        trigger: str = TRIGGER_RAG,
        sender_uin: str = "",
    ) -> None:
        """Caller MUST call this after the bot actually sent a message."""
        now_utc = now_utc if now_utc is not None else time.time()
        with self._lock:
            self._roll_hour(now_utc)
            self._last_reply_ts = now_utc
            self._hourly_used += 1.0
            if trigger == TRIGGER_AT and sender_uin:
                self._last_at_reply_ts_by_user[sender_uin] = now_utc

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "active_interjection": self.active_interjection,
                "reply_on_at": self.reply_on_at,
                "topic_bank_enabled": self.topic_bank_enabled,
                "current_hour": self._current_hour,
                "hourly_used": self._hourly_used,
                "last_reply_ts": self._last_reply_ts,
                "at_cooldowns": dict(self._last_at_reply_ts_by_user),
            }
