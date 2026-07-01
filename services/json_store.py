"""JsonStore — atomic, UTF-8, mtime-aware JSON/JSONL persistence.

Plan §3 contract:
  - load_json(name, default) -> dict
  - save_json(name, data)
  - append_jsonl(name, record)
  - reload_if_changed(name) -> dict | None  (None == unchanged)

Write protocol (every save_json):
  1. Read current bytes (if exists) and copy to <name>.bak.
  2. Write to <name>.tmp with utf-8 + ensure_ascii=False + indent=2.
  3. fsync, then os.replace(.tmp, name) -> atomic on POSIX & NTFS.

Never overwrites a file the user has touched between our writes — the
plugin's drift task is supposed to emit *new* suggestion files, never
clobber editable ones (acceptance checklist #4). This class provides the
mechanism; the policy lives in callers.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional


class JsonStore:
    def __init__(self, data_dir: str | os.PathLike) -> None:
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._mtime: dict[str, float] = {}

    # ---- paths ----
    def _path(self, name: str) -> Path:
        return self._dir / name

    # ---- read ----
    def load_json(self, name: str, default: Optional[dict] = None) -> dict:
        path = self._path(name)
        if not path.exists():
            return {} if default is None else dict(default)
        try:
            data = json.loads(path.read_text("utf-8"))
            with self._lock:
                self._mtime[name] = path.stat().st_mtime
            return data if isinstance(data, dict) else {"_root": data}
        except (json.JSONDecodeError, OSError):
            return {} if default is None else dict(default)

    def reload_if_changed(self, name: str) -> Optional[dict]:
        """Return the new dict if mtime changed since the last load/save,
        else None."""
        path = self._path(name)
        if not path.exists():
            return None
        mt = path.stat().st_mtime
        with self._lock:
            prev = self._mtime.get(name)
        if prev is not None and mt == prev:
            return None
        return self.load_json(name)

    # ---- write ----
    def save_json(self, name: str, data: Any) -> None:
        path = self._path(name)
        tmp = path.with_suffix(path.suffix + ".tmp")
        bak = path.with_suffix(path.suffix + ".bak")

        payload = json.dumps(data, ensure_ascii=False, indent=2)

        with self._lock:
            # backup current
            if path.exists():
                try:
                    bak.write_bytes(path.read_bytes())
                except OSError:
                    pass  # best-effort

            # atomic write
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(payload)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, path)
            self._mtime[name] = path.stat().st_mtime

    def append_jsonl(self, name: str, record: Any) -> None:
        """Append-only line. No backup (jsonl is monotonic). Crash-safe
        within a single record because we open() with `a` mode."""
        path = self._path(name)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(line)

    # ---- introspection ----
    def exists(self, name: str) -> bool:
        return self._path(name).exists()

    def mtime(self, name: str) -> float:
        p = self._path(name)
        return p.stat().st_mtime if p.exists() else 0.0
