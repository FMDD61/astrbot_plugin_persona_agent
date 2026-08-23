# -*- coding: utf-8 -*-
import json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.examples import load_examples_block, ExamplesState

EX = [
    {"topic": "A", "messages": [{"role": "群友", "content": "早上好"}, {"role": "夕化炭", "content": "枣商蚝~"}]},
    {"topic": "B", "messages": [{"role": "群友", "content": "x"}, {"role": "夕化炭", "content": "y"}]},
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
            self.assertIn("枣商蚝~", block)
            self.assertIn("[A]", block)
            self.assertIn("规则A", block)
            self.assertIn("规则B", block)
            # same mtime -> cached (no reload observable via same block)
            block2, st2 = load_examples_block(p, prev=st)
            self.assertEqual(block, block2)
            # modified -> new block (delete-then-write + sleep keeps mtime
            # deterministic on coarse-granularity filesystems)
            import time as _t
            os.unlink(p)
            _t.sleep(1.1)
            self._write(td, EX[:1])
            block3, st3 = load_examples_block(p, prev=st2)
            self.assertNotIn("[B]", block3)

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            block, st = load_examples_block(os.path.join(td, "nope.json"))
            self.assertEqual(block, "")
            # removal detected after previous load
            p = self._write(td, EX)
            b1, s1 = load_examples_block(p)
            os.unlink(p)
            b2, s2 = load_examples_block(p, prev=s1)
            self.assertEqual(b2, "")

    def test_corrupt(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "example_dialogs.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write("{broken")
            block, _ = load_examples_block(p)
            self.assertEqual(block, "")

    def test_cap_and_short_msgs(self):
        with tempfile.TemporaryDirectory() as td:
            many = EX * 10  # 20 entries
            p = self._write(td, many)
            block, _ = load_examples_block(p, max_entries=12)
            self.assertLessEqual(block.count("[A]") + block.count("[B]"), 12)
            bad = [{"topic": "C", "messages": [{"role": "群友", "content": "only-one"}]}]
            p = self._write(td, bad)
            block, _ = load_examples_block(p)
            self.assertEqual(block, "")

if __name__ == "__main__":
    unittest.main()
