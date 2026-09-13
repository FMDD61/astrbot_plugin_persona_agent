"""MemoryStore — SQLite-backed ADD-only entity + relation graph with FTS5.

② 存储层: 每条消息到达时异步抽取实体/关系, 写入图。
ADD-only 模式: 边永不覆盖, 只追加时间戳。检索时按时间衰减加权。

SQLite schema:
  entities: (id, alias, type, text, ts)
  edges: (id, from_alias, to_alias, type, ts, properties_json)
  entity_fts: FTS5 virtual table on entities(text) for BM25 keyword search

所有方法为同步 (SQLite 操作 < 1ms), 调用方用 asyncio.to_thread() 包 fire-and-forget.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import kg_stopwords


@dataclass
class MemoryEvent:
    speaker_alias: str
    text: str
    group_id: str
    ts: float = field(default_factory=time.time)


@dataclass
class EntityLink:
    alias: str
    type: str
    text: str


@dataclass
class RelationEdge:
    from_alias: str
    to_alias: str
    type: str
    ts: float
    properties: dict = field(default_factory=dict)


@dataclass
class RelationResult:
    interaction_count: int = 0
    last_interaction_ts: float = 0.0
    common_topics: list[str] = field(default_factory=list)
    closeness: str = "new"


_SQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alias TEXT NOT NULL,
    type TEXT NOT NULL,
    text TEXT NOT NULL,
    ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_alias TEXT NOT NULL,
    to_alias TEXT NOT NULL,
    type TEXT NOT NULL,
    ts REAL NOT NULL,
    properties_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_alias, ts);
CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_alias, ts);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(type, ts);
CREATE INDEX IF NOT EXISTS idx_entities_alias ON entities(alias, ts);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type, ts);

CREATE VIRTUAL TABLE IF NOT EXISTS entity_fts USING fts5(
    alias, type, text, content=entities, content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS entities_ai AFTER INSERT ON entities BEGIN
    INSERT INTO entity_fts(rowid, alias, type, text)
    VALUES (new.id, new.alias, new.type, new.text);
END;

CREATE TRIGGER IF NOT EXISTS entities_ad AFTER DELETE ON entities BEGIN
    INSERT INTO entity_fts(entity_fts, rowid, alias, type, text)
    VALUES ('delete', old.id, old.alias, old.type, old.text);
END;
"""


