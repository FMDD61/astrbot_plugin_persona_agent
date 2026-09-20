# -*- coding: utf-8 -*-
"""做梦（S11）：取日记 → 残缺化 → **做梦**（单阶段，C33⑤）。

用户 2026-09-14 定的口径（2026-09-19 的 v2 修正见括号）：
- 原料 = 最近 **7 个不同 day** 的日记（**取 body**，C33②）；不足按实际数量
- **残缺化** = 每篇随机丢弃 **20%~30%** 的句子
- 温度 1.3 / 思考强度 low
- 样例文本是**参考**，不是要照搬的结构模板
- **不做熟悉度汇报**（已从 DreamJob 剥离）
- 产物要推送（**没有 LLM 消费者**，C33①）
- system 只给 **§4 世界段**（C33④）；**锚点阶段已删**（C33⑤）
"""
import asyncio
import inspect
import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.dream import (  # noqa: E402
    DROP_MAX, DROP_MIN, DreamMaker, DreamResult, build_dream_prompt,
    fragment, gather_diaries, load_fragments, persist_dream, split_sentences,
)

LONG = ("今天群里还是老样子热闹，成员卯一整天在那娶群友，娶成员寅娶成员戊全被拒了。"
        "成员乙和成员甲又开始互相损，我在旁边看戏顺便拱火。"
        "成员辰折腾新名字，说自己是成员丁，被我笑了一顿。"
        "晚上一堆人喊晚安，成员酉那家伙又熬夜，真是的。")


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
        rows = [{"day": f"2026-09-{d:02d}", "group_id": "g1", "body": f"第{d}天"}
                for d in range(1, 15)]
        self._write(self.gdir / "daily_diary.jsonl", rows)
        got = gather_diaries(self.td.name, "g1")
        self.assertEqual(len(got), 7)
        self.assertEqual(got[-1]["day"], "2026-09-14")     # 最新在最后（时间顺序）
        self.assertEqual(got[0]["day"], "2026-09-08")

    def test_duplicate_day_takes_latest(self):
        """实测同日多条（B-018 幂等是后加的）→ 取最新一条，不依赖清理历史。"""
        self._write(self.gdir / "daily_diary.jsonl", [
            {"day": "2026-09-13", "group_id": "g1", "body": "旧",
             "created_at": "2026-09-14T02:05:00Z"},
            {"day": "2026-09-13", "group_id": "g1", "body": "新",
             "created_at": "2026-09-14T02:06:00Z"},
        ])
        got = gather_diaries(self.td.name, "g1")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["body"], "新")

    def test_reads_both_locations(self):
        """取材位置有两个（现行 logs/<gid>/ 与早期根目录）—— 都要读。"""
        self._write(self.gdir / "daily_diary.jsonl",
                    [{"day": "2026-09-13", "group_id": "g1", "body": "群目录"}])
        self._write(Path(self.td.name) / "daily_diary.jsonl",
                    [{"day": "2026-09-12", "group_id": "g1", "body": "根目录"}])
        got = gather_diaries(self.td.name, "g1")
        self.assertEqual([d["day"] for d in got], ["2026-09-12", "2026-09-13"])

    def test_filters_other_groups(self):
        self._write(self.gdir / "daily_diary.jsonl", [
            {"day": "2026-09-13", "group_id": "other", "body": "别人的"},
            {"day": "2026-09-13", "group_id": "g1", "body": "我的"},
        ])
        got = gather_diaries(self.td.name, "g1")
        self.assertEqual([d["body"] for d in got], ["我的"])

    def test_tolerates_corrupt_lines(self):
        p = self.gdir / "daily_diary.jsonl"
        p.write_text('{坏行\n{"day":"2026-09-13","group_id":"g1","body":"好的"}\n\n',
                     encoding="utf-8")
        self.assertEqual(len(gather_diaries(self.td.name, "g1")), 1)

    def test_fewer_than_seven_is_fine(self):
        """不足 7 篇按实际数量做（用户明确），不报错。"""
        self._write(self.gdir / "daily_diary.jsonl",
                    [{"day": "2026-09-13", "group_id": "g1", "body": "只有一篇"}])
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
                 "body": LONG, "created_at": f"2026-09-{d:02d}T03:00:00Z"}
                for d in range(8, 15)]
        (gdir / "daily_diary.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8")

    def tearDown(self):
        self.td.cleanup()

    def test_single_stage_flow(self):
        seen = {}

        async def dream_fn(sys_p, prompt):
            seen["dream_sys"] = sys_p
            seen["dream_prompt"] = prompt
            return "我梦见了那些傍晚。光像融化的糖浆。"

        mk = DreamMaker(self.td.name, dream_fn=dream_fn, seed=1)
        res = asyncio.run(mk.make("g1"))
        self.assertEqual(res.diaries_used, 7)
        self.assertIn("糖浆", res.text)
        # C33⑤：锚点阶段已删 —— 提示词里**不得**再出现"感觉锚点"段
        self.assertNotIn("感觉锚点", seen["dream_prompt"])
        self.assertNotIn("anchors", json.dumps(res.stats, ensure_ascii=False))

    def test_budgets_are_generous(self):
        """🔴 回归：预算必须给足（用户 2026-09-14 的核心批评）。

        思考 token 是**重尾随机变量**（实测同一 prompt 556~2048），
        `max_tokens` 是**上限而非消耗** —— 给紧只会在运气差时截断成空 content。
        实测：2048 会顶格成空、8192/16384 稳定（当时在锚点阶段观测到，结论对整条链成立）。
        C33⑤ 删掉锚点阶段后，这里只剩做梦这一条调用可守。
        """
        import json as _json, os as _os
        p = _os.path.join(_os.path.dirname(__file__), "..", "_conf_schema.json")
        d = _json.load(open(p, encoding="utf-8"))["dream"]["items"]
        self.assertGreaterEqual(d["max_tokens"]["default"], 8192)

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
        build_dream_prompt([{"day": "d", "text": "x"}], [])   # 冒烟：签名已无锚点参数
        # 提示词本体在 DREAM_SYSTEM 里，这里确认能拼进去
        from services.dream import DREAM_SYSTEM
        for must in ("仿佛整个世界都安静了", "梦醒了", "不要照抄", "沉降"):
            self.assertIn(must, DREAM_SYSTEM, f"缺少防退化条款: {must}")


