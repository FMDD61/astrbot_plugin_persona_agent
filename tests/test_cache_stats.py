# -*- coding: utf-8 -*-
"""缓存命中统计聚合（用户要求：设计成有长期留存的数据文件记录）。

背景：`llm_cache_probe.jsonl` 是逐次调用明细，**无轮转**（同目录 trace_log 已 43 MB）。
本工具把它压成两级长期产物：`cache_stats.jsonl`（每次一行，紧凑）与
`cache_daily.jsonl`（每天一行，趋势）。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from tools import cache_stats as C  # noqa: E402


def probe_row(ts, cached, other, sys_hash="aaa", sess=3000, ctx=100000):
    return {"ts": ts, "group_id": "g1", "session_size_before": sess,
            "contexts_len": sess + 3, "contexts_chars": ctx, "kg_tail_chars": 140,
            "sys_prompt_len": 6430, "sys_prompt_hash16": sys_hash, "local_hour": 12,
            "provider_id": "x",
            "usage": {"input_other": other, "input_cached": cached, "output": 80},
            "raw_usage": {}}


class TestCompact(unittest.TestCase):
    def test_hit_rate_based_on_prompt_total(self):
        r = C.compact(probe_row(1000.0, 80000, 400))
        self.assertEqual(r["cached"], 80000)
        self.assertEqual(r["other"], 400)
        self.assertEqual(r["prompt"], 80400)
        self.assertAlmostEqual(r["hit"], 80000 / 80400, places=3)

    def test_cold_start_flagged(self):
        r = C.compact(probe_row(1000.0, 0, 32000))
        self.assertEqual(r["cold"], 1)
        self.assertEqual(r["hit"], 0.0)

    def test_zero_prompt_is_not_division_error(self):
        r = C.compact(probe_row(1000.0, 0, 0))
        self.assertEqual(r["hit"], 0.0)
        self.assertEqual(r["cold"], 1)

    def test_day_is_utc(self):
        ts = 1789389384.0
        r = C.compact(probe_row(ts, 100, 10))
        self.assertEqual(r["day"], time.strftime("%Y-%m-%d", time.gmtime(ts)))


class TestRollup(unittest.TestCase):
    def test_totals_and_percentiles(self):
        rows = [C.compact(probe_row(1000.0, 90000, 100)),
                C.compact(probe_row(2000.0, 0, 32000)),      # 冷启动
                C.compact(probe_row(3000.0, 80000, 300))]
        d = C.rollup_day("2026-09-14", rows)
        self.assertEqual(d["calls"], 3)
        self.assertEqual(d["cold_start"], 1)
        self.assertEqual(d["other_total"], 32400)
        self.assertEqual(d["cached_total"], 170000)
        # ⚠️ 两个口径的大小关系**不固定**，取决于各次 prompt 的大小分布：
        #   总量口径 = Σcached/Σprompt（按 token 加权 → 反映**成本**）
        #   均值口径 = mean(cached/prompt)（按调用加权 → 反映**单次体验**）
        # 本例冷启动那条 prompt 小（32k）、两条热的 prompt 大（90k），
        # 于是加权后冷启动被稀释，`hit_rate_total > hit_rate_avg`。
        # 反过来（冷启动都是大 prompt）则 total < avg。**不要假设方向**。
        self.assertAlmostEqual(d["hit_rate_total"], 170000 / (170000 + 32400), places=3)
        self.assertAlmostEqual(d["hit_rate_avg"],
                               (90000 / 90100 + 0.0 + 80000 / 80300) / 3, places=3)
        # p10 应当反映"最差的那次"，冷启动在列
        self.assertEqual(d["hit_rate_p10"], 0.0)

    def test_counts_distinct_sys_hashes(self):
        """提示词哈希 >1 = 当天改过 system prompt（一改整段前缀缓存全废）。"""
        rows = [C.compact(probe_row(1000.0, 90000, 100, sys_hash="aaa")),
                C.compact(probe_row(2000.0, 90000, 100, sys_hash="bbb"))]
        d = C.rollup_day("2026-09-14", rows)
        self.assertEqual(d["distinct_sys_hashes"], 2)

    def test_empty_day(self):
        self.assertEqual(C.rollup_day("2026-09-14", []), {})


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.gdir = Path(self.td.name) / "logs" / "g1"
        self.gdir.mkdir(parents=True)
        self.probe = self.gdir / "llm_cache_probe.jsonl"
        # 两天数据；第二天换了 system prompt
        rows = []
        for i in range(6):
            rows.append(probe_row(1000.0 + i, 50000, 200, sys_hash="aaa"))
        for i in range(4):
            rows.append(probe_row(100000.0 + i, 60000, 300, sys_hash="bbb"))
        self.probe.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                              encoding="utf-8")

    def tearDown(self):
        self.td.cleanup()

    def _run(self, *args):
        return C.main(["--data-dir", self.td.name, "--group", "g1", *args])

    def test_rebuild_creates_both_artifacts(self):
        self.assertEqual(self._run("--rebuild"), 0)
        stats = C.read_jsonl(self.gdir / C.STATS_FILE)
        daily = C.read_jsonl(self.gdir / C.DAILY_FILE)
        self.assertEqual(len(stats), 10)
        self.assertEqual(len(daily), 2)
        # 每次一行 + 每天一行 = 长期留存且可被 LLM 读
        for d in daily:
            for k in ("day", "calls", "hit_rate_total", "other_total",
                      "distinct_sys_hashes"):
                self.assertIn(k, d)

    def test_incremental_only_processes_new(self):
        self._run("--rebuild")
        # 无新数据
        self.assertEqual(self._run(), 0)
        self.assertEqual(len(C.read_jsonl(self.gdir / C.STATS_FILE)), 10)
        # 追加一行 → 只加一行
        with open(self.probe, "a", encoding="utf-8") as f:
            f.write(json.dumps(probe_row(200000.0, 70000, 100)) + "\n")
        self._run()
        stats = C.read_jsonl(self.gdir / C.STATS_FILE)
        self.assertEqual(len(stats), 11)
        # 受影响那天被重算（不会出现重复的日期行）
        daily = C.read_jsonl(self.gdir / C.DAILY_FILE)
        days = [d["day"] for d in daily]
        self.assertEqual(len(days), len(set(days)), "日汇总不得出现重复日期")

    def test_tolerates_corrupt_lines(self):
        with open(self.probe, "a", encoding="utf-8") as f:
            f.write("{not json\n\n")
        self.assertEqual(self._run("--rebuild"), 0)
        self.assertEqual(len(C.read_jsonl(self.gdir / C.STATS_FILE)), 10)

    def test_missing_probe_returns_error(self):
        (self.gdir / "llm_cache_probe.jsonl").unlink()
        self.assertEqual(self._run("--rebuild"), 1)


if __name__ == "__main__":
    unittest.main()
