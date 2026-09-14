# -*- coding: utf-8 -*-
"""做梦（S11）：取日记 → 残缺化 → 抽锚点 → 做梦。

用户 2026-09-14 定的口径：
- 原料 = 最近 **7 个不同 day** 的日记；不足按实际数量
- **残缺化** = 每篇随机丢弃 **20%~30%** 的句子
- 温度 1.3 / 思考强度 low
- 样例文本是**参考**，不是要照搬的结构模板
- **不做熟悉度汇报**（已从 DreamJob 剥离）
- 产物要推送
"""
import asyncio
import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.dream import (  # noqa: E402
    DROP_MAX, DROP_MIN, DreamMaker, build_dream_prompt, fragment,
    gather_diaries, load_fragments, persist_dream, split_sentences,
)

LONG = ("今天群里还是老样子热闹，橘包一整天在那娶群友，娶木鱼娶内桑全被拒了。"
        "虾鱼丸和花鱼又开始互相损，我在旁边看戏顺便拱火。"
        "信施折腾新名字，说自己是夕化碳，被我笑了一顿。"
        "晚上一堆人喊晚安，风来那家伙又熬夜，真是的。")


class TestSplitAndFragment(unittest.TestCase):
    def test_split_keeps_punctuation(self):
        s = split_sentences("甲。乙！丙？丁；")
        self.assertEqual(s, ["甲。", "乙！", "丙？", "丁；"])

    def test_empty(self):
        self.assertEqual(split_sentences(""), [])
        self.assertEqual(split_sentences("   "), [])

    def test_drop_ratio_within_20_30_percent(self):
        """🔴 用户指定：每篇随机丢弃 **20%~30%** 的句子。"""
        sents = split_sentences(LONG)
        n = len(sents)
        rnd = random.Random(11)
        for _ in range(150):
            kept = len(split_sentences(fragment(LONG, rnd)))
            ratio = (n - kept) / n
            self.assertGreaterEqual(ratio, DROP_MIN - 0.01,
                                    f"丢弃 {ratio:.0%} 低于下限 20%")
            self.assertLessEqual(ratio, DROP_MAX + 0.01,
                                 f"丢弃 {ratio:.0%} 高于上限 30%")

    def test_always_keeps_at_least_one_sentence(self):
        rnd = random.Random(5)
        for _ in range(50):
            out = fragment(LONG, rnd)
            self.assertTrue(split_sentences(out), "不得丢空")

    def test_short_text_not_fragmented(self):
        """少于 3 句不丢 —— 丢了就不成篇。"""
        rnd = random.Random(1)
        self.assertEqual(fragment("就一句话。", rnd), "就一句话。")
        self.assertEqual(fragment("两句。而已。", rnd), "两句。而已。")

    def test_fragment_is_subsequence(self):
        """残缺化只**删句**，不改写、不重排、不截断半句。"""
        rnd = random.Random(9)
        orig = split_sentences(LONG)
        out = split_sentences(fragment(LONG, rnd))
        # 每个保留的句子必须与原文某句逐字相同
        for s in out:
            self.assertIn(s, orig, f"被改写/截断了: {s!r}")
        # 且顺序一致
        idx = [orig.index(s) for s in out]
        self.assertEqual(idx, sorted(idx), "不得重排")


