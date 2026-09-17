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
from typing import NamedTuple, Optional

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

    @staticmethod
    def is_bot_member(m: dict) -> bool:
        """该条目是否为 **bot 账号**（而非真人群友）。

        ## 为什么单独收口（S12，2026-09-14）

        成员表里有 **2 个 bot 账号混在真人中间**（星野、苗爷），原先靠
        `m.get("notes") == "bot"` 排除。但 `notes` 要**让给"群友描述"**
        （LLM 生成 + 人工可改），所以标记迁到 `kind`。

        兼容读取：**`kind` 或 `notes` 任一为 "bot" 都算 bot** ——
        迁移期双写，两边都认。**必须两边都查**：漏掉会让 bot 账号被当成真人
        暴露给 LLM（关系图谱、熟悉度、描述全都会带上它）。
        """
        return str(m.get("kind") or "").strip() == "bot" \
            or str(m.get("notes") or "").strip() == "bot"

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

        # 🔴 S9（2026-09-14）：**别名/关系图谱不再拼进这里**。
        #
        # 实测问题：这块随新成员入列持续增长（一天 +8~11 次、每次约 +23 字符），
        # 而它原本拼在 system prompt **末尾** → 每次增长都让**其后全部内容**
        # （session 全量历史，实测 8 万 token）的缓存失效：
        #   提示词变更那次: prompt 65560 → cached 3840 (5.9%) → other **61720** 全价
        #   正常调用:      prompt 82475 → cached 82176 (99.6%) → other 仅 299
        # 现在由 `relations_block()` 单独提供，调用方把它作为**独立的一条
        # system 消息**放在人格之后 —— 它变时只废自己之后的部分，且下一个调用
        # 就能重新缓存 session。
        return text

    def relations_lines(self) -> list[tuple[str, str]]:
        """关系图谱的逐行形式 ``[(uin, line), ...]``（按文件顺序）。

        S10 新增：需要**按 uin 算增量**（新成员入列时只把新增的人以一条
        system 消息追加到上下文尾部，而不是整块重发 —— 后者会让其后
        session 前缀失效）。
        """
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for m in self._iter_members():
            uin = str(m.get("uin", ""))
            alias = (m.get("alias") or "").strip()
            if not uin or not alias or self.is_bot_member(m):
                continue
            if alias in seen:
                continue
            seen.add(alias)
            other_names = m.get("other_names") or []
            closeness = (m.get("closeness") or "known").strip()
            label = CLOSENESS_LABEL.get(closeness, closeness)
            line = f"  {uin}: {alias}"
            if other_names:
                line += f" (也常被叫作: {'、'.join(other_names)})"
            line += f"  [{label}]"
            out.append((uin, line))
        return out

    def relations_delta(self, known: dict) -> tuple[list[str], list[str]]:
        """对比"已透露状态"，算出本次需要追加的增量。返回 ``(新群友行, 变化行)``。

        ## 为什么需要（S10，用户 2026-09-14 设计）

        关系图谱原先是**整块**进上下文前缀。它一变（新成员入列，实测一天 8~11 次），
        **它之后的所有内容**（示例块 + session 全量 8 万 token）前缀全不匹配 →
        按全价重算（实测 `other=61720`）。

        改为**追加式**：图谱块本身不动，新增/变化以一条 system 消息追加到
        **session 尾部** —— 前面逐字节不变，只有那一条消息是新的。
        代价从"图谱之后的一切"缩小到"就那一条"。

        ``known`` 是 ``{uin: 该行文本}`` 的已透露状态。**行文本变化也算增量** ——
        否则人工调整亲疏（如把某人 new 调成 known）对 LLM 永远不可见。

        首次运行（``known`` 为空）应返回空 —— 初始块已作为前缀存在，
        不该在第一次调用时灌一整块历史（那是几百行的浪费）。
        """
        if not known:
            return [], []
        new_lines: list[str] = []
        changed: list[str] = []
        for uin, line in self.relations_lines():
            prev = known.get(uin)
            if prev is None:
                new_lines.append(line)
            elif prev != line:
                changed.append(line)
        return new_lines, changed

    @staticmethod
    def filter_already_announced(lines: list[str], frozen_block: str) -> list[str]:
        """丢掉**头部冻结块里已经写着**的行（B-032，2026-09-17 独立核验发现）。

        会话边界（日轮转后新会话的首轮）上，头部块用**当时活的**图谱冻结 ——
        它已经含了新成员；而增量仍按 `known` 算 → 同一变更 head 与 tail **各讲一遍**，
        且尾部那条会留在会话里一整天（每轮都被 LLM 看两次）。

        头部已经写着的行不必再追加。判据是**整行精确匹配**（把冻结块按行切开做集合）：
        两处都是 `relations_lines()` 的同一份行文本，故可直接比对。

        ⚠️ 为什么不用子串比对（独立核验指出）：别名里若字面内嵌另一个成员的整行
        （如 alias = `甲  2: 乙  [认识]`），子串判据会把成员 2 的**真实新行**误判成
        "已播报"而丢掉。整行集合比对没有这个误杀面。

        空行剔除；`frozen_block` 为空时原样返回（未冻结/未启用时不改变行为）。
        """
        if not frozen_block:
            return list(lines)
        frozen_lines = {ln for ln in str(frozen_block).splitlines() if ln}
        return [ln for ln in lines if ln and ln.strip() and ln not in frozen_lines]

    def relations_block(self) -> str:
        """关系图谱（**独立块**，与人格分离）。

        为什么独立：见 ``system_prompt()`` 里的注释 —— 它增长，人格不增长。
        两者拼在一起时，"增长的块"会把"恒定的块"一起拖进失效区。
        """
        lines = self.relations_lines()
        if not lines:
            return ""
        return ("群友识别（按 QQ 号，优先用别称呼叫；[熟人] 是关系最近的人）：\n"
                + "\n".join(line for _, line in lines))

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
        """关系图谱块 —— **按文件顺序输出，保证"只在尾部追加"**。

        ## 为什么不再按亲疏分段分组（S9，2026-09-14）

        原实现把成员分进 `【熟人】/【认识】/【新人】` 三段再输出。后果：块内顺序
        与文件顺序**不一致**，任何中段插入都会让其后全部内容位移 →
        整块之后的前缀（session 全量，实测 8 万 token）缓存失效。

        而实测 `member_relations.json` 的**文件顺序本身就是追加式的**：

            [0..109]   人工策展的 close/known 混合（含少量 new）
            [110..184] 全部 `auto_added=True`，清一色 new —— 自动入列追加在尾部

        所以**按文件顺序输出**即天然满足"只在尾部追加"：新成员永远出现在块尾，
        前面逐字节不变 → 前缀稳定（这正是用户要的 skill-catalog 语义）。

        取舍：不再有 `【熟人】` 分段标题。但亲疏**没丢** —— 每个成员行的
        `[熟人]/[认识]/[新人]` 标签保留，LLM 照样能判断关系远近。
        """
        members = self._iter_members()
        if not members:
            return ""
        lines: list[str] = []
        seen_aliases: set[str] = set()
        # ⚠️ 不排序、不分组：严格按 `_iter_members()` 的顺序（= 文件顺序）
        for m in members:
            uin = str(m.get("uin", ""))
            alias = (m.get("alias") or "").strip()
            if not uin or not alias:
                continue
            if self.is_bot_member(m):
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
            lines.append(line)
        if not lines:
            return ""
        return ("群友识别（按 QQ 号，优先用别名称呼；列表按入群先后排列，"
                "**[熟人] 是关系最近的人**）：\n" + "\n".join(lines))

    def _hourly_local(self) -> dict:
        """按**本地小时**索引的 hourly 分布（兼容历史 UTC 文件）。

        🔴 2026-09-13 实测踩到并修复的静默 bug：
          `tools/analyze_style.py` 用 ``dt.hour`` 统计（**UTC**），文件里也写了
          `"tz_note": "Counts are UTC. The plugin should shift to its local TZ on load."`
          —— 但**下游从来没做这个转换**。于是插件在**本地 20:33** 读的是
          **UTC 20 点的预算 = 0.34**（那条实际是本地凌晨 4 点），
          而 active_interjection 每条回复消耗 1.0 → **预算永远不够 → 主动插话
          在本地 10:00–24:00 被完全压制**（恰好是群最活跃的时段）。
          @ 回复不受预算限制，所以之前没被发现。

        这里在**读取时**统一成"按本地小时索引"：
          - 新文件（生成端已改为本地时）→ 直接用
          - 旧文件（含 tz_note / 无 tz 标记）→ 按 tz_offset 平移
        """
        h = self._get("my_hourly_distribution.json")
        tz = str(h.get("tz") or "").lower()
        legacy_utc = (not tz) and bool(h.get("tz_note"))
        if tz == "local" or (not legacy_utc):
            return h
        # ⚠️ 不能用 `h.get("tz_offset_hours", 8) or 8` —— 合法的偏移 0（UTC）
        # 会被 `or` 当成缺省值静默变成 +8。必须区分"键不存在"与"值为 0"。
        _raw_off = h.get("tz_offset_hours", None)
        try:
            off = 8 if _raw_off is None else int(_raw_off)
        except (TypeError, ValueError):
            off = 8

        out = dict(h)
        for key in ("hourly_message_count", "hourly_share", "hourly_budget"):
            src = h.get(key) or {}
            if not isinstance(src, dict):
                continue
            shifted: dict[str, object] = {}
            for k, v in src.items():
                try:
                    hk = (int(k) + off) % 24
                except (TypeError, ValueError):
                    continue
                shifted[str(hk)] = v
            out[key] = shifted
        return out

    def hourly_budget(self, hour: int) -> float:
        budgets = self._hourly_local().get("hourly_budget") or {}
        return float(budgets.get(str(hour), 0.0))

    def peak_hours(self) -> set[int]:
        """活跃时段（**本地小时**）。同样走 `_hourly_local()` 的兼容转换。"""
        h = self._hourly_local()
        shares = h.get("hourly_share") or {}
        if not shares:
            return set()
        vals = [float(v) for v in shares.values()]
        mean = sum(vals) / len(vals)
        return {int(k) for k, v in shares.items() if float(v) >= mean}

    def resolve_member_name(self, name: str) -> tuple[Optional[str], str]:
        """把模型写的**名字**解析成 uin。返回 ``(uin 或 None, 原因)``。

        ## 为什么需要（S7 拍一拍）

        `[poke:QQ号]` 要求模型从 185 人名单里背出正确号码 —— 比"叫出昵称"难得多，
        而**戳错人是对外可见的社交事故**。实测模型能准确叫出 `虾鱼丸`/`焦糖`/
        `小闵`/`纱纱`，所以改成 `[poke:名字]`，由服务端解析。

        ## 严格优先于宽松（用户 2026-09-14 拍板）

        池子里有大量互含简称（`小clove`/`clove`、`虾鱼丸`/`虾虾`/`私虾`）。
        解析规则：**精确匹配** `alias` → 精确匹配 `other_names` → 都不唯一/都没有
        **即返回 None**。不猜、不做模糊匹配。代价是"模型用了未收录的昵称"时戳不出去，
        这个代价可接受；戳错人的代价不可接受。
        """
        key = (name or "").strip()
        if not key:
            return None, "empty_name"
        low = key.lower()
        primary: list[str] = []
        secondary: list[str] = []
        for m in self._iter_members():
            uin = str(m.get("uin") or "")
            if not uin:
                continue
            alias = str(m.get("alias") or "").strip()
            if alias and alias.lower() == low:
                primary.append(uin)
                continue
            for n in (m.get("other_names") or []):
                if str(n).strip().lower() == low:
                    secondary.append(uin)
                    break
        if len(primary) == 1:
            return primary[0], "alias"
        if len(primary) > 1:
            return None, f"ambiguous_alias({len(primary)})"
        if len(secondary) == 1:
            return secondary[0], "other_name"
        if len(secondary) > 1:
            return None, f"ambiguous_other_name({len(secondary)})"
        return None, "unknown_name"

    # 等级序（与 services/familiarity.py 的 CLOSENESS_ORDER 同源语义）
    _CLOSENESS_RANK = {"new": 0, "known": 1, "close": 2}

    def set_closeness(self, uin: str, value: str, *, force: bool = False) -> bool:
        """写入某成员的亲疏等级。返回是否真的改了。

        ``force=False``（默认）时**只允许提升** —— 这是写入层的最后一道闸：
        即便调用方判断失误，也不会把等级降下来（"只升不降"是三处共同保证的：
        提案生成过滤 / 批准时二次校验 / 这里）。

        ``force=True`` 给人工路径用（用户明确"人工可以绕过所有限制"）。

        原子写 + 只改目标条目的 `closeness` 字段，**不动** human 编辑的其他字段。
        """
        import os as _os
        u = str(uin or "").strip()
        v = str(value or "").strip()
        if not u or v not in self._CLOSENESS_RANK:
            return False
        rel = self._get("member_relations.json")
        members = rel.get("members", [])
        target = None
        for m in members:
            if str(m.get("uin") or "") == u:
                target = m
                break
        if target is None:
            return False
        cur = str(target.get("closeness") or "").strip()
        if cur == v:
            return False
        if not force and self._CLOSENESS_RANK[v] <= self._CLOSENESS_RANK.get(cur, -1):
            return False          # 非提升 → 拒绝
        target["closeness"] = v
        rel["members"] = members
        path = self._path("member_relations.json")
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(rel, ensure_ascii=False, indent=2), encoding="utf-8")
        _os.replace(tmp, path)
        self._cache.pop("member_relations.json", None)   # 让下次读取拿到新值
        return True

    def member_closeness(self, uin: str) -> str:
        """返回该成员的亲疏等级（``close``/``known``/``new``）；未知返回空串。"""
        u = str(uin or "")
        for m in self._iter_members():
            if str(m.get("uin") or "") == u:
                return str(m.get("closeness") or "")
        return ""

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


