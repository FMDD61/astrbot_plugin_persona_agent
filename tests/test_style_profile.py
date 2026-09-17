# -*- coding: utf-8 -*-
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.style_profile import StyleProfile, plan_relations_delta  # noqa: E402

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

    def test_tolerates_format_drift_in_frozen_block(self):
        """独立核验第三轮 1b：块里行尾/行首多空白时也必须认得出"已播报"。

        否则该行会被**再播报一次**（失败方向安全，但没必要）。
        """
        line = "  2: 乙  [认识]"
        for drifted in (line + "  ", "  " + line, "\t" + line + "\t"):
            self.assertEqual(
                StyleProfile.filter_already_announced([line], drifted), [],
                f"格式漂移未被容忍: {drifted!r}")

    def test_drops_blank_lines(self):
        self.assertEqual(StyleProfile.filter_already_announced(["", "   "], "x"), [])


class TestPlanRelationsDelta(unittest.TestCase):
    """B-032 收口后，增量决策下沉为纯函数 `plan_relations_delta`（可离线单测）。

    独立核验指出：此前这段决策埋在 main 里，而 main.py 不被任何测试导入 ——
    它只能靠 AST 抽取才验到。这里把四条出口全锁住。
    """

    @staticmethod
    def _sp(td, members):
        with open(os.path.join(td, "member_relations.json"), "w", encoding="utf-8") as f:
            json.dump({"members": members}, f, ensure_ascii=False)
        return StyleProfile(td)

    def test_first_run_registers_all_without_announcing(self):
        with tempfile.TemporaryDirectory() as td:
            sp = self._sp(td, [{"uin": "1", "alias": "甲", "closeness": "close"}])
            plan = plan_relations_delta(sp, {}, frozen_block="")
            self.assertTrue(plan.first_run)
            self.assertEqual(plan.text, "", "首次不得追加（初始块已在前缀里）")
            self.assertEqual(list(plan.known), ["1"])

    def test_new_member_is_announced(self):
        with tempfile.TemporaryDirectory() as td:
            sp = self._sp(td, [{"uin": "1", "alias": "甲", "closeness": "close"},
                               {"uin": "2", "alias": "乙", "closeness": "new"}])
            plan = plan_relations_delta(sp, {"1": sp.relations_lines()[0][1]},
                                        frozen_block="")
            self.assertFalse(plan.first_run)
            self.assertIn("新加入或新认识的群友", plan.text)
            self.assertIn("乙", plan.text)
            self.assertEqual(plan.added, 1)

    def test_change_is_announced(self):
        with tempfile.TemporaryDirectory() as td:
            sp = self._sp(td, [{"uin": "1", "alias": "甲", "closeness": "close"}])
            plan = plan_relations_delta(sp, {"1": "  1: 甲  [认识]"}, frozen_block="")
            self.assertIn("关系/称呼有变化", plan.text)
            self.assertEqual(plan.changed, 1)

    def test_frozen_block_absorbs_change_without_announcing(self):
        """🔴 B-032：头部冻结块里已写着的行不再往尾部讲一遍（但仍推进 known）。"""
        with tempfile.TemporaryDirectory() as td:
            sp = self._sp(td, [{"uin": "1", "alias": "甲", "closeness": "close"},
                               {"uin": "2", "alias": "乙", "closeness": "new"}])
            cur_lines = dict(sp.relations_lines())
            plan = plan_relations_delta(sp, {"1": cur_lines["1"]},
                                        frozen_block="\n".join(cur_lines.values()))
            self.assertEqual(plan.text, "", "头部已含乙 → 不得重复播报（B-032）")
            self.assertEqual(plan.known, cur_lines, "known 仍要推进到当前")
            self.assertFalse(plan.first_run)

    def test_state_write_decisions_are_locked(self):
        """独立核验第三轮：main 的接线（是否落盘 / 是否先对齐）此前无任何测试。

        现在这些判断都在 plan 里，四条出口的 state 形状逐条锁住。
        """
        with tempfile.TemporaryDirectory() as td:
            sp = self._sp(td, [{"uin": "1", "alias": "甲", "closeness": "close"}])
            cur = dict(sp.relations_lines())
            # ① 首次：要落盘 initialized_at，且必须先对齐头部
            p1 = plan_relations_delta(sp, {}, now=1000.0)
            self.assertEqual(sorted(p1.state), ["initialized_at", "known"])
            self.assertTrue(p1.need_align)
            # ② 完全无差异：不落盘（state=None），也不需要对齐
            p2 = plan_relations_delta(sp, cur, frozen_block="", now=1000.0)
            self.assertIsNone(p2.state, "无差异时不得写盘")
            self.assertFalse(p2.need_align)
            self.assertEqual(p2.known, {},
                             "state=None 时 known 返回空 dict —— 它表示「无落盘需求」，"
                             "不是「当前 known」（独立核验第四轮的 footgun 提示）")
            # ③ 被冻结块吸收：要落盘（推 known）但不需要对齐
            p3 = plan_relations_delta(sp, {"1": "  1: 甲  [认识]"},
                                      frozen_block=cur["1"], now=1000.0)
            self.assertEqual(p3.text, "")
            self.assertIsNotNone(p3.state, "头部已讲过也要推进 known（否则每轮重算）")
            self.assertTrue(p3.state.get("aligned_with_frozen"))
            self.assertFalse(p3.need_align)
            # ④ 真变化：落盘带 added/changed 计数
            p4 = plan_relations_delta(sp, {"1": "  1: 甲  [认识]"}, now=1000.0)
            self.assertIn("关系/称呼有变化", p4.text)
            self.assertEqual((p4.state["added"], p4.state["changed"]), (0, 1))
            self.assertEqual(p4.state["updated_at"], 1000.0, "now 应可注入（确定性）")

    def test_alias_embedding_another_line_is_not_swallowed(self):
        """🔴 独立核验给的唯一误杀构造：某个成员的别名里**字面内嵌**另一成员的整行。

        子串判据会把成员 2 的真实新行当成"已播报"丢掉；**整行集合**判据不会。
        用例用旧判据会红、用新判据会绿 —— 这条就是那次改进的守卫。
        """
        with tempfile.TemporaryDirectory() as td:
            # 先建"只有成员 2"的表，拿到它的**真实行文本**（含中文亲疏标签）
            line2 = dict(self._sp(td, [
                {"uin": "2", "alias": "乙", "closeness": "known"},
            ]).relations_lines())["2"]
            # 再把成员 1 的别名设成"字面内嵌 line2"
            sp = self._sp(td, [
                {"uin": "1", "alias": "甲" + line2, "closeness": "close"},
                {"uin": "2", "alias": "乙", "closeness": "known"},
            ])
            cur = dict(sp.relations_lines())
            self.assertIn(line2, cur["1"], "构造前提：成员 1 的行里内嵌成员 2 的整行")
            # frozen = 成员 1 的行；known 只有成员 1 → 成员 2 属"新增"
            plan = plan_relations_delta(sp, {"1": cur["1"]}, frozen_block=cur["1"])
            self.assertIn(cur["2"], plan.text,
                          "成员 2 的真实新行被误判成已播报（子串判据的坑）")



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
