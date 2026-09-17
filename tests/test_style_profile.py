# -*- coding: utf-8 -*-
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.style_profile import StyleProfile

REL = {"members": [{"uin": "1", "alias": "已知", "closeness": "close"}]}


class TestAddNewMember(unittest.TestCase):
    def _make(self, td):
        with open(os.path.join(td, "member_relations.json"), "w", encoding="utf-8") as f:
            json.dump(REL, f, ensure_ascii=False)
        return StyleProfile(td)

    def test_appends_new(self):
        with tempfile.TemporaryDirectory() as td:
            sp = self._make(td)
            self.assertTrue(sp.add_new_member("99", "新人"))
            rel = json.load(open(os.path.join(td, "member_relations.json"), encoding="utf-8"))
            entry = [m for m in rel["members"] if m["uin"] == "99"][0]
            self.assertEqual(entry["alias"], "新人")
            self.assertEqual(entry["closeness"], "new")
            self.assertTrue(entry["auto_added"])
            self.assertEqual(len(rel["members"]), 2)
            # reload sees it
            self.assertEqual(sp.preferred_alias("99"), "新人")

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            sp = self._make(td)
            self.assertTrue(sp.add_new_member("99", "新人"))
            self.assertFalse(sp.add_new_member("99", "新人"))
            self.assertEqual(len(REL["members"]) + 1, 2)

    def test_empty_name_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            sp = self._make(td)
            sp.add_new_member("77", "  ")
            self.assertEqual(sp.preferred_alias("77"), "群友77")

    def test_collision_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            sp = self._make(td)
            sp.add_new_member("77", "已知")  # alias already used by uin=1
            self.assertEqual(sp.preferred_alias("77"), "群友77")


class TestFilterAlreadyAnnounced(unittest.TestCase):
    """🔴 B-032（2026-09-17 独立核验反例 1）：头部冻结块里已写着的行不得再追加。

    会话边界（日轮转后首轮）头部块用**当时活的**图谱冻结 —— 已含新成员；
    而增量仍按 `known` 算 → 同一变更 head 与 tail 各讲一遍，且尾部那条会留一整天。
    """

    def test_drops_lines_already_in_frozen_block(self):
        frozen = "【熟人】\n  2: 乙  [新人]\n  1: 甲  [熟人]"
        self.assertEqual(
            StyleProfile.filter_already_announced(["  2: 乙  [新人]"], frozen), [])
        self.assertEqual(
            StyleProfile.filter_already_announced(
                ["  2: 乙  [新人]", "  9: 己  [新人]"], frozen),
            ["  9: 己  [新人]"], "只有冻块里没有的行才留下")

    def test_empty_frozen_block_is_noop(self):
        lines = ["  1: 甲  [熟人]", "  2: 乙  [新人]"]
        self.assertEqual(StyleProfile.filter_already_announced(lines, ""), lines)

    def test_drops_blank_lines(self):
        self.assertEqual(StyleProfile.filter_already_announced(["", "   "], "x"), [])


class TestCacheStableSystemPrompt(unittest.TestCase):
    """2026-09-13 缓存重排：system prompt 必须**逐轮完全恒定**。

    system prompt 是请求的第一个 token 位置 —— 它一变，其后整段会话前缀
    （可达上千条）全部 miss。旧实现把「现在本地时间 HH 时」拼在它的末尾
    （每小时变一次），心情有值时更是每 30s 变一次。时间/心情现已移到上下文
    末尾的 volatile_line()。
    """

    def _make(self, td):
        with open(os.path.join(td, "member_relations.json"), "w", encoding="utf-8") as f:
            json.dump(REL, f, ensure_ascii=False)
        return StyleProfile(td)

    def test_system_prompt_ignores_hour(self):
        with tempfile.TemporaryDirectory() as td:
            sp = self._make(td)
            # 无论传不传 local_hour（含旧调用点），结果必须一致
            self.assertEqual(sp.system_prompt(), sp.system_prompt(local_hour=3))
            self.assertEqual(sp.system_prompt(), sp.system_prompt(local_hour=21))
            self.assertNotIn("现在本地时间", sp.system_prompt())
            self.assertNotIn("现在本地时间", sp.system_prompt(local_hour=9))

    def test_volatile_line_carries_hour_and_mood(self):
        with tempfile.TemporaryDirectory() as td:
            sp = self._make(td)
            self.assertEqual(sp.volatile_line(local_hour=14), "现在本地时间 14 时。")
            self.assertEqual(
                sp.volatile_line(local_hour=3, mood="有点困"),
                "现在本地时间 03 时。\n当前心情：有点困",
            )
            self.assertEqual(sp.volatile_line(local_hour=None, mood=""), "")
            # 非法小时 → 不出时间行，只留心情
            self.assertEqual(sp.volatile_line(local_hour=99, mood=""), "")
            self.assertEqual(sp.volatile_line(local_hour=None, mood="烦"), "当前心情：烦")

    def test_system_prompt_stable_across_hours_is_the_point(self):
        """不变式：24 小时里 system prompt 只应有一种取值。"""
        with tempfile.TemporaryDirectory() as td:
            sp = self._make(td)
            seen = {sp.system_prompt(local_hour=h) for h in range(24)}
            self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()