# ---------------------------------------------------------------------------
# 关系增量决策（B-032 收口）：从 main 下沉为**可导入的纯函数**
# ---------------------------------------------------------------------------

class RelationsDeltaPlan(NamedTuple):
    """一次关系增量的决策结果。"""

    text: str
    """要追加到会话尾部的 system 文本；空串 = 本轮不追加。"""
    known: dict
    """应写入状态的"已透露"快照（调用方据此决定是否落盘）。"""
    added: int
    changed: int
    first_run: bool
    """True = 状态为空（首次运行或状态被删/重置）→ 调用方需做头部对齐。"""


def plan_relations_delta(style, known: dict, frozen_block: str = "") -> RelationsDeltaPlan:
    """决定"这一轮要不要往会话尾部追加关系增量、追加什么"（纯函数，可离线单测）。

    为什么要抽出来（2026-09-17 独立核验建议）：这段决策此前整个埋在
    `main._relations_delta_block()` 里，而 **main.py 不被任何测试导入** ——
    核验者只能靠 AST 抽取才验到。下沉后 `test_style_profile.py` 能直接锁住四条出口。

    四条出口：
      ① `known` 为空（首次/状态被重置）→ 不追加，`first_run=True`（调用方需对齐头部块）
      ② 变化全被头部冻结块吸收（B-032）→ 不追加，但仍返回新的 `known`
      ③ 有新增 → 追加"新加入或新认识的群友"段
      ④ 有行文本变化 → 追加"关系/称呼有变化"段
    """
    cur = {str(uin): line for uin, line in style.relations_lines()}
    if not known:
        return RelationsDeltaPlan("", cur, 0, 0, first_run=True)
    new_lines, changed = style.relations_delta(known)
    # 🔴 B-032：头部冻结块里已经写着的行不再往尾部讲一遍
    new_lines = style.filter_already_announced(new_lines, frozen_block)
    changed = style.filter_already_announced(changed, frozen_block)
    if not new_lines and not changed:
        return RelationsDeltaPlan("", cur, 0, 0, first_run=False)
    parts: list[str] = ["［群友识别更新］"]
    if new_lines:
        parts.append("新加入或新认识的群友：")
        parts.extend(new_lines)
    if changed:
        parts.append("以下群友的关系/称呼有变化（以本行为准）：")
        parts.extend(changed)
    return RelationsDeltaPlan("\n".join(parts), cur, len(new_lines), len(changed),
                              first_run=False)
