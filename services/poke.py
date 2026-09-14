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
        proactive_hourly_cap: int = 5,
        local_tz_offset_hours: int = 8,
        serious_keywords: Optional[list[str]] = None,
    ) -> None:
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self.bot_qq = str(bot_qq or "")
        self.cooldown_sec = float(cooldown_sec)
        self.hourly_cap = int(hourly_cap)
        # S7 主动戳人（默认关；schema 默认 0）
        self.proactive_enabled = False
        self.proactive_hourly_cap = int(proactive_hourly_cap)
        self.tz_offset = int(local_tz_offset_hours)
        self._log_path = self._dir / "poke_log.jsonl"
        # runtime state
        self._last_poke_ts: dict[str, float] = {}
        self._hour_key: int = -1
        self._hour_count: int = 0
        # 主动配额与被动**分开计**（被动是社交回应，主动是自主行为，混在一起会互相挤占）
        self._proactive_hour_count: int = 0
        # serious-context keywords (hot-reload via mtime)
        self._kw_mtime: float = 0.0
        self._kw: list[str] = []
        if serious_keywords is not None:
            self._kw = [k.strip().lower() for k in serious_keywords if k.strip()]
        else:
            self._load_keywords()

    # ------------------------------------------------------------------ public

    def configure(
        self, *, cooldown_sec: Optional[float] = None, hourly_cap: Optional[int] = None,
        proactive_enabled: Optional[bool] = None,
        proactive_hourly_cap: Optional[int] = None,
    ) -> None:
        if cooldown_sec is not None:
            self.cooldown_sec = float(cooldown_sec)
        if hourly_cap is not None:
            self.hourly_cap = int(hourly_cap)
        if proactive_enabled is not None:
            self.proactive_enabled = bool(proactive_enabled)
        if proactive_hourly_cap is not None:
            self.proactive_hourly_cap = int(proactive_hourly_cap)
        # S7 主动戳人（默认关；schema 默认 0）
        self.proactive_enabled = False
        self.proactive_hourly_cap = int(proactive_hourly_cap)

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

    def decide_proactive(
        self,
        *,
        now_utc: Optional[float] = None,
        target_uin: str = "",
        closeness: str = "",
        allowed_closeness: tuple[str, ...] = ("close",),
        conflict: bool = False,
        recent_text: str = "",
    ) -> tuple[bool, str]:
        """主动戳人的硬闸（S7）。返回 ``(do_it, reason)``。

        **不信任模型**：模型只提供"想戳谁"，是否能戳由这里逐条判定。
        任一不过 → 调用方**静默跳过戳、正文照发**（绝不因戳失败而丢正文）。

        检查顺序（便宜且高确定性的在前）：
          1. target 为空 → no_target
          2. 亲疏不在白名单（默认仅 `close`）→ not_close
          3. **目标就是 bot 自己** → self_poke
          4. 冲突中 → conflict
          5. 严肃上下文（conflict_keywords 命中）→ serious_context
          6. **同人冷却：与被动回戳共用同一计时器**（用户 2026-09-14 指定
             "复用冷却计时"）→ 刚聊过/刚回戳过的人不会被追着戳
          7. 主动小时配额（与被动**分开计**）→ proactive_hourly_cap
        """
        now_utc = now_utc if now_utc is not None else time.time()
        if not self.proactive_enabled:
            return False, "proactive_disabled"
        tgt = str(target_uin or "")
        if not tgt:
            return False, "no_target"
        if self.bot_qq and tgt == self.bot_qq:
            return False, "self_poke"
        if str(closeness or "") not in tuple(allowed_closeness):
            return False, f"not_in_pool({closeness or 'unknown'})"
        if conflict:
            return False, "conflict"
        if self._serious(recent_text):
            return False, "serious_context"
        self._roll_hour(now_utc)
        last = self._last_poke_ts.get(tgt, 0.0)
        left = self.cooldown_sec - (now_utc - last)
        if left > 0:
            return False, f"cooldown:{left:.0f}s"
        if self._proactive_hour_count >= self.proactive_hourly_cap:
            return False, "proactive_hourly_cap"
        return True, "ok"

    def record_proactive(
        self, *, now_utc: Optional[float] = None, target_uin: str = "",
        group_id: str = "", done: bool, reason: str,
        raw_name: str = "", resolved_via: str = "",
    ) -> None:
        """记录一次主动戳尝试（成功才推进冷却与配额）。"""
        now_utc = now_utc if now_utc is not None else time.time()
        # 与 `record` 对称：记录时也要翻转小时，否则计数在"记录阶段"永不重置
        self._roll_hour(now_utc)
        if done:
            self._last_poke_ts[str(target_uin or "")] = now_utc
            self._proactive_hour_count += 1
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_utc)),
            "ts_epoch": round(now_utc, 3),
            "direction": "proactive",
            "group_id": str(group_id or ""),
            "poker": self.bot_qq,
            "target": str(target_uin or ""),
            "responded": bool(done),
            "reason": reason,
            # 模型写的名字与解析方式 —— 事后可核对"想戳谁 vs 实际戳了谁"
            "raw_name": str(raw_name or ""),
            "resolved_via": str(resolved_via or ""),
        }
        self._append(entry)

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
        self._append(entry)

    def _append(self, entry: dict) -> None:
        """原子追加一行；**日志失败绝不影响 handler**。"""
        try:
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def snapshot(self) -> dict:
        return {
            "cooldown_sec": self.cooldown_sec,
            "hourly_cap": self.hourly_cap,
            "hour_count": self._hour_count,
            "hour_key": self._hour_key,
            "last_pokes": dict(self._last_poke_ts),
            "keywords": list(self._kw),
            # S7
            "proactive_enabled": self.proactive_enabled,
            "proactive_hourly_cap": self.proactive_hourly_cap,
            "proactive_hour_count": self._proactive_hour_count,
        }

    # ------------------------------------------------------------------ private

    def _local_hour(self, now_utc: float) -> int:
        return int(((now_utc / 3600.0) + self.tz_offset) % 24)

    def _roll_hour(self, now_utc: float) -> None:
        """跨本地小时则重置**两个**计数。

        ⚠️ 加 S7 主动配额时这里漏改过：只重置 `_hour_count`，
        导致主动配额**一天只重置一次**（跨小时后仍卡在 cap 上，
        表现为"主动戳人只在当天最初几小时能用"）。测试抓到。
        """
        h = self._local_hour(now_utc)
        if h != self._hour_key:
            self._hour_key = h
            self._hour_count = 0
            self._proactive_hour_count = 0

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
