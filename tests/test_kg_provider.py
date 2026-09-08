# -*- coding: utf-8 -*-
"""MultiSignalKGProvider A7③ tests: dense_enabled 退化分支 / external hits 复用。"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.kg_provider import KGContext, MultiSignalKGProvider


class _FakeRag:
    """记录 query 调用次数（断言是否自查 dense）。"""
    def __init__(self):
        self.calls = 0

    def query(self, context_text, k=8, now_utc=None, top_n_final=3):
        self.calls += 1
        return [{
            "id": f"id{i}", "document": f"历史回复{i}", "metadata": {"reply_text": f"你回{i}"},
            "dense": 0.9 - i * 0.1, "recency": 0.5, "hour_match": 0.5, "score": 0.8 - i * 0.1,
        } for i in range(min(top_n_final, 3))]


class _FakeStore:
    def search_bm25(self, text, limit=20):
        return [{"alias": "小明", "type": "topic", "text": "麻薯", "rank": 1}]

    def get_entities(self, text):
        return []

    def get_relation(self, from_alias, to_alias, since_days=365):
        from services.memory_store import RelationResult
        return RelationResult(
            interaction_count=12, closeness="熟", common_topics=["麻薯", "游戏"],
        )


def _ctx(**over):
    kw = dict(recent_messages=[{"role": "user", "name": "小明", "content": "今天麻薯好吃"}],
              current_speaker="小红", current_text="今天麻薯好吃", group_id="g1")
    kw.update(over)
    return KGContext(**kw)


def _mk(dense_enabled=True):
    rag = _FakeRag()
    kg = MultiSignalKGProvider(rag, _FakeStore(), dense_enabled=dense_enabled)
    return rag, kg


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestDenseDisabled(unittest.TestCase):
    def test_dense_off_returns_relation_only(self):
        """dense_enabled=0 → 不调 rag；内容仅关系块（互动/关系等级/共同话题）。"""
        rag, kg = _mk(dense_enabled=False)
        res = _run(kg.query(_ctx()))
        self.assertEqual(rag.calls, 0)          # 完全不查 rag_service
        self.assertIsNotNone(res)
        self.assertIn("provider", res.metadata)
        self.assertEqual(res.metadata.get("provider"), "relation_only")
        self.assertIn("互动12次", res.content)
        self.assertIn("关系等级", res.content)
        # 无"历史回复"话术示例（退化语义：无 BGE 话术）
        self.assertNotIn("你回", res.content)

    def test_dense_off_no_relation_returns_none(self):
        """dense 关且无关系块 → 返回 None（不注入任何内容）。"""
        class NoRelStore(_FakeStore):
            def get_relation(self, from_alias, to_alias, since_days=365):
                return None
        rag = _FakeRag()
        kg = MultiSignalKGProvider(rag, NoRelStore(), dense_enabled=False)
        res = _run(kg.query(_ctx()))
        self.assertIsNone(res)
        self.assertEqual(rag.calls, 0)


class TestExternalHitsReuse(unittest.TestCase):
    def test_external_hits_no_self_query(self):
        """external_dense_hits 传入 → KG 不自查 rag（省一次 BGE）。"""
        rag, kg = _mk(dense_enabled=True)
        external = [{
            "id": "e1", "document": "外部文档", "metadata": {"reply_text": "外部回复"},
            "dense": 0.9, "recency": 0.5, "hour_match": 0.5, "score": 0.85,
        }]
        res = _run(kg.query(_ctx(), external_dense_hits=external))
        self.assertEqual(rag.calls, 0)          # 用外部 hits，不自查
        self.assertIsNotNone(res)
        self.assertIn("外部回复", res.content)

    def test_no_external_fallback_self_query(self):
        """不传 external（离线/独立调用）→ fallback 自查（向后兼容）。"""
        rag, kg = _mk(dense_enabled=True)
        res = _run(kg.query(_ctx()))
        self.assertEqual(rag.calls, 1)          # 自查一次
        self.assertIsNotNone(res)
        self.assertIn("你回", res.content)       # 含话术示例


if __name__ == "__main__":
    unittest.main()