if __name__ == "__main__":
    unittest.main()


class TestC33DreamV2(unittest.TestCase):
    """C33：dream 四项改造的回归（原料 body / §4 世界段 / 注释更正 / 删锚点）。

    设计依据：`docs/specs/dream_v2.md` §5③/§8/§9/§10。
    """

    def _write_day(self, td, day, **fields):
        gdir = Path(td) / "logs" / "g1"
        gdir.mkdir(parents=True, exist_ok=True)
        rec = {"day": day, "group_id": "g1", **fields}
        with open(gdir / "daily_diary.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def test_dream_reads_body_not_digest(self):
        """§7.3①：原料是 body（digest 不进梦）。"""
        with tempfile.TemporaryDirectory() as td:
            self._write_day(td, "2026-09-13", digest="一句话摘要",
                            body="这一天发生的正文")
            got = gather_diaries(td, "g1")
            self.assertEqual(got[0]["body"], "这一天发生的正文")
            self.assertEqual(got[0]["digest"], "一句话摘要")
            self.assertFalse(got[0]["legacy_summary"])

    def test_legacy_record_without_body_still_usable(self):
        """🔴 迁移桥：旧记录只有 summary → 回落为 body 并**标出来**。

        不回落的话，迁移首日 dream/周报的原料会全空，而表现是「没有日记」——
        正是本项目反复栽的静默失效（独立核验第 1 轮风险 2）。
        """
        with tempfile.TemporaryDirectory() as td:
            self._write_day(td, "2026-09-13", summary="旧格式的那一天")
            got = gather_diaries(td, "g1")
            self.assertEqual(got[0]["body"], "旧格式的那一天")
            self.assertTrue(got[0]["legacy_summary"])

    def test_legacy_fallback_counted_in_stats(self):
        """回落条数必须进 stats（降级可见，不靠人肉比对）。"""
        with tempfile.TemporaryDirectory() as td:
            for d in range(8, 15):
                self._write_day(td, f"2026-09-{d:02d}", summary=LONG)
            mk = DreamMaker(td, seed=1)
            res, frag, _ = mk.prepare("g1")
            self.assertEqual(res.stats.get("legacy_summary_diaries"), 7)
            self.assertTrue(all("text" in f for f in frag))

    def test_world_block_appended_to_system(self):
        """C33④：梦的 system = DREAM_SYSTEM + §4 世界段（梦唯一的设定来源）。"""
        from services.dream import DREAM_SYSTEM, build_dream_system
        self.assertEqual(build_dream_system(""), DREAM_SYSTEM.strip())
        got = build_dream_system("【我待的这个地方】\n这里是测试群")
        self.assertIn("【我待的这个地方】", got)
        self.assertTrue(got.startswith(DREAM_SYSTEM.strip()))

    def test_maker_passes_world_block_into_system(self):
        seen = {}

        async def dream_fn(sys_p, prompt):
            seen["sys"] = sys_p
            return "梦"

        with tempfile.TemporaryDirectory() as td:
            self._write_day(td, "2026-09-13", body=LONG)
            mk = DreamMaker(td, dream_fn=dream_fn, seed=1,
                            world_block="【我待的这个地方】世界段")
            asyncio.run(mk.make("g1"))
            self.assertIn("【我待的这个地方】", seen["sys"])
            self.assertNotIn("感觉锚点", seen["sys"])

    def test_persist_has_no_anchors_field(self):
        """C33③ 作废的连带：落盘记录里不该再有 anchors 字段。"""
        with tempfile.TemporaryDirectory() as td:
            mk = DreamMaker(td, seed=1)
            res = DreamResult(text="梦", days=["2026-09-13"], diaries_used=1)
            rec = persist_dream(td, "g1", res)
            self.assertNotIn("anchors", rec)
            self.assertIn("dream", rec)

    def test_no_anchor_symbols_remain_in_module(self):
        """删除要彻底：模块里不得再留锚点阶段的任何符号。"""
        import services.dream as dm
        for gone in ("ANCHOR_SYSTEM", "parse_anchors", "build_anchor_prompt"):
            self.assertFalse(hasattr(dm, gone), f"{gone} 未删干净")
            self.assertNotIn(gone, inspect.getsource(dm))