class TestGatherDiaries(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.gdir = Path(self.td.name) / "logs" / "g1"
        self.gdir.mkdir(parents=True)

    def tearDown(self):
        self.td.cleanup()

    def _write(self, path: Path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                        encoding="utf-8")

    def test_takes_last_seven_distinct_days(self):
        rows = [{"day": f"2026-09-{d:02d}", "group_id": "g1", "summary": f"第{d}天"}
                for d in range(1, 15)]
        self._write(self.gdir / "daily_diary.jsonl", rows)
        got = gather_diaries(self.td.name, "g1")
        self.assertEqual(len(got), 7)
        self.assertEqual(got[-1]["day"], "2026-09-14")     # 最新在最后（时间顺序）
        self.assertEqual(got[0]["day"], "2026-09-08")

    def test_duplicate_day_takes_latest(self):
        """实测同日多条（B-018 幂等是后加的）→ 取最新一条，不依赖清理历史。"""
        self._write(self.gdir / "daily_diary.jsonl", [
            {"day": "2026-09-13", "group_id": "g1", "summary": "旧",
             "created_at": "2026-09-14T02:05:00Z"},
            {"day": "2026-09-13", "group_id": "g1", "summary": "新",
             "created_at": "2026-09-14T02:06:00Z"},
        ])
        got = gather_diaries(self.td.name, "g1")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["summary"], "新")

    def test_reads_both_locations(self):
        """取材位置有两个（现行 logs/<gid>/ 与早期根目录）—— 都要读。"""
        self._write(self.gdir / "daily_diary.jsonl",
                    [{"day": "2026-09-13", "group_id": "g1", "summary": "群目录"}])
        self._write(Path(self.td.name) / "daily_diary.jsonl",
                    [{"day": "2026-09-12", "group_id": "g1", "summary": "根目录"}])
        got = gather_diaries(self.td.name, "g1")
        self.assertEqual([d["day"] for d in got], ["2026-09-12", "2026-09-13"])

    def test_filters_other_groups(self):
        self._write(self.gdir / "daily_diary.jsonl", [
            {"day": "2026-09-13", "group_id": "other", "summary": "别人的"},
            {"day": "2026-09-13", "group_id": "g1", "summary": "我的"},
        ])
        got = gather_diaries(self.td.name, "g1")
        self.assertEqual([d["summary"] for d in got], ["我的"])

    def test_tolerates_corrupt_lines(self):
        p = self.gdir / "daily_diary.jsonl"
        p.write_text('{坏行\n{"day":"2026-09-13","group_id":"g1","summary":"好的"}\n\n',
                     encoding="utf-8")
        self.assertEqual(len(gather_diaries(self.td.name, "g1")), 1)

    def test_fewer_than_seven_is_fine(self):
        """不足 7 篇按实际数量做（用户明确），不报错。"""
        self._write(self.gdir / "daily_diary.jsonl",
                    [{"day": "2026-09-13", "group_id": "g1", "summary": "只有一篇"}])
        self.assertEqual(len(gather_diaries(self.td.name, "g1")), 1)

    def test_no_diaries_returns_empty(self):
        self.assertEqual(gather_diaries(self.td.name, "g1"), [])


