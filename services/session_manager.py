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

import threading
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
        self._sessions: dict[str, Session] = {}
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

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {gid: s.size() for gid, s in self._sessions.items()}