class TestHourlyTimezoneGuard(unittest.TestCase):
    """🔴 2026-09-13 实测修复的静默 bug：hourly 分布的时区错位。

    `analyze_style` 原来用 `dt.hour` 统计（**UTC**），文件里也写着
    "Counts are UTC. The plugin should shift to its local TZ on load." ——
    但**下游从来没做这个转换**。于是插件在**本地 20:33** 读的是 UTC 20 点
    的预算（0.34，实为本地凌晨 4 点），而 active_interjection 每条消耗 1.0
    → **主动插话在本地 10:00–24:00 被完全压制**（群最活跃的时段）。
    @ 回复不受预算限制，所以一直没被发现。
    """

    def _mk(self, td, payload):
        with open(os.path.join(td, "my_hourly_distribution.json"), "w",
                  encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

    def test_legacy_utc_file_is_shifted(self):
        with tempfile.TemporaryDirectory() as td:
            # 旧格式：UTC 索引 + tz_note（无 tz 标记）
            self._mk(td, {
                "tz_note": "Counts are UTC. The plugin should shift to its local TZ on load.",
                "hourly_share": {"20": 0.0001, "12": 0.05},
                "hourly_budget": {"20": 0.34, "12": 122.80},
            })
            sp = StyleProfile(td)
            # UTC 20 → 本地 04；UTC 12 → 本地 20
            self.assertAlmostEqual(sp.hourly_budget(4), 0.34)
            self.assertAlmostEqual(sp.hourly_budget(20), 122.80)
            self.assertAlmostEqual(sp.hourly_budget(12), 0.0)

    def test_new_local_file_is_used_as_is(self):
        with tempfile.TemporaryDirectory() as td:
            self._mk(td, {
                "tz": "local",
                "tz_note": "Counts are LOCAL time (UTC+offset). Do not shift again.",
                "hourly_share": {"20": 0.05},
                "hourly_budget": {"20": 122.80},
            })
            sp = StyleProfile(td)
            self.assertAlmostEqual(sp.hourly_budget(20), 122.80)   # 不再平移
            self.assertAlmostEqual(sp.hourly_budget(4), 0.0)

    def test_shift_wraps_around_midnight(self):
        with tempfile.TemporaryDirectory() as td:
            self._mk(td, {
                "tz_note": "Counts are UTC.",
                "hourly_budget": {"20": 1.0, "23": 2.0, "0": 3.0},
            })
            sp = StyleProfile(td)
            # UTC 20→本地 4, UTC 23→本地 7, UTC 0→本地 8
            self.assertAlmostEqual(sp.hourly_budget(4), 1.0)
            self.assertAlmostEqual(sp.hourly_budget(7), 2.0)
            self.assertAlmostEqual(sp.hourly_budget(8), 3.0)

    def test_custom_offset_honoured(self):
        with tempfile.TemporaryDirectory() as td:
            self._mk(td, {
                "tz_note": "Counts are UTC.",
                "tz_offset_hours": 0,
                "hourly_budget": {"20": 1.0},
            })
            sp = StyleProfile(td)
            self.assertAlmostEqual(sp.hourly_budget(20), 1.0)   # 偏移 0 → 不平移

    def test_peak_hours_shifted_too(self):
        """peak_hours 也必须按本地时（否则两个消费方口径不一致）。"""
        with tempfile.TemporaryDirectory() as td:
            self._mk(td, {
                "tz_note": "Counts are UTC.",
                "hourly_share": {str(h): (0.1 if h == 12 else 0.001) for h in range(24)},
            })
            sp = StyleProfile(td)
            self.assertIn(20, sp.peak_hours())     # UTC 12 → 本地 20
            self.assertNotIn(12, sp.peak_hours())

    def test_missing_file_is_zero_not_crash(self):
        with tempfile.TemporaryDirectory() as td:
            sp = StyleProfile(td)
            self.assertEqual(sp.hourly_budget(12), 0.0)
            self.assertEqual(sp.peak_hours(), set())