class TestFragments(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(load_fragments(td), [])

    def test_filters_by_id(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "dream_fragments.json"
            p.write_text(json.dumps({"items": [
                {"id": "a3", "author": "鲁迅", "text": "甲"},
                {"id": "zz", "author": "别人", "text": "乙"},
            ]}, ensure_ascii=False), encoding="utf-8")
            got = load_fragments(td, ids=("a3",))
            self.assertEqual([x["id"] for x in got], ["a3"])


class TestDreamMaker(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        gdir = Path(self.td.name) / "logs" / "g1"
        gdir.mkdir(parents=True)
        rows = [{"day": f"2026-09-{d:02d}", "group_id": "g1",
                 "summary": LONG, "created_at": f"2026-09-{d:02d}T03:00:00Z"}
                for d in range(8, 15)]
        (gdir / "daily_diary.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8")

    def tearDown(self):
        self.td.cleanup()

    def test_two_stage_flow(self):
        seen = {}

        async def anchor_fn(sys_p, prompt):
            seen["anchor_sys"] = sys_p
            seen["anchor_prompt"] = prompt
            # 锚点阶段用 JSON schema（实测自由文本会让思考吃光 token，见 ANCHOR_SYSTEM 注释）
            return '{"anchors": ["低烧不退", "凌晨的屏幕光"]}'

        async def dream_fn(sys_p, prompt):
            seen["dream_sys"] = sys_p
            seen["dream_prompt"] = prompt
            return "我梦见了那些傍晚。光像融化的糖浆。"

        mk = DreamMaker(self.td.name, anchor_fn=anchor_fn, dream_fn=dream_fn, seed=1)
        res = asyncio.run(mk.make("g1"))
        self.assertEqual(res.diaries_used, 7)
        self.assertIn("糖浆", res.text)
        self.assertIn("低烧", res.anchors)
        # 锚点结果必须进入做梦提示词（这正是子代理指出的"意象源"缺口）
        self.assertIn("低烧", seen["dream_prompt"])
        self.assertIn("感觉锚点", seen["dream_prompt"])
        self.assertIn("身体和感官", seen["anchor_sys"])
        self.assertIn('"anchors"', seen["anchor_sys"], "锚点阶段必须约定 JSON 格式")

    def test_parse_anchors_forms(self):
        from services.dream import parse_anchors
        self.assertEqual(parse_anchors('{"anchors": ["甲", "乙"]}'), "甲\n乙")
        self.assertEqual(parse_anchors('```json\n{"anchors": ["甲"]}\n```'), "甲")
        self.assertEqual(parse_anchors('前言 {"anchors": ["丙"]} 后语'), "丙")
        self.assertEqual(parse_anchors("自由文本"), "自由文本")   # 兜底原样
        self.assertEqual(parse_anchors(""), "")
        self.assertEqual(parse_anchors('{"anchors": []}'), '{"anchors": []}')  # 空列表兜底

    def test_anchor_failure_degrades_gracefully(self):
        """锚点失败不致命 —— 降级为直接做梦（仍有日记原料）。"""
        async def boom(sys_p, prompt):
            raise RuntimeError("anchor boom")

        async def dream_fn(sys_p, prompt):
            return "梦"

        mk = DreamMaker(self.td.name, anchor_fn=boom, dream_fn=dream_fn, seed=1)
        res = asyncio.run(mk.make("g1"))
        self.assertEqual(res.text, "梦")
        self.assertIn("anchor_error", res.stats)

    def test_dream_failure_reported(self):
        async def dream_fn(sys_p, prompt):
            raise RuntimeError("dream boom")

        mk = DreamMaker(self.td.name, dream_fn=dream_fn, seed=1)
        res = asyncio.run(mk.make("g1"))
        self.assertEqual(res.text, "")
        self.assertIn("dream_failed", res.stats["error"])

    def test_empty_dream_is_error(self):
        async def dream_fn(sys_p, prompt):
            return "   "

        mk = DreamMaker(self.td.name, dream_fn=dream_fn, seed=1)
        res = asyncio.run(mk.make("g1"))
        self.assertEqual(res.stats.get("error"), "empty_dream")

    def test_no_diaries_short_circuits(self):
        with tempfile.TemporaryDirectory() as td:
            mk = DreamMaker(td, dream_fn=lambda *a: None)
            res = asyncio.run(mk.make("g1"))
            self.assertEqual(res.stats.get("error"), "no_diaries")

    def test_dropped_ratio_in_range(self):
        mk = DreamMaker(self.td.name, seed=42)
        res, frag, frags = mk.prepare("g1")
        self.assertGreaterEqual(res.dropped_ratio, DROP_MIN - 0.01)
        self.assertLessEqual(res.dropped_ratio, DROP_MAX + 0.01)
        self.assertEqual(len(frag), 7)

    def test_persist_writes_jsonl(self):
        mk = DreamMaker(self.td.name, seed=1)
        res, _, _ = mk.prepare("g1")
        res.text = "梦的内容"
        rec = persist_dream(self.td.name, "g1", res)
        p = Path(self.td.name) / "logs" / "g1" / "dreams.jsonl"
        self.assertTrue(p.exists())
        rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dream"], "梦的内容")
        self.assertEqual(rows[0]["days"], rec["days"])
        self.assertEqual(rows[0]["diaries_used"], 7)

    def test_prompt_has_guardrails(self):
        """防退化条款必须在（禁 AI 套语/禁元叙述/禁止照抄）。"""
        p = build_dream_prompt([{"day": "d", "summary": "x"}], "锚点", [])
        # 提示词本体在 DREAM_SYSTEM 里，这里确认能拼进去
        from services.dream import DREAM_SYSTEM
        for must in ("仿佛整个世界都安静了", "梦醒了", "不要照抄", "沉降"):
            self.assertIn(must, DREAM_SYSTEM, f"缺少防退化条款: {must}")


if __name__ == "__main__":
    unittest.main()
