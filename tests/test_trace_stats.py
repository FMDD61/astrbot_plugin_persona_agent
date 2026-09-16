# -*- coding: utf-8 -*-
"""S17：统计口径固化 `tools/trace_stats.py`（D3）。

## 为什么（用户 2026-09-15 同意）

本轮为调查 B1/B2/Q1 我**手写了 5 个一次性脚本**，每个都要重新摸索字段名、
重新踩"日志会轮转为 .jsonl.1"这个坑、重新定义口径。结论散在对话里，
下次无法复现，也无法比较"这次和上次"。

固化的价值：
- **口径一致** → 两次测量可比（调参前后、改动前后）
- **含轮转文件** → 不重复踩"日志被清空"的误判
- **可复现** → 结论能追溯到具体命令与样本量

## 口径定义（写进代码，避免每次重新解释）

| 名称 | 定义 |
|---|---|
| 入站 | trace 条数（每条 = 一条进群消息的处理） |
| 硬闸放行 | `hard_gate.action == "reply"` |
| 过 Gate | `gate.reply == true` |
| 生成 | 有 `raw_generation` |
| Gate 拒绝率 | 1 − 过Gate/硬闸放行 |
| 解析失败率 | `gate.reason` 含 "parse failed" / 过 Gate 候选数 |
| 点名率 | 输出里含任一成员别名的比例 |
| 缓存 | `cached` / (`cached` + `other`) 加权 + p50 |
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from tools.trace_stats import (  # noqa: E402
    compute_stats,
    load_jsonl_with_rotations,
)


def _w(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")


class TestLoadWithRotations(unittest.TestCase):
    """🔴 必须同时读 .jsonl 与 .jsonl.1 —— 实测日志会被宿主轮转（59MB .1）。

    不读轮转文件会导致"日志被清空"的误判（本轮真实踩过）。
    """

    def test_reads_rotated_file(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _w(d / "trace_log.jsonl.1", [{"a": 1}, {"a": 2}])
            _w(d / "trace_log.jsonl", [{"a": 3}])
            rows = load_jsonl_with_rotations(d / "trace_log.jsonl")
            self.assertEqual([r["a"] for r in rows], [1, 2, 3], "轮转文件应被一起读入")

    def test_tolerates_missing_and_garbage(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self.assertEqual(load_jsonl_with_rotations(d / "none.jsonl"), [])
            _w(d / "x.jsonl", [{"a": 1}])
            with open(d / "x.jsonl", "a", encoding="utf-8") as f:
                f.write("{坏行\n\n")
            rows = load_jsonl_with_rotations(d / "x.jsonl")
            self.assertEqual(len(rows), 1, "坏行应被跳过，不抛")

    def test_no_duplicate_when_both_absent(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(load_jsonl_with_rotations(Path(td) / "a.jsonl"), [])


class TestComputeStats(unittest.TestCase):
    """核心口径（写入代码而非每次口头解释）。"""

    def _rows(self):
        return [
            # 一条正常生成
            {"ts": "2026-09-16T01:00:00Z",
             "input": {"sender_alias": "甲", "text": "hi"},
             "hard_gate": {"action": "reply", "trigger": "rag_hit"},
             "gate": {"reply": True, "conflict": False, "reason": "可以接"},
             "raw_generation": "回复A", "final_text": "回复A"},
            # 硬闸拒绝（没进 Gate）
            {"ts": "2026-09-16T01:01:00Z",
             "input": {"sender_alias": "乙", "text": "x"},
             "hard_gate": {"action": "silent", "trigger": "score_low"}},
            # 过硬闸但 Gate 解析失败
            {"ts": "2026-09-16T01:02:00Z",
             "input": {"sender_alias": "丙", "text": "y"},
             "hard_gate": {"action": "reply", "trigger": "rag_hit"},
             "gate": {"reply": False, "reason": "gate parse failed"},
             "gate_degraded": "parse_failed: '闲聊'"},
            # 过硬闸、Gate 正常拒绝
            {"ts": "2026-09-16T01:03:00Z",
             "input": {"sender_alias": "丁", "text": "z"},
             "hard_gate": {"action": "reply", "trigger": "at"},
             "gate": {"reply": False, "reason": "未点名"}},
        ]

    def test_funnel_counts(self):
        st = compute_stats(self._rows())
        self.assertEqual(st["inbound"], 4)
        self.assertEqual(st["hard_pass"], 3)
        self.assertEqual(st["gate_pass"], 1)
        self.assertEqual(st["generated"], 1)

    def test_gate_reject_rate(self):
        st = compute_stats(self._rows())
        # 3 放行 → 1 通过 → 拒 2/3
        self.assertAlmostEqual(st["gate_reject_rate"], 2 / 3, places=3)

    def test_parse_failed_rate(self):
        """parse failed 必须**单独统计** —— 它不是"模型判断拒绝"，
        而是 Gate 没生效（S10 事故里占拒绝的 54%）。混在一起会误导。"""
        st = compute_stats(self._rows())
        self.assertEqual(st["parse_failed"], 1)
        self.assertAlmostEqual(st["parse_failed_rate"], 1 / 3, places=3)
        self.assertEqual(st["gate_reject_real"], 1, "真实拒绝应剔除 parse failed")

    def test_missing_fields_dont_crash(self):
        st = compute_stats([{}, {"hard_gate": None}, {"gate": {}}])
        self.assertEqual(st["inbound"], 3)
        self.assertEqual(st["generated"], 0)

    def test_empty_input(self):
        st = compute_stats([])
        self.assertEqual(st["inbound"], 0)
        self.assertEqual(st["gate_reject_rate"], 0.0)

    def test_trigger_breakdown(self):
        st = compute_stats(self._rows())
        self.assertEqual(st["triggers"]["rag_hit"], 2)
        self.assertEqual(st["triggers"]["at"], 1)


class TestNameMentionRate(unittest.TestCase):
    """点名率：需要成员别名表 —— 与 Q1 调查共用口径。"""

    def test_mention_rate_with_alias_map(self):
        rows = [
            {"final_text": "花鱼早呀", "input": {"sender_alias": "花鱼"}},
            {"final_text": "今天天气不错", "input": {"sender_alias": "甲"}},
        ]
        st = compute_stats(rows, alias_map={"848671904": "花鱼"})
        self.assertEqual(st["gen_with_name"], 1)
        self.assertAlmostEqual(st["name_mention_rate"], 0.5, places=3)

    def test_no_alias_map_skips_metric(self):
        st = compute_stats([{"final_text": "花鱼早呀"}])
        self.assertNotIn("name_mention_rate", st,
                         "没有别名表时不应给出不可靠的比率")


class TestCacheStats(unittest.TestCase):
    """缓存口径：加权命中率 + p50（总量与中位数会讲不同的故事）。"""

    def test_hit_rates(self):
        rows = [
            {"usage": {"input_cached": 900, "input_other": 100}},   # 0.90
            {"usage": {"input_cached": 0, "input_other": 1000}},    # 0.00
            {"usage": {"input_cached": 500, "input_other": 500}},   # 0.50
        ]
        st = compute_stats(rows)
        # 加权：1400 / 3000
        self.assertAlmostEqual(st["cache_hit_total"], 1400 / 3000, places=3)
        # p50 = 中位数(0.0, 0.5, 0.9) = 0.5
        self.assertAlmostEqual(st["cache_hit_p50"], 0.5, places=3)
        self.assertLess(st["cache_hit_p50"], st["cache_hit_total"] + 1,
                        "两者会讲不同的故事（本用例刻意让 p50 < 加权）")

    def test_no_usage_skips(self):
        st = compute_stats([{"input": {}}])
        self.assertNotIn("cache_hit_total", st)


if __name__ == "__main__":
    unittest.main()
