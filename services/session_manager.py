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
        max_messages: int = 300,
    ) -> None:
        self._dir = Path(data_dir) if data_dir else None
        self._max_messages = max_messages
        self._persist_step = 50
        self._persist_interval = 300.0
        self._sessions: dict[str, Session] = {}
        self._last_save: dict[str, float] = {}
        self._saved_at_count: dict[str, int] = {}
        self._lock = threading.Lock()

    def _get_or_create(self, group_id: str) -> Session:
        if group_id not in self._sessions:
            self._sessions[group_id] = Session(group_id=group_id)
            self._sessions[group_id].messages = deque(maxlen=self._max_messages)
        return self._sessions[group_id]

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
                self._session_path(group_id).unlink(missing_ok=True)
            except OSError:
                pass

    # ---- persistence (v0.2 4.2 lightweight: every ~50 msgs or ~5 min) ----

    def _session_path(self, group_id: str) -> Path:
        safe = group_id.replace("/", "_").replace("\\", "_")
        return self._dir / f"session_{safe}.json" if self._dir else Path(f"session_{safe}.json")

    def _save(self, group_id: str) -> None:
        if self._dir is None:
            return
        with self._lock:
            sess = self._sessions.get(group_id)
            if sess is None or not sess.messages:
                return
            payload = {
                "version": 1,
                "group_id": group_id,
                "saved_at": time.time(),
                "messages": list(sess.messages),
            }
            path = self._session_path(group_id)
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
        """Restore per-group sessions from session_<group>.json at startup.

        Corrupt/unknown files are skipped with a warning count; never raises.
        Returns {group_id: restored_message_count}.
        """
        restored: dict[str, int] = {}
        if self._dir is None:
            return restored
        skipped = 0
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
                cleaned = [
                    m for m in msgs
                    if isinstance(m, dict)
                    and m.get("role") in ("user", "assistant")
                    and isinstance(m.get("content"), str)
                ]
                with self._lock:
                    sess = self._get_or_create(gid)
                    sess.messages = deque(cleaned[-self._max_messages:], maxlen=self._max_messages)
                    self._last_save[gid] = time.time()
                    self._saved_at_count[gid] = len(sess.messages)
                restored[gid] = len(sess.messages)
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                skipped += 1
        if skipped:
            import logging
            logging.getLogger("persona_session").warning(
                f"[persona_agent] session restore skipped {skipped} file(s)"
            )
        return restored

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {gid: s.size() for gid, s in self._sessions.items()}
