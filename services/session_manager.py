"""SessionManager — per-group conversation session with multi-user name support.

Each group gets one session that accumulates messages across calls.
Messages use the OpenAI Chat API format with optional ``name`` field to
differentiate participants under the same ``user`` role.

Session lifecycle:
  1. On startup: empty. Builds naturally from incoming messages.
  2. Each inbound message: ``append("user", text, name=alias)``.
  3. Each bot reply:     ``append("assistant", reply_text)``.
  4. KG injection is appended separately by the caller before the LLM call.

Trim policy: keep system message at position 0 + last N messages.
Thread-safe.
"""
from __future__ import annotations

import datetime
import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Session:
    group_id: str
    day: str = ""
    """Session day key (rotation window), empty until first use."""
    messages: deque[dict] = field(default_factory=lambda: deque(maxlen=600))

    def append(
        self,
        role: str,
        content: str,
        name: Optional[str] = None,
        *,
        message_id: str = "",
        sender_uin: str = "",
    ) -> None:
        msg: dict = {"role": role, "content": content}
        if name and role == "user":
            msg["name"] = name
        # B-001: 内部元数据。仅用于 ``[r:-N]`` 引用目标解析（编号基与 LLM 所见
        # 一致），**绝不进 LLM 请求** —— 所有对外出口都经 ``_public`` 剥离。
        if message_id:
            msg["_mid"] = str(message_id)
        if sender_uin and role == "user":
            msg["_uin"] = str(sender_uin)
        self.messages.append(msg)

    def get_messages(self) -> list[dict]:
        """公开条目（剥离内部键 + 丢弃空 content）。

        B-002: 历史脏数据里出现过 ``content=""`` 的条目，原样送进 LLM 会让
        网关返回 HTTP 400 ``user message must have content``，插件的 try/except
        把异常吞掉 → 表现为「收到消息但永远不回复」的永久静默。
        这里在唯一收口点过滤：正常会话无空条目 → **零行为变化**。
        """
        return [_public(m) for m in self.messages if _has_content(m)]

    def quote_entries(self) -> list[tuple[str, str, str]]:
        """引用编号基的原始三元组（与 ``get_messages()`` 逐条对齐）。

        ``get_messages()`` 会丢弃空 content 条目，这里必须用**同一过滤**，
        否则编号基又会与 LLM 所见错位 —— 那正是 B-001 的次要成因。
        """
        return [
            (str(m.get("_mid") or ""), str(m.get("_uin") or ""), str(m.get("name") or ""))
            for m in self.messages
            if _has_content(m)
        ]

    def recent(self, n: int = 20) -> list[dict]:
        # 与 get_messages 同过滤，保证调用方看到的内容一致（B-002）
        items = [_public(m) for m in self.messages if _has_content(m)]
        return items[-n:] if len(items) > n else items

    def size(self) -> int:
        return len(self.messages)

    def clear(self) -> None:
        self.messages.clear()


# 内部元数据键前缀：绝不进 LLM 请求/对外 API
_INTERNAL_PREFIX = "_"


def _public(msg: dict) -> dict:
    """抹掉内部元数据键（``_mid``/``_uin``）。无元数据时原样返回。"""
    if not any(k.startswith(_INTERNAL_PREFIX) for k in msg):
        return msg
    return {k: v for k, v in msg.items() if not k.startswith(_INTERNAL_PREFIX)}


def _has_content(msg) -> bool:
    """B-002 判据：content 非空白。非 dict / 非 str 一律视为无内容。"""
    if not isinstance(msg, dict):
        return False
    c = msg.get("content")
    return isinstance(c, str) and bool(c.strip())



