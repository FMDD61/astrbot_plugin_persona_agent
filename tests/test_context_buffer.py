# -*- coding: utf-8 -*-
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.context_buffer import ContextBuffer, QuoteEntry, QuoteIndex


class TestQuoteTarget(unittest.TestCase):
    def test_n_mapping(self):
        with tempfile.TemporaryDirectory() as td:
            cb = ContextBuffer(td, persist_jsonl=False)
            cb.add(ts=1, group_id='g', sender_id='a', sender_name='a', text='第一条', message_id='m1')
            cb.add(ts=2, group_id='g', sender_id='b', sender_name='b', text='第二条', message_id='m2')
            self.assertEqual(cb.quote_target(1), 'm2')
            self.assertEqual(cb.quote_target(2), 'm1')
            self.assertIsNone(cb.quote_target(3))
            self.assertIsNone(cb.quote_target(0))

    def test_bot_message_no_id(self):
        with tempfile.TemporaryDirectory() as td:
            cb = ContextBuffer(td, persist_jsonl=False)
            cb.add(ts=1, group_id='g', sender_id='a', sender_name='a', text='x', message_id='m1')
            cb.add(ts=2, group_id='g', sender_id='bot', sender_name='<bot>', text='y', message_id='', message_type='bot')
            self.assertIsNone(cb.quote_target(1))  # bot msg has no id
            self.assertEqual(cb.quote_target(2), 'm1')


class TestQuoteIndex(unittest.TestCase):
    """B-001：引用编号基必须**冻结在生成前**，不受生成期间新消息影响。"""

    def test_resolve_maps_from_the_end(self):
        qi = QuoteIndex.from_entries([("m1", "u1", "甲"), ("m2", "u2", "乙"), ("m3", "u3", "丙")])
        self.assertEqual(qi.resolve(1).message_id, "m3")
        self.assertEqual(qi.resolve(3).message_id, "m1")
        self.assertEqual(qi.resolve(2).alias, "乙")
        self.assertIsNone(qi.resolve(4))
        self.assertIsNone(qi.resolve(0))
        self.assertEqual(len(qi), 3)

    def test_frozen_basis_ignores_later_messages(self):
        """核心不变式：快照建成后再来消息，编号解析结果**不变**。"""
        qi = QuoteIndex.from_entries([("m1", "u1", "甲"), ("m2", "u2", "乙")])
        before = qi.resolve(1).message_id
        # 模拟生成窗口内又进了两条（实时引用基会因此偏移 2 位）
        live = QuoteIndex.from_entries(
            [("m1", "u1", "甲"), ("m2", "u2", "乙"), ("m3", "u3", "丙"), ("m4", "u4", "丁")]
        )
        self.assertEqual(before, "m2")
        self.assertEqual(live.resolve(1).message_id, "m4")  # 旧实现会取到这个
        self.assertEqual(qi.resolve(1).message_id, "m2")    # 冻结基不受影响

    def test_entries_without_id_are_unresolvable(self):
        # 机器人自己的回复（无 message_id）在编号基里占位但不可被引用
        qi = QuoteIndex.from_entries([("m1", "u1", "甲"), ("", "", ""), ("m3", "u3", "丙")])
        self.assertEqual(qi.resolve(1).message_id, "m3")
        self.assertIsNone(qi.resolve(2))                    # ← bot 回复：占位但不可引用
        self.assertEqual(qi.resolve(3).message_id, "m1")    # 占位仍在，编号不塌陷
        self.assertEqual(len(qi), 3)

    def test_from_entries_tolerates_junk(self):
        # 畸形条目（长度不足）直接跳过，不污染编号基
        qi = QuoteIndex.from_entries([("m1",), None, ("m2", "u2", "乙"), ("m3", "u3", "丙")])
        self.assertEqual(len(qi), 2)
        self.assertEqual(qi.resolve(1).message_id, "m3")
        self.assertEqual(qi.resolve(2).message_id, "m2")

    def test_accepts_quoteentry_objects(self):
        qi = QuoteIndex.from_entries([QuoteEntry("m1", "u1", "甲")])
        self.assertEqual(len(qi), 1)
        self.assertEqual(qi.resolve(1).message_id, "m1")


if __name__ == "__main__":
    unittest.main()