class MemoryStore:
    """SQLite-backed ADD-only entity + relation graph."""

    def __init__(
        self,
        data_dir: str,
        *,
        topic_min_occurrences: int = 2,
        keyword_fn=None,
    ) -> None:
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._db_path = str(self._dir / "memory_store.db")
        self._lock = threading.Lock()
        # B-014 门槛②：topic 至少出现 N 次才建 talks_about 边（1 = 关闭门槛，
        # 退回旧行为）。实测 66.3% 的 topic 只出现一次 —— 那是一次性噪声。
        self._topic_min_occurrences = max(1, int(topic_min_occurrences))
        # 观测计数（进程生命周期）
        self._topics_deferred = 0    # 因门槛②未建边的 topic
        self._image_desc_skipped = 0  # 因门槛①跳过 topic 抽取的消息数
        self._stopword_blocked = 0    # 因停用词被拦下建边的 topic 次数
        # 取词函数可注入：生产用 jieba，测试可注入确定性桩 —— 否则"门槛"逻辑
        # 的测试会因环境缺 jieba 而空转（2026-09-13 实测本机即如此）。
        self._keyword_fn = keyword_fn
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_schema(self) -> None:
        with self._get_conn() as conn:
            conn.executescript(_SQL_SCHEMA)

    # ---- ingest ----

    def ingest(self, event: MemoryEvent) -> None:
        """抽取实体/关系, 写入图。同步 (< 5ms), 调用方包 asyncio.to_thread().

        B-014（2026-09-13）两道质量门 —— 对齐 RAG 语料的第二轮清洗思路：

        ① **图片描述不入 topic**：含 ``（配图：`` 的消息跳过关键词抽取。
           描述是"眼前信息"（给 RP 看这一轮发生了什么），不是"值得长期记住的
           话题"；且几百字描述进 jieba 必然持续产出噪声实体。
        ② **只出现一次不入图**：topic 首次出现只记实体行（待观察），
           **第二次出现才建 `talks_about` 边** → 一次性噪声不进图结构。
           实测 66.3% 的 topic 实体只出现过一次。

        两道门都不影响 member 实体与 `mentions` 边（@ 关系照常记录）。
        """
        entities = self._extract_entities(event)
        with self._lock, self._get_conn() as conn:
            for e in entities:
                conn.execute(
                    "INSERT INTO entities(alias, type, text, ts) VALUES (?,?,?,?)",
                    (e.alias, e.type, e.text, event.ts),
                )
            if entities:
                member_entities = [e for e in entities if e.type == "member"]
                topic_entities = [e for e in entities if e.type == "topic"]
                speaker = next((e for e in member_entities if e.alias == event.speaker_alias), None)
                if speaker:
                    for other in member_entities:
                        if other.alias != speaker.alias:
                            conn.execute(
                                "INSERT INTO edges(from_alias, to_alias, type, ts, properties_json) VALUES (?,?,?,?,?)",
                                (speaker.alias, other.alias, "mentions", event.ts, "{}"),
                            )
                    for topic in topic_entities:
                        # 门①补漏（2026-09-13 实测发现）：停用词**永不建边**。
                        # 历史清洗只删了边、没删实体行，而门槛②看的是实体行计数
                        # —— 残留的 `配图`(1526 行) 一出现就 ≥2 → 立刻重新建边。
                        # 故判据必须包含"不是停用词"，而不只是"见过两次"。
                        if kg_stopwords.is_stopword(topic.alias):
                            self._stopword_blocked += 1
                            continue
                        # 门槛②：本条之前是否已见过该 topic（含本次刚插的行）
                        if self._topic_min_occurrences > 1:
                            seen = conn.execute(
                                "SELECT COUNT(*) FROM entities WHERE type='topic' AND alias=?",
                                (topic.alias,),
                            ).fetchone()
                            if not seen or seen[0] < self._topic_min_occurrences:
                                self._topics_deferred += 1
                                continue
                        conn.execute(
                            "INSERT INTO edges(from_alias, to_alias, type, ts, properties_json) VALUES (?,?,?,?,?)",
                            (speaker.alias, topic.alias, "talks_about", event.ts, "{}"),
                        )

    def _extract_entities(self, event: MemoryEvent) -> list[EntityLink]:
        entities: list[EntityLink] = []
        if event.speaker_alias:
            entities.append(EntityLink(
                alias=event.speaker_alias, type="member",
                text=event.speaker_alias,
            ))
        at_pattern = __import__("re").findall(r"@(\S{1,16})", event.text)
        for name in at_pattern:
            alias = name
            entities.append(EntityLink(alias=alias, type="member", text=name))
        # 门槛①：图片描述（含失败占位）整条不抽 topic —— 描述是眼前信息，
        # 不是长期话题；且长描述进 jieba 会持续产出噪声实体。
        if kg_stopwords.carries_image_description(event.text):
            self._image_desc_skipped += 1
            return entities
        keywords = (
            self._keyword_fn(event.text)
            if self._keyword_fn is not None
            else self._extract_keywords(event.text)
        )
        for kw in keywords:
            entities.append(EntityLink(
                alias=kw, type="topic", text=kw,
            ))
        return entities

    @staticmethod
    def _extract_keywords(text: str, topk: int = 3) -> list[str]:
        try:
            import jieba.analyse
        except ImportError:
            return []
        tags = jieba.analyse.extract_tags(text, topK=topk, withWeight=False)
        # B-014：停用词/结构词过滤（系统占位符切出的词 + 无信息量通用词）
        return kg_stopwords.filter_keywords(t for t in tags if len(t) >= 2)

    # ---- query: entities ----

    def get_entities(self, text: str) -> list[EntityLink]:
        """从文本提取实体 (< 1ms)."""
        return MemoryStore._extract_keywords_entities(text)

    @staticmethod
    def _extract_keywords_entities(text: str) -> list[EntityLink]:
        keywords = MemoryStore._extract_keywords(text, topk=5)
        return [EntityLink(alias=kw, type="topic", text=kw) for kw in keywords]

    # ---- query: relations ----

    def get_relation(self, from_alias: str, to_alias: str, since_days: int = 365) -> Optional[RelationResult]:
        cutoff = time.time() - since_days * 86400
        with self._lock, self._get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*), MAX(ts) FROM edges "
                "WHERE from_alias=? AND to_alias=? AND ts >= ?",
                (from_alias, to_alias, cutoff),
            ).fetchone()
            if not row or row[0] == 0:
                return None
            topics = conn.execute(
                "SELECT DISTINCT e.text FROM edges ed "
                "JOIN entities e ON ed.to_alias = e.alias "
                "WHERE ed.from_alias=? AND ed.type='talks_about' AND ed.ts >= ? "
                "ORDER BY ed.ts DESC LIMIT 5",
                (from_alias, cutoff),
            ).fetchall()
            count, last_ts = row
            closeness = "new"
            if count >= 30:
                closeness = "close"
            elif count >= 10:
                closeness = "known"
            return RelationResult(
                interaction_count=count,
                last_interaction_ts=last_ts or 0.0,
                common_topics=[t[0] for t in topics],
                closeness=closeness,
            )

    def get_topics(self, alias: str, since_days: int = 7) -> list[str]:
        cutoff = time.time() - since_days * 86400
        with self._lock, self._get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT e.text FROM edges ed "
                "JOIN entities e ON ed.to_alias = e.alias AND e.type='topic' "
                "WHERE ed.from_alias=? AND ed.type='talks_about' AND ed.ts >= ? "
                "ORDER BY ed.ts DESC LIMIT 10",
                (alias, cutoff),
            ).fetchall()
        return [r[0] for r in rows]

    def get_hot_topics(self, group_id: str, since_days: int = 7) -> list[str]:
        cutoff = time.time() - since_days * 86400
        with self._lock, self._get_conn() as conn:
            rows = conn.execute(
                "SELECT e.text, COUNT(*) as cnt FROM edges ed "
                "JOIN entities e ON ed.to_alias = e.alias AND e.type='topic' "
                "WHERE ed.type='talks_about' AND ed.ts >= ? "
                "GROUP BY e.text ORDER BY cnt DESC LIMIT 10",
                (cutoff,),
            ).fetchall()
        return [r[0] for r in rows]

    def search_edges(
        self, entity: str, edge_type: str = "", limit: int = 100, since_days: int = 7
    ) -> list[RelationEdge]:
        cutoff = time.time() - since_days * 86400
        with self._lock, self._get_conn() as conn:
            where = ["ts >= ?"]
            params = [cutoff]
            if entity:
                where.append("(from_alias=? OR to_alias=?)")
                params.extend([entity, entity])
            if edge_type:
                where.append("type=?")
                params.append(edge_type)
            clause = " AND ".join(where)
            rows = conn.execute(
                f"SELECT from_alias, to_alias, type, ts, properties_json FROM edges "
                f"WHERE {clause} ORDER BY ts DESC LIMIT ?",
                params + [limit],
            ).fetchall()
        return [
            RelationEdge(
                from_alias=r[0], to_alias=r[1], type=r[2], ts=r[3],
                properties=json.loads(r[4]) if r[4] else {},
            )
            for r in rows
        ]

    # ---- query: BM25 ----

    def search_bm25(self, text: str, limit: int = 20) -> list[dict]:
        """FTS5 full-text search, returned as {alias, type, text, rank}."""
        with self._lock, self._get_conn() as conn:
            try:
                rows = conn.execute(
                    "SELECT e.alias, e.type, e.text, rank FROM entity_fts f "
                    "JOIN entities e ON f.rowid = e.id "
                    "WHERE entity_fts MATCH ? ORDER BY rank LIMIT ?",
                    (text, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        return [
            {"alias": r[0], "type": r[1], "text": r[2], "rank": r[3]}
            for r in rows
        ]

    def stats(self) -> dict:
        with self._lock, self._get_conn() as conn:
            entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            try:
                topics = conn.execute(
                    "SELECT COUNT(DISTINCT alias) FROM entities WHERE type='topic'"
                ).fetchone()[0]
            except sqlite3.Error:
                topics = -1
        return {
            "entities": entities,
            "edges": edges,
            "distinct_topics": topics,
            # B-014 观测：两道质量门各自拦下多少（判断门槛松紧的数据依据）
            "topic_min_occurrences": self._topic_min_occurrences,
            "topics_deferred": self._topics_deferred,
            "image_desc_skipped": self._image_desc_skipped,
            "stopword_blocked": self._stopword_blocked,
        }
