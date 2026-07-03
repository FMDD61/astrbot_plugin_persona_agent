"""DreamJob — weekly cron memory consolidation.

③ 进化层: 离线分析 MemoryStore 中的交互数据, 输出风格漂移报告.
只建议, 不覆盖人工编辑的 member_relations.json.

Closeness tiers (from data):
  - new: < 60 active days in last 90
  - known: 60-89 active days, daily avg >= 2.0
  - close: >= 90 active days, daily avg >= 2.0

Upgrade rules (auto-suggested, based purely on data):
  - new → known: >= 60 active days AND max_consecutive >= 60
  - known → close: >= 90 active days AND max_consecutive >= 90

Downgrade rules:
  - known/new → suggested_downgrade: >= 90 days since last interaction
  - close: never auto-downgraded
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .memory_store import MemoryStore, RelationEdge


@dataclass
class RelationChange:
    from_alias: str
    to_alias: str
    suggested_closeness: str
    reason: str
    requires_confirmation: bool = False


@dataclass
class TopicTrend:
    text: str
    this_week: int
    last_week: int
    change_pct: float


@dataclass
class DreamReport:
    generated_at: str = ""
    suggested_upgrades: list[RelationChange] = field(default_factory=list)
    suggested_downgrades: list[RelationChange] = field(default_factory=list)
    topic_trends: list[TopicTrend] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


class DreamJob:
    def __init__(self, store: MemoryStore, data_dir: str) -> None:
        self._store = store
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> DreamReport:
        now = time.time()

        edges = self._store.search_edges("", since_days=90, limit=100000)
        if not edges:
            return self._empty_report(now)

        member_stats = self._build_member_stats(edges, now)
        upgrades = self._suggest_upgrades(member_stats)

        inactive_edges = self._store.search_edges("", since_days=365, limit=100000)
        all_stats = self._build_member_stats(inactive_edges, now, window_days=365)
        downgrades = self._suggest_downgrades(all_stats)
        topics = self._detect_topic_trends(now)

        report = DreamReport(
            generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            suggested_upgrades=upgrades,
            suggested_downgrades=downgrades,
            topic_trends=topics,
            stats={
                "total_edges_90d": len(edges),
                "total_edges_365d": len(inactive_edges),
                "members_analyzed": len(member_stats),
                "total_entities": self._store.stats().get("entities", 0),
            },
        )

        self._save(report)
        return report

    def _empty_report(self, now: float) -> DreamReport:
        report = DreamReport(
            generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            stats={"total_edges_90d": 0, "members_analyzed": 0},
        )
        self._save(report)
        return report

    # ---- member analysis ----

    def _build_member_stats(
        self, edges: list[RelationEdge], now: float, window_days: int = 90
    ) -> dict[str, dict]:
        """Build per-member stats: {alias: {active_days, daily_avg, max_consecutive, last_ts}}."""
        members: dict[str, dict] = {}
        day_sets: dict[str, set] = {}

        for e in edges:
            if e.type not in ("mentions", "talks_about"):
                continue
            alias = e.from_alias
            if alias not in members:
                members[alias] = {"count": 0, "last_ts": 0.0, "first_ts": e.ts}
                day_sets[alias] = set()
            d = members[alias]
            d["count"] += 1
            d["last_ts"] = max(d["last_ts"], e.ts)
            d["first_ts"] = min(d["first_ts"], e.ts)
            day_sets[alias].add(int(e.ts // 86400))

        for alias, days in day_sets.items():
            sorted_days = sorted(days)
            consecutive = 1
            max_consecutive = 1
            for i in range(1, len(sorted_days)):
                if sorted_days[i] - sorted_days[i - 1] <= 1:
                    consecutive += 1
                    max_consecutive = max(max_consecutive, consecutive)
                else:
                    consecutive = 1
            span_days = max(1, (now - members[alias]["first_ts"]) // 86400 + 1)
            members[alias]["active_days"] = len(days)
            members[alias]["daily_avg"] = members[alias]["count"] / span_days
            members[alias]["max_consecutive"] = max_consecutive
            members[alias]["days_since_last"] = int(
                (now - members[alias]["last_ts"]) // 86400
            )

        return members

    @staticmethod
    def _data_closeness(stats: dict) -> str:
        active = stats.get("active_days", 0)
        daily = stats.get("daily_avg", 0.0)
        if active >= 90 and daily >= 2.0:
            return "close"
        if active >= 60 and daily >= 2.0:
            return "known"
        return "new"

    # ---- upgrade / downgrade ----

    def _suggest_upgrades(
        self, member_stats: dict[str, dict]
    ) -> list[RelationChange]:
        changes: list[RelationChange] = []
        for alias, s in member_stats.items():
            suggested = self._data_closeness(s)
            cons = s.get("max_consecutive", 0)
            daily = s.get("daily_avg", 0.0)
            active = s.get("active_days", 0)

            if suggested == "close":
                changes.append(RelationChange(
                    from_alias=alias, to_alias="<group>",
                    suggested_closeness="close",
                    reason=f"活跃 {active} 天, 日均 {daily:.1f}, 连续 {cons} 天",
                ))
            elif suggested == "known":
                changes.append(RelationChange(
                    from_alias=alias, to_alias="<group>",
                    suggested_closeness="known",
                    reason=f"活跃 {active} 天, 日均 {daily:.1f}, 连续 {cons} 天",
                ))
            else:
                changes.append(RelationChange(
                    from_alias=alias, to_alias="<group>",
                    suggested_closeness="new",
                    reason=f"活跃仅 {active} 天",
                ))
        return changes

    def _suggest_downgrades(
        self, member_stats: dict[str, dict]
    ) -> list[RelationChange]:
        changes: list[RelationChange] = []
        for alias, s in member_stats.items():
            days_inactive = s.get("days_since_last", 0)
            suggested = self._data_closeness(s)

            if suggested == "close":
                continue

            if days_inactive >= 90:
                changes.append(RelationChange(
                    from_alias=alias, to_alias="<group>",
                    suggested_closeness="inactive",
                    reason=f"{days_inactive} 天无互动",
                    requires_confirmation=True,
                ))
        return changes

    # ---- topic trends ----

    def _detect_topic_trends(self, now: float) -> list[TopicTrend]:
        this_week = self._store.get_hot_topics("", since_days=7)
        this_count: dict[str, int] = {}
        for t in this_week:
            this_count[t] = this_count.get(t, 0) + 1

        last_week = self._store.get_hot_topics("", since_days=14)
        last_count: dict[str, int] = {}
        for t in last_week:
            last_count[t] = last_count.get(t, 0) + 1
            if t in this_count:
                last_count[t] -= 1

        all_topics = set(this_count) | set(last_count)
        trends = []
        for topic in sorted(all_topics, key=lambda t: -(this_count.get(t, 0))):
            t_cnt = this_count.get(topic, 0)
            l_cnt = last_count.get(topic, 0)
            if t_cnt == 0 and l_cnt == 0:
                continue
            change = 100.0 if l_cnt == 0 and t_cnt > 0 else (
                -100.0 if t_cnt == 0 else ((t_cnt - l_cnt) / max(l_cnt, 1)) * 100
            )
            trends.append(TopicTrend(
                text=topic, this_week=t_cnt, last_week=l_cnt, change_pct=round(change, 1),
            ))
        return trends

    # ---- output ----

    def _save(self, report: DreamReport) -> None:
        path = self._data_dir / "style_drift_report.json"
        data = {
            "generated_at": report.generated_at,
            "suggested_upgrades": [
                {
                    "from": c.from_alias,
                    "suggested_closeness": c.suggested_closeness,
                    "reason": c.reason,
                }
                for c in report.suggested_upgrades
            ],
            "suggested_downgrades": [
                {
                    "from": c.from_alias,
                    "suggested_closeness": c.suggested_closeness,
                    "reason": c.reason,
                    "requires_confirmation": c.requires_confirmation,
                }
                for c in report.suggested_downgrades
            ],
            "topic_trends": [
                {
                    "text": t.text,
                    "this_week": t.this_week,
                    "last_week": t.last_week,
                    "change_pct": t.change_pct,
                }
                for t in report.topic_trends
            ],
            "stats": report.stats,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.rename(path)

    @staticmethod
    def load_last_report(data_dir: str) -> Optional[DreamReport]:
        path = Path(data_dir) / "style_drift_report.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text("utf-8"))
        report = DreamReport(generated_at=data.get("generated_at", ""))
        for u in data.get("suggested_upgrades", []):
            report.suggested_upgrades.append(RelationChange(
                from_alias=u["from"], to_alias="",
                suggested_closeness=u["suggested_closeness"],
                reason=u["reason"],
            ))
        for d in data.get("suggested_downgrades", []):
            report.suggested_downgrades.append(RelationChange(
                from_alias=d["from"], to_alias="",
                suggested_closeness=d["suggested_closeness"],
                reason=d["reason"],
                requires_confirmation=d.get("requires_confirmation", True),
            ))
        for t in data.get("topic_trends", []):
            report.topic_trends.append(TopicTrend(
                text=t["text"], this_week=t["this_week"],
                last_week=t["last_week"], change_pct=t["change_pct"],
            ))
        report.stats = data.get("stats", {})
        return report