class SessionManager:
    def __init__(
        self,
        data_dir: Optional[str] = None,
        max_messages: Optional[int] = 300,
        rotation_hour: int = 2,
        tz_offset_hours: int = 8,
    ) -> None:
        self._dir = Path(data_dir) if data_dir else None
        # None => unbounded (daily rotation bounds the session anyway)
        self._max_messages = max_messages
        self._rotation_hour = int(rotation_hour)
        self._tz_offset_hours = int(tz_offset_hours)
        self._persist_step = 50
        self._persist_interval = 300.0
        self._sessions: dict[str, Session] = {}
        self._last_save: dict[str, float] = {}
        self._saved_at_count: dict[str, int] = {}
        # B-002 观测：恢复期丢弃的空 content 条目（>0 说明数据曾损坏）
        self._empty_dropped: dict[str, int] = {}
        self._lock = threading.Lock()

    def _get_or_create(self, group_id: str) -> Session:
        if group_id not in self._sessions:
            sess = Session(group_id=group_id)
            sess.messages = deque(maxlen=self._max_messages)
            sess.day = self.day_key()
            self._sessions[group_id] = sess
        return self._sessions[group_id]

    def day_key(self, ts: float = 0.0) -> str:
        """Day-boundary key: dates roll over at `rotation_hour` local time."""
        dt = datetime.datetime.fromtimestamp(ts or time.time(), datetime.timezone.utc)
        dt = dt + datetime.timedelta(hours=self._tz_offset_hours)
        if dt.hour < self._rotation_hour:
            dt = dt - datetime.timedelta(days=1)
        return dt.strftime("%Y-%m-%d")

    def append(
        self,
        group_id: str,
        role: str,
        content: str,
        name: Optional[str] = None,
        *,
        message_id: str = "",
        sender_uin: str = "",
    ) -> None:
        with self._lock:
            sess = self._get_or_create(group_id)
        sess.append(
            role, content, name=name, message_id=message_id, sender_uin=sender_uin
        )
        self._maybe_save(group_id)

    def get_contexts(self, group_id: str) -> list[dict]:
        with self._lock:
            sess = self._get_or_create(group_id)
        return sess.get_messages()

    def quote_snapshot(self, group_id: str):
        """B-001: 冻结当前编号基，供 ``[r:-N]`` 在生成结束后求值。

        必须在**构建上下文之后、发起生成之前**调用。返回 ``QuoteIndex``
        （不可变），生成窗口内新到的消息不会影响它。
        """
        from .context_buffer import QuoteIndex

        with self._lock:
            sess = self._get_or_create(group_id)
        return QuoteIndex.from_entries(sess.quote_entries())

    def recent(self, group_id: str, n: int = 20) -> list[dict]:
        with self._lock:
            sess = self._get_or_create(group_id)
        return sess.recent(n)

    def size(self, group_id: str) -> int:
        with self._lock:
            sess = self._get_or_create(group_id)
        return sess.size()

    def dropped_empty(self, group_id: str) -> int:
        """B-002 观测：本会话被丢弃的空 content 条目数（自愈计数）。"""
        with self._lock:
            sess = self._sessions.get(group_id)
            if sess is None:
                return 0
            return sum(1 for m in sess.messages if not _has_content(m))

    def empty_dropped_on_load(self) -> dict[str, int]:
        """B-002 观测：启动恢复期丢弃的空 content 条目数（按群）。"""
        with self._lock:
            return dict(self._empty_dropped)

    def clear(self, group_id: str) -> None:
        with self._lock:
            if group_id in self._sessions:
                self._sessions[group_id].clear()
            self._saved_at_count[group_id] = 0
        if self._dir is not None:
            try:
                for f in self._dir.glob(f"session_{self._safe_name(group_id)}_*.json"):
                    f.unlink(missing_ok=True)
                self._session_path(group_id).unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _safe_name(group_id: str) -> str:
        return group_id.replace("/", "_").replace("\\", "_")

    # ---- daily rotation (v3: rotate at rotation_hour local, keep unlimited) ----

    def rotate_if_day_changed(self, group_id: str) -> Optional[list[dict]]:
        """If the session belongs to a previous day window, archive and clear it.

        Returns the archived messages (for diary generation) or None when no
        rotation happened. Persists the old day to session_<group>_<day>.json.
        """
        key = self.day_key()
        with self._lock:
            sess = self._sessions.get(group_id)
            if sess is None:
                sess = Session(group_id=group_id)
                sess.messages = deque(maxlen=self._max_messages)
                sess.day = key
                self._sessions[group_id] = sess
                return None
            if sess.day == key:
                return None
            old_msgs = list(sess.messages)
            if not old_msgs:
                sess.day = key
                return None
            old_day = sess.day
            sess.messages.clear()
            sess.day = key
            self._saved_at_count[group_id] = 0
            self._last_save[group_id] = 0.0
        if self._dir is not None and old_day:
            try:
                payload = {
                    "version": 2,
                    "group_id": group_id,
                    "day": old_day,
                    "saved_at": time.time(),
                    "messages": old_msgs,
                }
                path = self._session_path(group_id, old_day)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, path)
            except OSError:
                pass
        return old_msgs

    def save_all(self) -> None:
        """Best-effort flush of all live sessions (terminate / pre-rotation)."""
        with self._lock:
            gids = list(self._sessions.keys())
        for gid in gids:
            self._save(gid)

    # ---- persistence (v0.2 4.2 lightweight: every ~50 msgs or ~5 min) ----

    def _session_path(self, group_id: str, day: Optional[str] = None) -> Path:
        safe = group_id.replace("/", "_").replace("\\", "_")
        if day:
            return self._dir / f"session_{safe}_{day}.json" if self._dir else Path(f"session_{safe}_{day}.json")
        return self._dir / f"session_{safe}.json" if self._dir else Path(f"session_{safe}.json")

    def _save(self, group_id: str) -> None:
        if self._dir is None:
            return
        with self._lock:
            sess = self._sessions.get(group_id)
            if sess is None or not sess.messages:
                return
            payload = {
                "version": 2,
                "group_id": group_id,
                "day": sess.day or "",
                "saved_at": time.time(),
                "messages": list(sess.messages),
            }
            path = self._session_path(group_id, sess.day or None)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    def _maybe_save(self, group_id: str) -> None:
        now = time.time()
        with self._lock:
            sess = self._sessions.get(group_id)
            if sess is None or len(sess.messages) == 0:
                return
            size = len(sess.messages)
            prev_count = self._saved_at_count.get(group_id, 0)
            prev_ts = self._last_save.get(group_id, 0.0)
            if size == prev_count:
                if (now - prev_ts) < self._persist_interval:
                    return
            else:
                if (size - prev_count) < self._persist_step and (now - prev_ts) < self._persist_interval:
                    return
            self._saved_at_count[group_id] = size
            self._last_save[group_id] = now
        self._save(group_id)

    def load_all(self) -> dict[str, int]:
        """Restore the newest per-group day session at startup.

        Picks, per group, the day file with the largest day key (the current
        session window); legacy v1 files without a day field are treated as
        current-day. Corrupt files are skipped; never raises.
        Returns {group_id: restored_message_count}.
        """
        restored: dict[str, int] = {}
        if self._dir is None:
            return restored
        skipped = 0
        best: dict[str, tuple[str, dict]] = {}
        today = self.day_key()
        for path in sorted(self._dir.glob("session_*.json")):
            if path.name.endswith(".tmp"):
                continue
            try:
                payload = json.loads(path.read_text("utf-8"))
                gid = str(payload.get("group_id", ""))
                msgs = payload.get("messages") or []
                if not gid or not isinstance(msgs, list):
                    skipped += 1
                    continue
                day = str(payload.get("day") or today)
                prev = best.get(gid)
                if prev is None or day > prev[0]:
                    best[gid] = (day, payload)
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                skipped += 1
        for gid, (day, payload) in best.items():
            msgs = payload.get("messages") or []
            # B-002: 恢复时同样过滤空 content（脏数据可能来自旧版本插件）。
            # B-001: 保留 _mid/_uin 元数据（deque 整体迁移，不重建条目）。
            cleaned = [
                _public(m) | {
                    k: m[k] for k in ("_mid", "_uin") if m.get(k)
                }
                for m in msgs
                if _has_content(m) and m.get("role") in ("user", "assistant")
            ]
            dropped = sum(
                1 for m in msgs
                if isinstance(m, dict)
                and m.get("role") in ("user", "assistant")
                and not _has_content(m)
            )
            with self._lock:
                if dropped:
                    self._empty_dropped[gid] = self._empty_dropped.get(gid, 0) + dropped
                if gid not in self._sessions:
                    sess = Session(group_id=gid)
                    sess.messages = deque(maxlen=self._max_messages)
                    self._sessions[gid] = sess
                sess = self._sessions[gid]
                if self._max_messages is not None:
                    cleaned = cleaned[-self._max_messages:]
                sess.messages = deque(cleaned, maxlen=self._max_messages)
                sess.day = day
                self._last_save[gid] = time.time()
                self._saved_at_count[gid] = len(sess.messages)
            restored[gid] = len(sess.messages)
        if skipped:
            import logging
            logging.getLogger("persona_session").warning(
                f"[persona_agent] session restore skipped {skipped} file(s)"
            )
        return restored

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {gid: s.size() for gid, s in self._sessions.items()}
