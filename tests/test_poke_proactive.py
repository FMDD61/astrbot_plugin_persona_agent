# -*- coding: utf-8 -*-
"""S7 主动戳人：严格名称解析 + 硬闸校验。

用户拍板（2026-09-14）：接口用**名字**不用 QQ 号；解析**严格**；
"模型用了未收录的昵称时戳不出去"可接受；同人冷却复用被动计时器；候选池仅 close。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.poke import PokeService  # noqa: E402
from services.style_profile import StyleProfile  # noqa: E402

MEMBERS = {
    "members": [
        {"uin": "1", "alias": "焦糖", "other_names": ["糖糖"], "closeness": "close"},
        {"uin": "2", "alias": "虾鱼丸", "other_names": ["虾虾", "私虾"], "closeness": "close"},
        {"uin": "3", "alias": "路人", "other_names": [], "closeness": "new"},
        # 歧义：两个成员共享同一个 other_name
        {"uin": "4", "alias": "智乃", "other_names": ["小智"], "closeness": "close"},
        {"uin": "5", "alias": "桔皮", "other_names": ["小智"], "closeness": "close"},
    ]
}


class TestStrictNameResolution(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        with open(os.path.join(self.td.name, "member_relations.json"), "w",
                  encoding="utf-8") as f:
            json.dump(MEMBERS, f, ensure_ascii=False)
        self.sp = StyleProfile(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_alias_exact(self):
        self.assertEqual(self.sp.resolve_member_name("焦糖"), ("1", "alias"))

    def test_other_name_exact(self):
        self.assertEqual(self.sp.resolve_member_name("虾虾"), ("2", "other_name"))

    def test_unknown_returns_none(self):
        uin, why = self.sp.resolve_member_name("完全不存在的昵称")
        self.assertIsNone(uin)
        self.assertEqual(why, "unknown_name")

    def test_ambiguous_is_rejected_not_guessed(self):
        """🔴 核心：歧义必须**拒绝**，不能像旧解析器那样静默取第一个。

        实测真实数据里 `智乃` 同时属于 `智乃` 与 `桔皮` ——
        旧 `resolve_uin_from_name` 会返回首个匹配 → **戳错人**（对外可见的社交事故）。
        """
        uin, why = self.sp.resolve_member_name("小智")
        self.assertIsNone(uin)
        self.assertIn("ambiguous_other_name", why)
        # 对照：旧解析器确实会猜（证明这个新方法的必要性）
        self.assertEqual(self.sp.resolve_uin_from_name("小智"), "4")

    def test_empty_name(self):
        self.assertIsNone(self.sp.resolve_member_name("")[0])
        self.assertIsNone(self.sp.resolve_member_name("   ")[0])

    def test_case_insensitive(self):
        assert self.sp.resolve_member_name("焦糖")[0] == "1"

    def test_closeness_lookup(self):
        self.assertEqual(self.sp.member_closeness("1"), "close")
        self.assertEqual(self.sp.member_closeness("3"), "new")
        self.assertEqual(self.sp.member_closeness("999"), "")


class TestProactiveDecision(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.p = PokeService(self.td.name, bot_qq="999", cooldown_sec=60.0,
                             hourly_cap=30, proactive_hourly_cap=5)
        self.p.proactive_enabled = True

    def tearDown(self):
        self.td.cleanup()

    def _d(self, **kw):
        kw.setdefault("target_uin", "1")
        kw.setdefault("closeness", "close")
        return self.p.decide_proactive(**kw)

    def test_disabled_is_silent(self):
        self.p.proactive_enabled = False
        ok, why = self._d()
        self.assertFalse(ok)
        self.assertEqual(why, "proactive_disabled")

    def test_no_target(self):
        self.assertEqual(self._d(target_uin=""), (False, "no_target"))

    def test_pool_restriction(self):
        ok, why = self._d(closeness="new")
        self.assertFalse(ok)
        self.assertIn("not_in_pool", why)
        self.assertFalse(self._d(closeness="")[0])

    def test_self_poke_forbidden(self):
        ok, why = self._d(target_uin="999", closeness="close")
        self.assertFalse(ok)
        self.assertEqual(why, "self_poke")

    def test_conflict_suppressed(self):
        """冲突中不戳 —— 与 Gate 的 conflict 判定联动。"""
        ok, why = self._d(conflict=True)
        self.assertFalse(ok)
        self.assertEqual(why, "conflict")

    def test_cooldown_shared_with_passive(self):
        """冷却与被动回戳**共用同一计时器**（用户指定）。

        刚被动回戳过的人，短期内不会被主动追着戳。
        """
        now = 1000.0
        self.p.record(now_utc=now, poker="1", group_id="g", responded=True, reason="ok")
        ok, why = self._d(now_utc=now + 10)
        self.assertFalse(ok)
        self.assertIn("cooldown", why)
        # 过了冷却就能戳
        self.assertTrue(self._d(now_utc=now + 61)[0])

    def test_proactive_hourly_cap_separate_from_passive(self):
        """主动配额与被动**分开计** —— 被动回戳不该挤占主动额度，反之亦然。"""
        now = 1000.0
        self.p._roll_hour(now)
        # 用满被动配额（30）但不影响主动
        for i in range(30):
            self.p.record(now_utc=now + i, poker=f"p{i}", group_id="g",
                          responded=True, reason="ok")
        self.assertEqual(self.p.snapshot()["hour_count"], 30)
        self.assertEqual(self.p.snapshot()["proactive_hour_count"], 0)
        ok, _ = self._d(now_utc=now + 100, target_uin="1")
        self.assertTrue(ok, "被动配额满了不应阻止主动戳")

    def test_proactive_cap_enforced(self):
        now = 1000.0
        # 先让小时键落到当前小时（否则首次 roll 会重置刚预载的计数）
        self.p._roll_hour(now)
        for i in range(5):
            self.p.record_proactive(now_utc=now + i, target_uin=f"t{i}", group_id="g",
                                    done=True, reason="ok")
        ok, why = self._d(now_utc=now + 100)
        self.assertFalse(ok)
        self.assertEqual(why, "proactive_hourly_cap")

    def test_hour_roll_resets_proactive(self):
        """跨小时重置主动配额。

        注意陷阱：`_d` 默认 target="1"，而 record_proactive 记的是 `t0..t4` ——
        所以必须用**被戳过的 target** 来验证，否则"通过"可能只是因为冷却计时
        刚好过期而非配额重置（写测试时踩到过）。
        """
        now = 1000.0
        self.p._roll_hour(now)
        for i in range(5):
            self.p.record_proactive(now_utc=now + i, target_uin=f"t{i}", group_id="g",
                                    done=True, reason="ok")
        # 同小时内：配额满（冷却也已过，所以不是冷却导致的拒绝）
        ok, why = self.p.decide_proactive(now_utc=now + 500, target_uin="t0",
                                          closeness="close")
        self.assertFalse(ok)
        self.assertEqual(why, "proactive_hourly_cap")
        # 跨到下一个本地小时 → 配额重置，且冷却早已过期
        later = now + 3600 * 3
        self.p._roll_hour(later)
        self.assertTrue(self.p.decide_proactive(now_utc=later, target_uin="t0",
                                                closeness="close")[0])

    def test_serious_context_suppressed(self):
        p = PokeService(self.td.name, bot_qq="999", serious_keywords=["吵架"])
        p.proactive_enabled = True
        ok, why = p.decide_proactive(target_uin="1", closeness="close",
                                     recent_text="你们别吵架了")
        self.assertFalse(ok)
        self.assertEqual(why, "serious_context")

    def test_failed_attempt_does_not_consume_quota(self):
        """被拒的尝试**不推进冷却、不占配额**（只有成功才记账）。"""
        now = 1000.0
        ok, why = self._d(now_utc=now, closeness="new")     # 被拒
        self.assertFalse(ok)
        self.assertEqual(self.p.snapshot()["proactive_hour_count"], 0)
        self.assertTrue(self._d(now_utc=now, target_uin="1")[0])

    def test_log_records_direction_and_name(self):
        """观测：记录 direction 与"模型想戳谁 vs 实际戳谁"，便于事后核对。"""
        self.p.record_proactive(now_utc=1000.0, target_uin="1", group_id="g",
                                done=False, reason="unknown_name",
                                raw_name="没收录的昵称", resolved_via="unknown_name")
        rows = [json.loads(l) for l in
                open(os.path.join(self.td.name, "poke_log.jsonl"), encoding="utf-8")
                if l.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["direction"], "proactive")
        self.assertEqual(rows[0]["raw_name"], "没收录的昵称")
        self.assertFalse(rows[0]["responded"])

    def test_passive_log_has_no_direction_break(self):
        """被动记录保持兼容（不强制带 direction 字段）。"""
        self.p.record(now_utc=1000.0, poker="7", group_id="g", responded=True, reason="ok")
        rows = [json.loads(l) for l in
                open(os.path.join(self.td.name, "poke_log.jsonl"), encoding="utf-8")
                if l.strip()]
        self.assertEqual(rows[0]["poker"], "7")
        self.assertEqual(rows[0]["target"], "999")


if __name__ == "__main__":
    unittest.main()
