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


def find_jsonl_record(path: Path, match: dict, *, tail_lines: int = 500) -> Optional[dict]:
    """在 JSONL 文件里找 ``match`` 全部键值相等的记录（幂等去重，跨进程有效）。

    ## 为什么必须有（2026-09-14 实测事故）

    用户在 09-13 夜间反复重启 AstrBot（02:01–02:17 注册了 6 次），**新旧实例交叠**
    期间同一个 02:05 日旋转 cron 被两个进程各执行一次 →
    - `weekly_summary.jsonl` 出现 **2 条 2026-W37** → 用户私聊收到**两份周报**
    - `logs/<gid>/daily_diary.jsonl` 出现 2–3 条同一天日记

    教训：**内存去重（`self._xxx_done` 标记）跨不了进程** —— 多实例场景下两个
    进程各自"第一次"执行。幂等判据必须落在**文件**上，读回已有记录做比对。

    `tail_lines` 限制读取量：日志文件会长到几千行，但重复记录必然出现在尾部
    （同一天/同一周期的记录是刚写的）。
    """
    try:
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in reversed(lines[-tail_lines:]):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and all(rec.get(k) == v for k, v in match.items()):
            return rec
    return None


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
    def path(self, name: str) -> Path:
        """公开的路径解析（`_path` 的对外形式）。

        存在的理由：调用方需要把路径交给 `find_jsonl_record()` 做**文件级幂等**
        检查。没有它就只能用私有 `_path`（跨模块用私有名是坏味道，且改名即炸）。
        """
        return self._path(name)

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
            # auto-create parent dirs (names may include subdirs like usages/)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
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
            # auto-create parent dirs (names may include subdirs like logs/<g>/)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(line)

    # ---- introspection ----
    def exists(self, name: str) -> bool:
        return self._path(name).exists()

    def mtime(self, name: str) -> float:
        p = self._path(name)
        return p.stat().st_mtime if p.exists() else 0.0
