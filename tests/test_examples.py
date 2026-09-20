# -*- coding: utf-8 -*-
"""示例块加载（C1/C2 重做后的口径）。

用户 2026-09-20：**只保留新 20 条**；部署用文件替换；**任何情况下不回退旧句**。
所以"文件缺失/损坏"不再是"空块"，而是**回落内置的 20 条**。
"""
import json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services import examples_default
from services.examples import ExamplesState, MAX_ENTRIES, load_examples_block

EX = [
    {"topic": "A", "messages": [{"role": "群友", "content": "早上好"}, {"role": "成员丁", "content": "早上好~"}]},
    {"topic": "B", "messages": [{"role": "群友", "content": "x"}, {"role": "成员丁", "content": "y"}]},
]

class TestExamplesLoader(unittest.TestCase):
    def _write(self, td, data, ns_shift=1_000_000_000):
        p = os.path.join(td, "example_dialogs.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        st = os.stat(p)
        os.utime(p, ns=(st.st_atime_ns + ns_shift, st.st_mtime_ns + ns_shift))
        return p

    def test_load_and_cache(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, EX)
            block, st = load_examples_block(os.path.join(td, "example_dialogs.json"))
            self.assertIn("早上好~", block)
            self.assertIn("[A]", block)
            self.assertEqual(st.source, "file")
            self.assertEqual(st.entries, 2)
            # 旧的两条"规则A/规则B"已删（规则B 已证伪、规则A 是错误示例）
            self.assertNotIn("规则A", block)
            self.assertNotIn("规则B", block)
            # same mtime -> cached
            block2, st2 = load_examples_block(p, prev=st)
            self.assertEqual(block, block2)
            import time as _t
            os.unlink(p)
            _t.sleep(1.1)
            self._write(td, EX[:1])
            block3, st3 = load_examples_block(p, prev=st2)
            self.assertNotIn("[B]", block3)

    def test_missing_file_falls_back_to_bundled_20(self):
        """🔴 C1：文件缺失**不再等于空块** —— 回落到内置的新 20 条。"""
        with tempfile.TemporaryDirectory() as td:
            block, st = load_examples_block(os.path.join(td, "nope.json"))
            self.assertNotEqual(block, "")
            self.assertEqual(st.source, "bundled")
            self.assertEqual(st.entries, len(examples_default.ENTRIES))
            self.assertIn("早上好", block)
            self.assertTrue(block.startswith(examples_default.HEADER))
            # 删文件后同样回落（不是"变空"）
            p = self._write(td, EX)
            b1, s1 = load_examples_block(p)
            self.assertEqual(s1.source, "file")
            os.unlink(p)
            b2, s2 = load_examples_block(p, prev=s1)
            self.assertEqual(s2.source, "bundled")
            self.assertEqual(len(b2), len(block))

    def test_corrupt_falls_back_to_bundled(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "example_dialogs.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write("{broken")
            block, st = load_examples_block(p)
            self.assertEqual(st.source, "bundled")
            self.assertNotEqual(block, "")

    def test_no_old_example_sentences_anywhere(self):
        """🔴 用户要求：**不论如何，不会回退到旧的示例句**。"""
        joined = json.dumps(examples_default.ENTRIES, ensure_ascii=False)
        for old in ("成员甲", "成员甲", "规则A", "规则B", "口癖丙"):
            self.assertNotIn(old, joined, f"旧示例残留：{old}")
        self.assertEqual(len(examples_default.ENTRIES), 20)

    def test_cap_and_short_msgs(self):
        with tempfile.TemporaryDirectory() as td:
            many = EX * 10  # 20 entries
            p = self._write(td, many)
            block, _ = load_examples_block(p, max_entries=12)
            self.assertLessEqual(block.count("[A]") + block.count("[B]"), 12)
            # 只有单条消息的条目不算示例 → 该文件没有可用条目 → 回落内置 20
            bad = [{"topic": "C", "messages": [{"role": "群友", "content": "only-one"}]}]
            p = self._write(td, bad)
            block, st = load_examples_block(p)
            self.assertEqual(st.source, "bundled")
            self.assertEqual(st.entries, len(examples_default.ENTRIES))

    def test_max_entries_default_is_20(self):
        """C1：默认条数上限不再是 12（用户要求 20，且要可调）。"""
        self.assertEqual(MAX_ENTRIES, 20)
        block, st = load_examples_block("/nonexistent/path.json")
        self.assertEqual(st.entries, min(20, len(examples_default.ENTRIES)))

if __name__ == "__main__":
    unittest.main()
