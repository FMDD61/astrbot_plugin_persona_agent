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

    def append(self, role: str, content: str, name: Optional[str] = None) -> None:
        msg: dict = {"role": role, "content": content}
        if name and role == "user":
            msg["name"] = name
        self.messages.append(msg)

    def get_messages(self) -> list[dict]:
        return list(self.messages)

    def recent(self, n: int = 20) -> list[dict]:
        items = list(self.messages)
        return items[-n:] if len(items) > n else items

    def size(self) -> int:
        return len(self.messages)

    def clear(self) -> None:
        self.messages.clear()


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

    def append(self, group_id: str, role: str, content: str, name: Optional[str] = None) -> None:
        with self._lock:
            sess = self._get_or_create(group_id)
        sess.append(role, content, name=name)
        self._maybe_save(group_id)

    def get_contexts(self, group_id: str) -> list[dict]:
        with self._lock:
            sess = self._get_or_create(group_id)
        return sess.get_messages()

    def recent(self, group_id: str, n: int = 20) -> list[dict]:
        with self._lock:
            sess = self._get_or_create(group_id)
        return sess.recent(n)

    def size(self, group_id: str) -> int:
        with self._lock:
            sess = self._get_or_create(group_id)
        return sess.size()

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
            cleaned = [
                m for m in msgs
                if isinstance(m, dict)
                and m.get("role") in ("user", "assistant")
                and isinstance(m.get("content"), str)
            ]
            with self._lock:
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
