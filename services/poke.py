"""PokeService — G11 戳一戳响应控制（纯 stdlib，可离线单测）。

决策面（main.py on_other 只做 OneBot 解码与本服务接线）：
  1. enabled=0 / 非目标群 / target != bot / 无 poker —— main 直接 return（零副作用）。
  2. 未知关系成员（member_relations 查不到）默认不回戳（acceptance §12）。
  3. 同一人冷却（poke.cooldown_sec，默认 300s）。
  4. 全局小时配额（_HOURLY_CAP，防连戳刷屏）。
  5. 严肃上下文抑制：最近群消息命中 conflict_keywords.json 任一关键词时不回戳
     （mtime 热重载；文件缺失/损坏视为无关键词）。

记录：poke_log.jsonl（原子追加，JSONL：ts/group_id/poker/target/responded/reason）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

_HOURLY_CAP = 4
_KW_FILE = "conflict_keywords.json"


class PokeService:
    def __init__(
        self,
        data_dir: str,
        *,
        bot_qq: str = "",
        cooldown_sec: float = 300.0,
        hourly_cap: int = _HOURLY_CAP,
        local_tz_offset_hours: int = 8,
        serious_keywords: Optional[list[str]] = None,
    ) -> None:
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self.bot_qq = str(bot_qq or "")
        self.cooldown_sec = float(cooldown_sec)
        self.hourly_cap = int(hourly_cap)
        self.tz_offset = int(local_tz_offset_hours)
        self._log_path = self._dir / "poke_log.jsonl"
        # runtime state
        self._last_poke_ts: dict[str, float] = {}
        self._hour_key: int = -1
        self._hour_count: int = 0
        # serious-context keywords (hot-reload via mtime)
        self._kw_mtime: float = 0.0
        self._kw: list[str] = []
        if serious_keywords is not None:
            self._kw = [k.strip().lower() for k in serious_keywords if k.strip()]
        else:
            self._load_keywords()

    # ------------------------------------------------------------------ public

    def configure(self, *, cooldown_sec: Optional[float] = None, hourly_cap: Optional[int] = None) -> None:
        if cooldown_sec is not None:
            self.cooldown_sec = float(cooldown_sec)
        if hourly_cap is not None:
            self.hourly_cap = int(hourly_cap)

    def decide(
        self,
        *,
        now_utc: Optional[float] = None,
        poker: str = "",
        group_id: str = "",
        known_member: bool = False,
        recent_text: str = "",
    ) -> tuple[bool, str]:
        """Return (respond, reason). Pure over current state."""
        now_utc = now_utc if now_utc is not None else time.time()
        if not poker:
            return False, "no_poker"
        self._roll_hour(now_utc)
        if not known_member:
            return False, "unknown_member"
        last = self._last_poke_ts.get(poker, 0.0)
        left = self.cooldown_sec - (now_utc - last)
        if left > 0:
            return False, f"cooldown:{left:.0f}s"
        if self._hour_count >= self.hourly_cap:
            return False, "hourly_cap"
        if self._serious(recent_text):
            return False, "serious_context"
        return True, "ok"

    def record(
        self,
        *,
        now_utc: Optional[float] = None,
        poker: str = "",
        group_id: str = "",
        responded: bool,
        reason: str,
    ) -> None:
        now_utc = now_utc if now_utc is not None else time.time()
        if responded:
            self._last_poke_ts[poker] = now_utc
            self._hour_count += 1
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_utc)),
            "ts_epoch": round(now_utc, 3),
            "group_id": str(group_id or ""),
            "poker": str(poker or ""),
            "target": self.bot_qq,
            "responded": bool(responded),
            "reason": reason,
        }
        try:
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass  # logging must never crash the handler

    def snapshot(self) -> dict:
        return {
            "cooldown_sec": self.cooldown_sec,
            "hourly_cap": self.hourly_cap,
            "hour_count": self._hour_count,
            "hour_key": self._hour_key,
            "last_pokes": dict(self._last_poke_ts),
            "keywords": list(self._kw),
        }

    # ------------------------------------------------------------------ private

    def _local_hour(self, now_utc: float) -> int:
        return int(((now_utc / 3600.0) + self.tz_offset) % 24)

    def _roll_hour(self, now_utc: float) -> None:
        h = self._local_hour(now_utc)
        if h != self._hour_key:
            self._hour_key = h
            self._hour_count = 0

    def _load_keywords(self) -> None:
        path = self._dir / _KW_FILE
        try:
            self._kw_mtime = path.stat().st_mtime
            data = json.loads(path.read_text("utf-8"))
            self._kw = [k.strip().lower() for k in data.get("keywords", []) if k.strip()]
        except Exception:
            self._kw = []

    def _maybe_reload_keywords(self) -> None:
        path = self._dir / _KW_FILE
        try:
            mt = path.stat().st_mtime
        except OSError:
            return
        if mt > self._kw_mtime:
            self._load_keywords()

    def _serious(self, recent_text: str) -> bool:
        if not recent_text:
            return False
        self._maybe_reload_keywords()
        if not self._kw:
            return False
        lower = recent_text.lower()
        return any(kw in lower for kw in self._kw)
