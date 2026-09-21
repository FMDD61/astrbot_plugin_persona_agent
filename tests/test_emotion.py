# -*- coding: utf-8 -*-
"""services/emotion.py v2（C24：代码计算 / C25：废弃文生图通道）单元测试。

设计依据：docs/specs/emotion_v2.md（仓库外）—— §6 定案 / §7 两条算术 /
§8 mood 映射表 / §9 sticker 废弃；BUGS.md B-053。

覆盖口径：
  · 分数演化（blocked 累积 / 时间恢复 / 上下限钳制 / 参数可配）
  · mood 映射表逐档边界 + 只许情绪词（§8 注 1）
  · 乘子 0.6+0.4×score 与 0.40 悬崖算术（§7.2）
  · register_blocked 幂等（event_key）与并发安全（有锁）
  · "参数改了就有效"（项目规矩：新增可调参数必须证明不是死参数）
  · 降级可见（last_error / stats / 日志）与"功能关闭时静默"
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import services.emotion as em  # noqa: E402
from services.emotion import (  # noqa: E402
    DefaultEmotionProvider,
    EmotionProvider,
    EmotionState,
    ScoreEmotionProvider,
    mood_for_score,
    multiplier_for_score,
)

REPO = os.path.join(os.path.dirname(__file__), "..")


class FakeClock:
    """可注入时钟（A7④ 重放语义：真实时钟在 main，重放用虚拟时钟）。"""

    def __init__(self, t: float = 1_700_000_000.0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, sec: float) -> float:
        self.t += float(sec)
        return self.t


def make(**kw):
    """构造 provider + 时钟；kw 里的 clock 可外部注入。"""
    clock = kw.pop("clock", None) or FakeClock()
    return ScoreEmotionProvider(now_utc_fn=clock, **kw), clock


# ---------------------------------------------------------------------------
# 1. 分数演化
# ---------------------------------------------------------------------------
class TestScoreEvolution(unittest.TestCase):
    def test_initial_score_is_one(self):
        p, _ = make()
        self.assertAlmostEqual(p.score(), 1.0)
        self.assertAlmostEqual(p.multiplier(), 1.0)

    def test_each_blocked_subtracts_0_1(self):
        p, _ = make()
        self.assertAlmostEqual(p.register_blocked("conflict"), 0.9)
        self.assertAlmostEqual(p.register_blocked("topic"), 0.8)
        self.assertAlmostEqual(p.register_blocked("pda"), 0.7)
        self.assertAlmostEqual(p.score(), 0.7)

    def test_recovery_0_1_per_minute(self):
        p, c = make()
        p.register_blocked("conflict")
        c.advance(30)
        self.assertAlmostEqual(p.score(), 0.95, places=6)
        c.advance(30)
        self.assertAlmostEqual(p.score(), 1.0, places=6)

    def test_recovery_clamped_at_max(self):
        p, c = make()
        p.register_blocked("conflict")
        c.advance(10 * 3600)
        self.assertAlmostEqual(p.score(), 1.0)
        self.assertAlmostEqual(p.multiplier(), 1.0)

    def test_clamped_at_min(self):
        p, _ = make()
        for _ in range(20):
            p.register_blocked("conflict")
        self.assertAlmostEqual(p.score(), 0.0)
        self.assertAlmostEqual(p.multiplier(), 0.6)

    def test_steady_state_3_blocked_per_minute_pins_to_min(self):
        """§7.1 算术：3 次/分 = −0.3/分，恢复 +0.1/分 → 净 −0.2/分 → 必然钉在下限。"""
        p, c = make()
        lows = []
        for _ in range(30):
            lows.append(p.register_blocked("conflict"))
            c.advance(20)
        # 稳态：每次扣分都打到下限；两次之间最多回 0.1/分 × 20s = 0.0333 —— 全程够不到悬崖
        self.assertAlmostEqual(lows[-1], 0.0)
        self.assertTrue(all(v == 0.0 for v in lows[-5:]), lows[-5:])
        self.assertLess(p.score(), 0.04)
        self.assertLess(multiplier_for_score(p.score()), 0.62)

    def test_steady_state_1_blocked_per_minute_is_equilibrium(self):
        """1 次/分 = −0.1/分 = 恢复 +0.1/分 → 恰好平衡，分数在 0.9~1.0 往复不衰减。"""
        p, c = make()
        lows = []
        for _ in range(30):
            lows.append(p.register_blocked("conflict"))
            c.advance(60)
        self.assertAlmostEqual(min(lows), 0.9, places=6)
        self.assertAlmostEqual(max(lows), 0.9, places=6)
        self.assertAlmostEqual(p.score(), 1.0, places=6)

    def test_steady_state_half_per_minute_recovers_to_full(self):
        """0.5 次/分（−0.05/分）< 恢复速度 → 每次都被抹平，分数回满（§7.1）。"""
        p, c = make()
        for _ in range(20):
            p.register_blocked("conflict")
            c.advance(120)
        self.assertAlmostEqual(p.score(), 1.0)

    def test_recovery_rate_is_configurable(self):
        p, c = make(blocked_penalty=0.6, recovery_per_min=1.0)
        p.register_blocked("conflict")
        self.assertAlmostEqual(p.score(), 0.4)
        c.advance(30)
        self.assertAlmostEqual(p.score(), 0.9)

    def test_recovery_zero_freezes_score(self):
        p, c = make(recovery_per_min=0.0)
        p.register_blocked("conflict")
        c.advance(3600)
        self.assertAlmostEqual(p.score(), 0.9)

    def test_penalty_is_configurable(self):
        p, _ = make(blocked_penalty=0.3)
        p.register_blocked("conflict")
        p.register_blocked("conflict")
        self.assertAlmostEqual(p.score(), 0.4, places=6)

    def test_min_score_is_configurable(self):
        p, _ = make(min_score=0.5)
        for _ in range(10):
            p.register_blocked("conflict")
        self.assertAlmostEqual(p.score(), 0.5)

    def test_max_score_is_configurable(self):
        p, c = make(initial_score=0.6, blocked_penalty=0.2, max_score=0.6)
        p.register_blocked("conflict")
        self.assertAlmostEqual(p.score(), 0.4, places=6)
        c.advance(600)
        self.assertAlmostEqual(p.score(), 0.6)

    def test_initial_score_is_configurable(self):
        p, _ = make(initial_score=0.4)
        self.assertAlmostEqual(p.score(), 0.4)
        self.assertAlmostEqual(p.multiplier(), 0.76)

    def test_initial_out_of_range_clamped_with_warning(self):
        with self.assertLogs("services.emotion", level="WARNING") as cm:
            p = ScoreEmotionProvider(initial_score=1.5, max_score=0.8)
        self.assertAlmostEqual(p.score(), 0.8)
        self.assertTrue(any("initial_score" in m for m in cm.output))

    def test_invalid_params_raise_visible(self):
        for bad in (
            dict(blocked_penalty=-0.1),
            dict(recovery_per_min=-1.0),
            dict(min_score=0.8, max_score=0.5),
            dict(min_score=-0.1),
            dict(max_score=1.5),
            dict(blocked_penalty=float("nan")),
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ScoreEmotionProvider(**bad)

    def test_clock_backwards_does_not_add_score_and_is_visible(self):
        """时钟回退（NTP 校正 / 重放虚拟时钟跳变）不能变成"负恢复"，且必须留痕。"""
        p, c = make()
        p.register_blocked("conflict")
        c.advance(-120)
        with self.assertLogs("services.emotion", level="WARNING") as cm:
            s = p.score()
        self.assertAlmostEqual(s, 0.9)
        self.assertIn("backwards", p.last_error or "")
        self.assertEqual(p.stats["clock_backwards"], 1)
        self.assertTrue(any("时钟回退" in m for m in cm.output))
        # 只告警一次（_last_ts 已回退对齐），不刷屏
        with self.assertNoLogs("services.emotion", level="WARNING"):
            p.score()


# ---------------------------------------------------------------------------
# 2. mood 映射（§8）
# ---------------------------------------------------------------------------
class TestMoodMapping(unittest.TestCase):
    def test_table_boundaries(self):
        for s, want in (
            (1.00, "轻快"), (0.99, "平常"), (0.80, "平常"),
            (0.799, "有点蔫"), (0.60, "有点蔫"),
            (0.599, "提不起劲"), (0.40, "提不起劲"),
            (0.399, "低落"), (0.20, "低落"),
            (0.199, "沉沉的"), (0.0, "沉沉的"),
        ):
            with self.subTest(score=s):
                self.assertEqual(mood_for_score(s), want)

    def test_out_of_range_is_clamped(self):
        self.assertEqual(mood_for_score(-3.0), "沉沉的")
        self.assertEqual(mood_for_score(9.0), "轻快")

    def test_mood_words_are_emotion_only(self):
        """§8 注 1：只用情绪词，不用意愿词 —— 意愿词会诱导 RP 表演"不想说话的人"。"""
        banned = ("不想", "愿意", "懒得", "说话", "沉默", "闭嘴", "意愿")
        for _, word in em.MOOD_TABLE:
            for b in banned:
                self.assertNotIn(b, word, f"mood 词 {word!r} 含意愿词 {b!r}")

    def test_mood_follows_score(self):
        p, _ = make()
        self.assertEqual(mood_for_score(p.score()), "轻快")
        for _ in range(3):
            p.register_blocked("conflict")
        self.assertAlmostEqual(p.score(), 0.7)
        self.assertEqual(mood_for_score(p.score()), "有点蔫")

    def test_mood_table_is_monotone(self):
        prev = None
        for threshold, _word in em.MOOD_TABLE:
            if prev is not None:
                self.assertLess(threshold, prev, "阈值必须严格递减")
            prev = threshold


# ---------------------------------------------------------------------------
# 3. 乘子与 0.40 悬崖（§6 / §7.2）
# ---------------------------------------------------------------------------
class TestMultiplier(unittest.TestCase):
    def test_formula(self):
        for s, want in ((1.0, 1.0), (0.8, 0.92), (0.6, 0.84), (0.5, 0.80),
                        (0.4, 0.76), (0.0, 0.60)):
            with self.subTest(score=s):
                self.assertAlmostEqual(multiplier_for_score(s), want, places=6)

    def test_below_cliff_rag_channel_is_mathematically_impossible(self):
        """§7.2：score < 0.40 ⇒ 乘子 < 0.76 ⇒ 0.79（raw 实测上限）× 乘子 < 0.60（阈值）。"""
        for s in (0.0, 0.1, 0.2, 0.3, 0.39):
            with self.subTest(score=s):
                mult = multiplier_for_score(s)
                self.assertLess(mult, 0.76)
                self.assertLess(0.79 * mult, 0.60)

    def test_exact_cliff_matches_arithmetic(self):
        # 精确临界 = 0.6/0.79 反解 = 0.39873…；§7.2 的书面口径是 0.40（差 0.3%）
        self.assertAlmostEqual(em.RAG_CLIFF_SCORE, (0.60 / 0.79 - 0.6) / 0.4, places=9)
        self.assertLess(em.RAG_CLIFF_SCORE, 0.40)
        self.assertAlmostEqual(0.79 * multiplier_for_score(em.RAG_CLIFF_SCORE), 0.60, places=9)
        # 0.40 正好卡在线上（0.79×0.76 = 0.6004 ≥ 0.6）；0.39 就够不到了
        self.assertGreaterEqual(0.79 * multiplier_for_score(0.40), 0.60)
        self.assertLess(0.79 * multiplier_for_score(0.39), 0.60)

    def test_state_willingness_is_multiplier_not_score(self):
        """量纲：global_willingness 是乘子位（pipeline 拿它当 emotion_multiplier），不是 score。"""
        p, _ = make(initial_score=0.5)
        st = asyncio.run(p.query("g", [], None))
        self.assertAlmostEqual(st.emotion_score, 0.5)
        self.assertAlmostEqual(st.global_willingness, 0.8)
        self.assertNotAlmostEqual(st.global_willingness, st.emotion_score)


# ---------------------------------------------------------------------------
# 4. 配置：可调参数必须真的改变输出 + schema 同步
# ---------------------------------------------------------------------------
class TestConfigKnobs(unittest.TestCase):
    def test_from_config_reads_every_key(self):
        p = ScoreEmotionProvider.from_config({
            "enabled": 1, "initial_score": 0.7, "blocked_penalty": 0.25,
            "recovery_per_min": 2.0, "min_score": 0.2, "max_score": 0.9,
            "recovery_log_step": 0.5,
        })
        self.assertAlmostEqual(p.initial_score, 0.7)
        self.assertAlmostEqual(p.blocked_penalty, 0.25)
        self.assertAlmostEqual(p.recovery_per_min, 2.0)
        self.assertAlmostEqual(p.min_score, 0.2)
        self.assertAlmostEqual(p.max_score, 0.9)
        self.assertAlmostEqual(p.recovery_log_step, 0.5)

    def test_from_config_tolerates_missing_and_legacy_keys(self):
        p = ScoreEmotionProvider.from_config({})
        self.assertAlmostEqual(p.score(), 1.0)
        self.assertAlmostEqual(p.blocked_penalty, 0.1)
        # LLM 时代的键已成死键：不该炸，也不该有任何影响
        p2 = ScoreEmotionProvider.from_config(
            {"temperature": 0.2, "reasoning_effort": "low",
             "timeout_sec": 30, "cache_ttl_sec": 30})
        self.assertAlmostEqual(p2.score(), 1.0)
        self.assertAlmostEqual(p2.blocked_penalty, 0.1)

    def test_every_knob_changes_output(self):
        """项目规矩：新增可调参数必须证明"改了参数会改变输出"（防代数消除/死参数）。"""
        def run(**kw):
            p, c = make(**kw)
            seq = [p.score(), p.register_blocked("conflict")]
            c.advance(60)
            seq.append(p.score())
            for _ in range(15):
                seq.append(p.register_blocked("conflict"))
            c.advance(3600)
            seq.append(p.score())
            return [round(x, 6) for x in seq]

        base = run()
        for name, alt in (("initial_score", 0.5), ("blocked_penalty", 0.3),
                          ("recovery_per_min", 0.0), ("min_score", 0.6),
                          ("max_score", 0.8)):
            with self.subTest(param=name):
                self.assertNotEqual(base, run(**{name: alt}),
                                    f"{name} 改了输出却没变 → 死参数")

    def test_schema_emotion_section_matches_code(self):
        """schema 的 emotion 段 == from_config 认得的键（防"配置了但没人读"）。"""
        with open(os.path.join(REPO, "_conf_schema.json"), encoding="utf-8") as f:
            schema = json.load(f)
        items = schema["emotion"]["items"]
        for key in ScoreEmotionProvider.CONFIG_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, items, f"schema 缺 {key}（代码读了但用户配不了）")
                self.assertIn("default", items[key])
        # 死键清理（C24 后无消费点）
        for dead in ("timeout_sec", "cache_ttl_sec", "temperature", "reasoning_effort"):
            self.assertNotIn(dead, items, f"{dead} 已无消费点，不该留在 emotion 段")

    def test_schema_defaults_equal_code_defaults(self):
        with open(os.path.join(REPO, "_conf_schema.json"), encoding="utf-8") as f:
            items = json.load(f)["emotion"]["items"]
        p = ScoreEmotionProvider.from_config(
            {k: v["default"] for k, v in items.items() if k != "enabled"})
        self.assertAlmostEqual(p.initial_score, 1.0)
        self.assertAlmostEqual(p.blocked_penalty, 0.1)
        self.assertAlmostEqual(p.recovery_per_min, 0.1)
        self.assertAlmostEqual(p.min_score, 0.0)
        self.assertAlmostEqual(p.max_score, 1.0)

    def test_enabled_key_still_present(self):
        with open(os.path.join(REPO, "_conf_schema.json"), encoding="utf-8") as f:
            items = json.load(f)["emotion"]["items"]
        self.assertIn("enabled", items)


# ---------------------------------------------------------------------------
# 5. register_blocked：幂等 / 并发 / 日志留痕
# ---------------------------------------------------------------------------
class TestRegisterBlocked(unittest.TestCase):
    def test_returns_new_score(self):
        p, _ = make()
        self.assertAlmostEqual(p.register_blocked("conflict"), 0.9)

    def test_idempotent_by_event_key(self):
        p, _ = make()
        p.register_blocked("conflict", event_key="ev-1")
        p.register_blocked("conflict", event_key="ev-1")
        self.assertAlmostEqual(p.score(), 0.9)
        self.assertEqual(p.stats["penalty_applied"], 1)
        self.assertEqual(p.stats["duplicate_skipped"], 1)

    def test_event_key_memory_is_bounded(self):
        """记忆有上限（LRU）：只对最近 N 个 key 幂等，避免无界增长。"""
        p, _ = make(event_key_memory=4)
        for i in range(10):
            p.register_blocked("conflict", event_key=f"e{i}")
        self.assertEqual(p.stats["duplicate_skipped"], 0)
        p.register_blocked("conflict", event_key="e9")   # 最近的 key 仍在记忆里
        self.assertEqual(p.stats["duplicate_skipped"], 1)
        p.register_blocked("conflict", event_key="e0")   # 早已被淘汰 → 再扣一次
        self.assertEqual(p.stats["duplicate_skipped"], 1)
        self.assertEqual(p.stats["penalty_applied"], 11)   # 10 + 淘汰后的 e0（e9 被去重）
        self.assertLessEqual(len(p._seen_keys), 4)

    def test_without_event_key_every_call_counts(self):
        p, _ = make()
        p.register_blocked("conflict")
        p.register_blocked("conflict")
        self.assertEqual(p.stats["duplicate_skipped"], 0)
        self.assertAlmostEqual(p.score(), 0.8)

    def test_concurrent_calls_are_lossless(self):
        """并发安全（有锁）：多线程同时打分不能丢更新。"""
        p, _ = make(blocked_penalty=0.001, recovery_per_min=0.0, event_key_memory=1)
        n_threads, per = 16, 50
        barrier = threading.Barrier(n_threads)

        def worker():
            barrier.wait()
            for _ in range(per):
                p.register_blocked("conflict")

        ts = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(p.stats["penalty_applied"], n_threads * per)
        self.assertAlmostEqual(p.score(), max(0.0, 1.0 - 0.001 * n_threads * per), places=9)

    def test_concurrent_duplicate_event_key_counts_once(self):
        p, _ = make(event_key_memory=1)
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            for _ in range(20):
                p.register_blocked("conflict", event_key="same")

        ts = [threading.Thread(target=worker) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(p.stats["penalty_applied"], 1)
        self.assertAlmostEqual(p.score(), 0.9)

    def test_every_penalty_logs_score_change(self):
        """每次变化落日志（用户口径：先跑几天拿 blocked 真实频率再定值）。"""
        p, _ = make()
        with self.assertLogs("services.emotion", level="INFO") as cm:
            p.register_blocked("conflict", group_id="100000001")
        line = "\n".join(cm.output)
        self.assertIn("blocked", line)
        self.assertIn("conflict", line)
        self.assertIn("100000001", line)
        self.assertIn("0.90", line)          # 变化后的分数
        self.assertIn("轻快", line)          # 变化前后的心情词

    def test_duplicate_skip_is_logged(self):
        p, _ = make()
        p.register_blocked("conflict", event_key="ev")
        with self.assertLogs("services.emotion", level="INFO") as cm:
            p.register_blocked("conflict", event_key="ev")
        self.assertTrue(any("重复" in m or "duplicate" in m for m in cm.output))

    def test_cliff_crossing_warns_that_rag_is_shut(self):
        p, _ = make(blocked_penalty=0.3)
        with self.assertLogs("services.emotion", level="WARNING") as cm:
            p.register_blocked("conflict")      # 1.0 → 0.7
            p.register_blocked("conflict")      # 0.7 → 0.4（还在悬崖之上）
            p.register_blocked("conflict")      # 0.4 → 0.1 跨过悬崖
        self.assertTrue(any("RAG 通道关闭" in m for m in cm.output))

    def test_cliff_reopen_logs_info(self):
        p, c = make(blocked_penalty=0.3)
        for _ in range(3):
            p.register_blocked("conflict")      # → 0.1（通道关闭）
        c.advance(180)                          # 恢复 +0.3 → 0.4 ≥ 悬崖
        with self.assertLogs("services.emotion", level="INFO") as cm:
            p.score()
        self.assertTrue(any("RAG 通道恢复" in m for m in cm.output))

    def test_recovery_log_step_controls_info_volume(self):
        p, c = make(blocked_penalty=0.5, recovery_log_step=0.05)
        p.register_blocked("conflict")
        with self.assertNoLogs("services.emotion", level="INFO"):
            c.advance(10)                       # +0.0167 < 0.05 → 不落
            p.score()
        with self.assertLogs("services.emotion", level="INFO") as cm:
            c.advance(40)                       # 累计 +0.083 ≥ 0.05 → 落一条
            p.score()
        self.assertTrue(any("恢复" in m for m in cm.output))

    def test_recovery_log_step_zero_logs_every_step(self):
        p, c = make(blocked_penalty=0.5, recovery_log_step=0.0)
        p.register_blocked("conflict")
        with self.assertLogs("services.emotion", level="INFO") as cm:
            c.advance(1)
            p.score()
        self.assertTrue(any("恢复" in m for m in cm.output))


# ---------------------------------------------------------------------------
# 6. 观测（降级可见）
# ---------------------------------------------------------------------------
class TestObservability(unittest.TestCase):
    def test_snapshot_reports_state(self):
        p, _ = make()
        p.register_blocked("conflict", group_id="g1")
        snap = p.snapshot()
        self.assertAlmostEqual(snap["score"], 0.9)
        self.assertAlmostEqual(snap["multiplier"], 0.96)
        self.assertEqual(snap["mood"], "平常")
        self.assertTrue(snap["rag_channel_open"])
        self.assertEqual(snap["stats"]["blocked"], 1)
        self.assertEqual(snap["recent_blocked"][-1]["reason"], "conflict")
        self.assertEqual(snap["recent_blocked"][-1]["group_id"], "g1")

    def test_snapshot_flags_closed_rag_channel(self):
        p, _ = make()
        for _ in range(7):
            p.register_blocked("conflict")      # 1.0 → 0.3 < 悬崖
        snap = p.snapshot()
        self.assertFalse(snap["rag_channel_open"])

    def test_recent_blocked_is_bounded(self):
        p, _ = make(recent_events=5)
        for i in range(12):
            p.register_blocked(f"r{i}")
        self.assertEqual(len(p.recent_blocked), 5)
        self.assertEqual(p.recent_blocked[-1]["reason"], "r11")

    def test_last_error_cleared_by_successful_query(self):
        p, c = make()
        p.register_blocked("conflict")
        c.advance(-30)
        p.score()
        self.assertIsNotNone(p.last_error)
        st = asyncio.run(p.query("g", [], None))
        self.assertIsNone(p.last_error)
        self.assertAlmostEqual(st.emotion_score, 0.9)

    def test_query_counts_are_recorded(self):
        p, _ = make()
        asyncio.run(p.query("g", [], None))
        asyncio.run(p.query("g", [], None))
        self.assertEqual(p.stats["queries"], 2)


# ---------------------------------------------------------------------------
# 7. 对外接口（pipeline 现有接线不改也对）
# ---------------------------------------------------------------------------
class TestQueryInterface(unittest.TestCase):
    def test_query_maps_score_to_state(self):
        p, _ = make()
        for _ in range(3):
            p.register_blocked("conflict")
        st = asyncio.run(p.query("g", [], None))
        self.assertAlmostEqual(st.emotion_score, 0.7)
        self.assertAlmostEqual(st.global_willingness, multiplier_for_score(0.7))
        self.assertEqual(st.current_mood, mood_for_score(0.7))

    def test_query_does_not_accept_llm_fn(self):
        """C24：LLM 调用整条移除 —— 不该再有任何 llm_fn 入口。"""
        with self.assertRaises(TypeError):
            ScoreEmotionProvider(llm_fn=lambda p: "{}")

    def test_query_is_repeatable(self):
        p, _ = make()
        st1 = asyncio.run(p.query("g", [], None))
        st2 = asyncio.run(p.query("g", [], None))
        self.assertEqual(st1, st2)

    def test_state_defaults_are_neutral_and_silent(self):
        st = EmotionState.neutral()
        self.assertEqual(st.global_willingness, 1.0)
        self.assertEqual(st.emotion_score, 1.0)
        self.assertEqual(st.current_mood, "")   # 关闭/缺省 = 不注入心情（功能关闭必须静默）


class TestDefaultProvider(unittest.TestCase):
    def test_disabled_provider_is_noop_and_silent(self):
        p = DefaultEmotionProvider()
        with self.assertNoLogs("services.emotion", level="INFO"):
            self.assertEqual(p.register_blocked("conflict"), 1.0)
            self.assertEqual(p.score(), 1.0)
            self.assertEqual(p.multiplier(), 1.0)
            st = asyncio.run(p.query("g", [], None))
        self.assertEqual(st, EmotionState.neutral())

    def test_base_class_exposes_neutral_hooks(self):
        """pipeline 可以无条件调用 register_blocked/multiplier —— 关闭时退化成满格。"""
        for name in ("register_blocked", "score", "multiplier", "snapshot"):
            self.assertTrue(hasattr(EmotionProvider, name), name)


# ---------------------------------------------------------------------------
# 8. C25：sticker / 文生图通道**整条不存在**
# ---------------------------------------------------------------------------
class TestStickerRemovalC25(unittest.TestCase):
    """收尾态（主代理 2026-09-21 接线后）：通道不是"废弃但还在"，而是**根本不存在**。

    过渡期的空属性桥与 LLM 垫片已随接线删除 —— 留着它们就等于留着一条
    随时能被悄悄接回来的通道（本项目栽过的"降级不可见"同类）。
    """

    def test_no_sticker_field_in_dataclass(self):
        self.assertNotIn("sticker_prompt", {f.name for f in dataclasses.fields(EmotionState)})

    def test_sticker_prompt_attribute_is_gone(self):
        """读它就该 AttributeError —— 桥删了就是删了（写普通属性 Python 允许，不必防）。"""
        st = EmotionState()
        with self.assertRaises(AttributeError):
            _ = st.sticker_prompt

    def test_llm_shim_is_gone(self):
        """`LLMEmotionProvider` 过渡桥已删 —— 想接回来必须显式改代码，不能靠旧调用复活。"""
        self.assertFalse(hasattr(em, "LLMEmotionProvider"))

    def test_emotion_system_prompt_deleted(self):
        self.assertFalse(hasattr(em, "EMOTION_SYSTEM_PROMPT"))

    def test_query_returns_state_without_sticker(self):
        p, _ = make()
        st = asyncio.run(p.query("g", [], None))
        self.assertFalse(hasattr(st, "sticker_prompt"))
