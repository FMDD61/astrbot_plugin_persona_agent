"""TopicBank — G12 冷场主动话题（纯 stdlib，可离线单测）。

数据文件（运行时 data_dir 下，人工可编辑、mtime 热重载）：
  topic_bank.json  话题池，结构见 IMPLEMENTATION_PLAN §10：
    [{"id","content","category","context_hints","priority" 1-5,
      "min_silence_seconds","enabled"}]
  topic_sent.json  已发送归档（追加写，原子落盘）：历史坏例/已用话题
    由人工置 enabled=false 或移出文件即规避（发送后自动归档 id）。

GROUP ISOLATION (2026-09-07, A7 2b):
  - 话题池 topic_bank.json 全群共享（素材通用）
  - 已发送归档 topic_sent.json 按 group_id 隔离：pick(group_id=...) 只排除
    本群已发话题，freshness 按本群上次发送计；mark_sent(group_id=...) 按群归档。
    旧记录无 group_id 视为 legacy，不参与排除（本仓从未启用，无迁移负担）。
  - group_id 缺省 "" 时维持旧全局行为（兼容旧测试/调用方）。

选择分数（§10）: 0.45*silence + 0.25*priority + 0.20*context_similarity + 0.10*freshness
  - silence 按 min_silence_seconds 归一（>1 封顶 1.0）
  - priority 按候选最大 priority 归一
  - context_similarity = 命中的 context_hints 数 / hints 总数（无 hint 记 0）
  - freshness：距本群上次发送天数归一到 7 天窗口；从未发送 = 1.0
无可发话题时 pick 返回 None —— main 必须保持沉默（红线 #8）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_TOPIC_FILE = "topic_bank.json"
_SENT_FILE = "topic_sent.json"
_FRESH_WINDOW_SEC = 7 * 86400.0


@dataclass
class Topic:
    id: str
    content: str
    category: str = ""
    context_hints: list[str] = field(default_factory=list)
    priority: int = 3
    min_silence_seconds: int = 180
    enabled: bool = True


class TopicBank:
    def __init__(self, data_dir: str) -> None:
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._topic_mtime: float = 0.0
        self._topics: list[Topic] = []
        # group_id -> {topic_id -> latest sent epoch}
        self._sent: dict[str, dict[str, float]] = {}
        self._load_topics()
        self._load_sent()

    # ------------------------------------------------------------------ public

    def pick(
        self,
        *,
        now: Optional[float] = None,
        silence_sec: float = 0.0,
        live_text: str = "",
        group_id: str = "",
    ) -> Optional[Topic]:
        now = now if now is not None else time.time()
        self._maybe_reload_topics()
        sent = self._sent_for(group_id)
        candidates = [
            t for t in self._topics
            if t.enabled and t.id not in sent
            and silence_sec >= float(t.min_silence_seconds)
        ]
        if not candidates:
            return None
        max_prio = max(float(t.priority) for t in candidates)

        def score(t: Topic) -> float:
            min_s = max(float(t.min_silence_seconds), 1.0)
            s_norm = min(silence_sec / min_s, 1.0)
            p_norm = t.priority / max(max_prio, 1.0)
            hints = [h for h in t.context_hints if h]
            ctx = (live_text or "").lower()
            hits = sum(1 for h in hints if h.lower() in ctx)
            sim = hits / max(len(hints), 1) if hints else 0.0
            last = sent.get(t.id)
            fresh = 1.0 if last is None else min(max(now - last, 0.0) / _FRESH_WINDOW_SEC, 1.0)
            return 0.45 * s_norm + 0.25 * p_norm + 0.20 * sim + 0.10 * fresh

        return max(candidates, key=lambda t: (score(t), t.priority, -len(t.id)))

    def mark_sent(self, topic: Topic, *, now: Optional[float] = None, reason: str = "cold_start", group_id: str = "") -> None:
        now = now if now is not None else time.time()
        self._sent_for(group_id)[topic.id] = now
        records = self._load_sent_records()
        records.append({
            "id": topic.id,
            "content": topic.content,
            "category": topic.category,
            "sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "sent_at_epoch": round(now, 3),
            "reason": reason,
            "group_id": str(group_id or ""),
        })
        self._save_sent(records)

    def snapshot(self) -> dict:
        return {
            "topics": [{"id": t.id, "content": t.content, "category": t.category,
                        "priority": t.priority, "enabled": t.enabled} for t in self._topics],
            "sent_by_group": {g: {k: round(v, 3) for k, v in s.items()}
                              for g, s in self._sent.items()},
        }

    # ------------------------------------------------------------------ private

    def _sent_for(self, group_id: str) -> dict[str, float]:
        g = str(group_id or "")
        if g not in self._sent:
            self._sent[g] = {}
        return self._sent[g]

    def _load_topics(self) -> None:
        path = self._dir / _TOPIC_FILE
        try:
            self._topic_mtime = path.stat().st_mtime_ns
            data = json.loads(path.read_text("utf-8")) or []
        except Exception:
            self._topics = []
            return
        topics: list[Topic] = []
        for item in data or []:
            if not isinstance(item, dict) or not item.get("id") or not item.get("content"):
                continue
            topics.append(Topic(
                id=str(item["id"]),
                content=str(item["content"]).strip(),
                category=str(item.get("category") or ""),
                context_hints=[str(h) for h in (item.get("context_hints") or [])],
                priority=int(item.get("priority", 3)),
                min_silence_seconds=int(item.get("min_silence_seconds", 180)),
                enabled=bool(item.get("enabled", True)),
            ))
        self._topics = [t for t in topics if t.content]

    def _maybe_reload_topics(self) -> None:
        path = self._dir / _TOPIC_FILE
        try:
            mt = path.stat().st_mtime_ns
        except OSError:
            return
        if mt > self._topic_mtime:
            self._load_topics()

    def _load_sent(self) -> None:
        for rec in self._load_sent_records():
            try:
                gid = str(rec.get("group_id") or "")
                self._sent_for(gid)[str(rec["id"])] = float(rec.get("sent_at_epoch") or 0.0)
            except (KeyError, TypeError, ValueError):
                continue

    def _load_sent_records(self) -> list[dict]:
        path = self._dir / _SENT_FILE
        try:
            data = json.loads(path.read_text("utf-8")) or []
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save_sent(self, records: list[dict]) -> None:
        path = self._dir / _SENT_FILE
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.rename(path)
        except OSError:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
