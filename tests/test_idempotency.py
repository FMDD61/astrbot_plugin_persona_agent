# -*- coding: utf-8 -*-
"""幂等去重：防止同一 cron 在多实例交叠时重复产出并重复推送。

## 实测事故（2026-09-14 用户反馈）

用户在 09-13 夜间反复重启 AstrBot（02:01–02:17 注册 6 次），**新旧实例交叠**
期间同一个 02:05 日旋转 cron 被两个进程各执行一次：
- 用户私聊**收到两份 W37 周报**（`weekly_summary.jsonl` 里 2 条同 period）
- `logs/<gid>/daily_diary.jsonl` 出现 2–3 条同一天日记

教训：**内存去重跨不了进程** —— 两个进程各自"第一次"执行。判据必须落在文件上。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.json_store import JsonStore, find_jsonl_record  # noqa: E402


class TestFindJsonlRecord(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.store = JsonStore(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_missing_file_returns_none(self):
        self.assertIsNone(find_jsonl_record(Path(self.td.name) / "nope.jsonl", {"a": 1}))

    def test_finds_exact_match(self):
        p = self.store.path("x.jsonl")
        self.store.append_jsonl("x.jsonl", {"day": "2026-09-13", "summary": "第一份"})
        self.store.append_jsonl("x.jsonl", {"day": "2026-09-12", "summary": "别的"})
        rec = find_jsonl_record(p, {"day": "2026-09-13"})
        self.assertIsNotNone(rec)
        self.assertEqual(rec["summary"], "第一份")

    def test_multi_key_match_requires_all(self):
        p = self.store.path("w.jsonl")
        self.store.append_jsonl("w.jsonl", {"kind": "weekly", "group_id": "g1",
                                            "period": "2026-W37", "summary": "A"})
        self.assertIsNotNone(find_jsonl_record(
            p, {"kind": "weekly", "group_id": "g1", "period": "2026-W37"}))
        # 任一键不匹配 → 不算重复（不同群的同周期摘要应各自保留）
        self.assertIsNone(find_jsonl_record(
            p, {"kind": "weekly", "group_id": "g2", "period": "2026-W37"}))
        self.assertIsNone(find_jsonl_record(
            p, {"kind": "monthly", "group_id": "g1", "period": "2026-W37"}))

    def test_tolerates_corrupt_lines(self):
        p = self.store.path("bad.jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{not json\n{"day": "d1", "summary": "ok"}\n\n', encoding="utf-8")
        self.assertIsNotNone(find_jsonl_record(p, {"day": "d1"}))

    def test_json_store_has_public_path(self):
        """回归：main 用 `self.store.path(...)` 取路径做幂等检查。

        没有这个公开方法就是 AttributeError —— 而 main.py 不被任何测试导入，
        只能在运行时炸（与 2026-09-14 的 `NameError: os` 同一类事故）。
        """
        self.assertTrue(hasattr(JsonStore, "path"))
        p = self.store.path("sub/dir/f.jsonl")
        self.assertIsInstance(p, Path)
        self.assertTrue(str(p).endswith(os.path.join("sub", "dir", "f.jsonl")))


class TestIdempotencySemantics(unittest.TestCase):
    """模拟"两个实例各跑一次"：第二次必须被拦。"""

    def test_second_write_is_detected(self):
        with tempfile.TemporaryDirectory() as td:
            store = JsonStore(td)
            rec = {"day": "2026-09-13", "group_id": "g1", "summary": "X", "n_messages": 7}
            path = store.path("logs/g1/daily_diary.jsonl")
            # 实例①
            self.assertIsNone(find_jsonl_record(path, {"day": rec["day"],
                                                       "group_id": rec["group_id"]}))
            store.append_jsonl("logs/g1/daily_diary.jsonl", rec)
            # 实例②（不同进程，各自内存都是"第一次"）
            dup = find_jsonl_record(path, {"day": rec["day"], "group_id": rec["group_id"]})
            self.assertIsNotNone(dup)
            self.assertEqual(dup["summary"], "X")

    def test_weekly_summary_dup_detected(self):
        with tempfile.TemporaryDirectory() as td:
            store = JsonStore(td)
            rec = {"kind": "weekly", "group_id": "g1", "period": "2026-W37",
                   "summary": "周记", "n_diaries": 7}
            p = store.path("weekly_summary.jsonl")
            store.append_jsonl("weekly_summary.jsonl", rec)
            dup = find_jsonl_record(p, {"kind": "weekly", "group_id": "g1",
                                        "period": "2026-W37"})
            self.assertIsNotNone(dup)
            # 并且**不重复推送**的前提就是这里能查到（推送在写入之后）
            self.assertEqual(dup["period"], "2026-W37")


if __name__ == "__main__":
    unittest.main()
