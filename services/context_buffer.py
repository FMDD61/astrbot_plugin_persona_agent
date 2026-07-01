"""ContextBuffer — bounded in-memory ring of recent group messages.

Plan §7 contract:
  - Keep the most recent N messages OR the last M seconds, whichever is
    smaller. New messages evict old ones.
  - Optionally mirror to recent_messages.jsonl (append-only, monotonic).
  - Provide format_recent(max_lines) -> str for prompt assembly.

Thread-safe. Add is O(1) amortised; format_recent is O(N) where N <=
max_messages (200 by default).
"""
from __future__ import annotations

import json
import os
import threading
from collections import deque
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


@dataclass
class BufferedMessage:
    ts: float
    group_id: str
    sender_id: str
    sender_name: str
    text: str
    message_id: str
    message_type: str  # "group" | "bot" | "system"


class ContextBuffer:
    def __init__(
        self,
        data_dir: str | os.PathLike,
        *,
        max_messages: int = 200,
        max_age_sec: int = 3600,
        persist_jsonl: bool = True,
        persist_name: str = "recent_messages.jsonl",
    ) -> None:
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max_n = int(max_messages)
        self._max_age = int(max_age_sec)
        self._persist = bool(persist_jsonl)
        self._persist_path = self._dir / persist_name
        self._buf: deque[BufferedMessage] = deque(maxlen=self._max_n)
        self._lock = threading.Lock()

    # ---- mutation ----
    def add(
        self,
        *,
        ts: float,
        group_id: str,
        sender_id: str,
        sender_name: str,
        text: str,
        message_id: str = "",
        message_type: str = "group",
    ) -> None:
        msg = BufferedMessage(
            ts=float(ts),
            group_id=str(group_id),
            sender_id=str(sender_id),
            sender_name=str(sender_name),
            text=str(text),
            message_id=str(message_id),
            message_type=str(message_type),
        )
        with self._lock:
            self._buf.append(msg)
            self._evict_old_locked()
        if self._persist:
            self._append_persist(msg)

    def _evict_old_locked(self) -> None:
        if not self._buf:
            return
        cutoff = self._buf[-1].ts - self._max_age
        while self._buf and self._buf[0].ts < cutoff:
            self._buf.popleft()

    def _append_persist(self, msg: BufferedMessage) -> None:
        try:
            with open(self._persist_path, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(asdict(msg), ensure_ascii=False) + "\n")
        except OSError:
            pass  # don't crash the bot on disk hiccups

    # ---- read ----
    def last_ts(self) -> float:
        with self._lock:
            return self._buf[-1].ts if self._buf else 0.0

    def size(self) -> int:
        with self._lock:
            return len(self._buf)

    def snapshot(self) -> list[BufferedMessage]:
        with self._lock:
            return list(self._buf)

    def format_recent(self, max_lines: int = 20, max_chars: int = 1200) -> str:
        """Render last N lines as 'sender_name: text', oldest first.

        Bot's own messages are tagged '<bot>: …'. Truncates long lines.
        Returns '' if the buffer is empty.
        """
        with self._lock:
            items = list(self._buf)[-max_lines:]
        if not items:
            return ""
        lines: list[str] = []
        total = 0
        for m in items:
            text = (m.text or "").replace("\n", " ").strip()
            if not text:
                continue
            if len(text) > 200:
                text = text[:200] + "…"
            line = f"{m.sender_name or m.sender_id}: {text}"
            total += len(line) + 1
            if total > max_chars:
                break
            lines.append(line)
        return "\n".join(lines)

    def find_since(self, since_ts: float) -> list[BufferedMessage]:
        with self._lock:
            return [m for m in self._buf if m.ts >= since_ts]
