"""StyleProfile — load and hot-reload the editable style files from §5.

Files watched (under <data_dir>):
  my_style_profile.json
  my_lexicon.json
  my_emoticons.json
  my_message_stats.json
  my_hourly_distribution.json
  member_relations.json
  system_prompt_fragments.json

Each load tracks mtime. On every accessor call we check mtime and reload if
changed. Weekly drift tasks are responsible for proposing *new* values; this
service never overwrites a file the user has touched (read-only side).

Interface contract for sub-agent C:
  sp = StyleProfile(data_dir)
  sp.system_prompt(local_hour=None) -> str
      Builds the **fixed** system prompt from system_prompt_fragments.json
      (identity/rules/alias block). Always returns a non-empty string.
      Hot-reloads. local_hour is accepted for backward compatibility but no
      longer participates (see volatile_line) — the prompt must stay
      byte-identical across turns so the gateway prefix cache survives.

  sp.volatile_line(local_hour=None, mood="") -> str
      Per-turn varying bits (local time sentence + current mood). Callers
      append this as a system message at the END of the context, never into
      the system prompt.

  sp.hourly_budget(hour: int) -> float
      Per-hour interjection budget from my_hourly_distribution.json.

  sp.peak_hours() -> set[int]
      Hours whose share is >= mean(share). Used by interjection cooldown.

  sp.preferred_alias(uin: str) -> str
      Returns the alias for a member, or empty string if unknown.

  sp.resolve_uin_from_name(name: str) -> Optional[str]
      Looks up a uin by alias or other_names. Returns None if not found.

  sp.snapshot() -> dict
      Returns the last-loaded raw dict of all 7 files (for debugging / smoke test).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional

_FILES = (
    "my_style_profile.json",
    "my_lexicon.json",
    "my_emoticons.json",
    "my_message_stats.json",
    "my_hourly_distribution.json",
    "member_relations.json",
    "system_prompt_fragments.json",
)

CLOSENESS_LABEL = {"close": "熟人", "known": "认识", "new": "新人"}


class StyleProfile:
    def __init__(self, data_dir: str | os.PathLike) -> None:
        self._dir = Path(data_dir)
        self._lock = threading.Lock()
        self._mtime: dict[str, float] = {}
        self._cache: dict[str, dict] = {}
        for name in _FILES:
            self._maybe_reload(name)

    def _path(self, name: str) -> Path:
        return self._dir / name

    def _maybe_reload(self, name: str) -> dict:
        path = self._path(name)
        with self._lock:
            if not path.exists():
                self._cache[name] = {}
                self._mtime[name] = 0.0
                return self._cache[name]
            mt = path.stat().st_mtime
            if mt != self._mtime.get(name):
                try:
                    self._cache[name] = json.loads(path.read_text("utf-8"))
                except (json.JSONDecodeError, OSError):
                    return self._cache.get(name, {})
                self._mtime[name] = mt
            return self._cache[name]

    def _get(self, name: str) -> dict:
        return self._maybe_reload(name)

    def _iter_members(self) -> list[dict]:
        rel = self._get("member_relations.json")
        return rel.get("members", rel.get("top_members", []))

    def add_new_member(self, uin: str, name: str) -> bool:
        """Append a brand-new member entry (G9, auto-join).

        Append-only and non-destructive: re-reads the file fresh (mtime
        reload), skips existing uins, never touches other entries. Falls back
        to 群友<uin> when the nickname is empty or already used as an alias.
        Returns True when the file was actually appended.
        """
        uin = str(uin or "").strip()
        if not uin:
            return False
        rel = self._get("member_relations.json")
        members = rel.get("members", [])
        if any(str(m.get("uin", "")) == uin for m in members):
            return False
        alias = (name or "").strip() or f"群友{uin}"
        if any((m.get("alias") or "").strip() == alias for m in members):
            alias = f"群友{uin}"
        entry = {
            "uin": uin,
            "alias": alias,
            "other_names": [],
            "closeness": "new",
            "auto_added": True,
            "first_seen": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            "notes": "",
        }
        members.append(entry)
        rel["members"] = members
        path = self._path("member_relations.json")
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(
                __import__("json").dumps(rel, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            import os
            os.replace(tmp, path)
        except OSError:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            return False
        self._mtime["member_relations.json"] = 0.0  # force reload on next access
        self._get("member_relations.json")
        return True

    def system_prompt(self, local_hour: Optional[int] = None) -> str:
        """固定人设段（WHO/HOW + 规则 + 别名块）。

        ⚠️ 缓存约束（2026-09-13 修正）：这里**不再拼「现在本地时间 HH 时」**。
        时间句原先拼在 system prompt 的**末尾**，而 system prompt 是请求的
        第一个 token 位置 —— 内容一变，其后整段会话前缀全部失效，每小时
        白白废掉一次全量缓存。时间/心情这类逐轮变化的量改由
        ``volatile_line()`` 放进**上下文末尾**的一条 system 消息里，
        只破坏尾部，稳定前缀（人设 + 会话 + 示例块）得以复用。
        ``local_hour`` 参数保留以兼容既有调用点（不再参与拼接）。
        """
        f = self._get("system_prompt_fragments.json")
        parts: list[str] = []
        for key in ("identity", "tone", "vocabulary", "schedule", "relations", "group_context", "personality"):
            v = f.get(key)
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
        rules = f.get("rules") or []
        if rules:
            parts.append("规则：\n- " + "\n- ".join(str(r) for r in rules if r))
        text = "\n\n".join(parts) if parts else "你是这个 QQ 群里的一名普通成员。"

        alias_block = self._build_alias_block()
        if alias_block:
            text += f"\n\n{alias_block}"
        return text

    def volatile_line(self, local_hour: Optional[int] = None, mood: str = "") -> str:
        """逐轮易变信息（时间 + 心情）—— 放在上下文**末尾**专用。

        与固定 system prompt 分离的理由见 ``system_prompt()`` 的缓存说明：
        易变量若留在前缀里，每次变化都会让整段会话前缀 miss。
        """
        lines: list[str] = []
        if local_hour is not None and 0 <= local_hour < 24:
            lines.append(f"现在本地时间 {local_hour:02d} 时。")
        if mood:
            lines.append(f"当前心情：{mood}")
        return "\n".join(lines)

    def _build_alias_block(self) -> str:
        members = self._iter_members()
        if not members:
            return ""

        close_lines: list[str] = []
        known_lines: list[str] = []
        new_lines: list[str] = []
        seen_aliases: set[str] = set()

        for m in members:
            uin = str(m.get("uin", ""))
            alias = (m.get("alias") or "").strip()
            if not uin or not alias:
                continue
            if m.get("notes") == "bot":
                continue
            if alias in seen_aliases:
                continue
            seen_aliases.add(alias)
            other_names = m.get("other_names") or []
            closeness = (m.get("closeness") or "known").strip()

            label = CLOSENESS_LABEL.get(closeness, closeness)
            line = f"  {uin}: {alias}"
            if other_names:
                line += f" (也常被叫作: {'、'.join(other_names)})"
            line += f"  [{label}]"

            if closeness == "close":
                close_lines.append(line)
            elif closeness == "known":
                known_lines.append(line)
            else:
                new_lines.append(line)

        blocks: list[str] = []
        for title, lines in [("熟人", close_lines), ("认识", known_lines), ("新人", new_lines)]:
            if lines:
                blocks.append(f"【{title}】\n" + "\n".join(lines))

        if not blocks:
            return ""
        return "群友识别（按 QQ 号，优先用别名称呼）：\n" + "\n\n".join(blocks)

    def hourly_budget(self, hour: int) -> float:
        h = self._get("my_hourly_distribution.json")
        budgets = h.get("hourly_budget") or {}
        return float(budgets.get(str(hour), 0.0))

    def peak_hours(self) -> set[int]:
        h = self._get("my_hourly_distribution.json")
        shares = h.get("hourly_share") or {}
        if not shares:
            return set()
        vals = [float(v) for v in shares.values()]
        mean = sum(vals) / len(vals)
        return {int(k) for k, v in shares.items() if float(v) >= mean}

    def preferred_alias(self, uin: str) -> str:
        for m in self._iter_members():
            if str(m.get("uin", "")) == uin:
                alias = (m.get("alias") or "").strip()
                if alias:
                    return alias
                return ""
        return ""

    def resolve_uin_from_name(self, name: str) -> str:
        if not name:
            return ""
        for m in self._iter_members():
            uin = str(m.get("uin", ""))
            alias = (m.get("alias") or "").strip()
            if alias and alias == name:
                return uin
            other_names = m.get("other_names") or []
            if name in other_names:
                return uin
        return ""

    def snapshot(self) -> dict[str, dict]:
        return {name: self._get(name) for name in _FILES}
