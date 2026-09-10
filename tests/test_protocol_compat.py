# -*- coding: utf-8 -*-
"""protocol_compat 归一化层测试（NapCat → LLBot v8 迁移，2026-09-10）。

覆盖两类知识：
  1. 入站 poke 形状变体（LLBot/NapCat 标准形、notice_type=poke 形、缺 notice_type 形、
     poke_recall、字段无效/缺失、以及必须被拒绝的非 poke 通知）
  2. 出站通道能力（LLBot/NapCat 无 poke 段 → action 优先）
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from services.protocol_compat import (  # noqa: E402
    CHANNEL_ACTION,
    CHANNEL_SEGMENT,
    LLBOT,
    NAPCAT,
    UNKNOWN,
    PokeNotice,
    capabilities_for,
    normalize_poke,
    poke_action_call,
    poke_channels,
)


class _FakeEvent:
    """模拟 aiocqhttp.Event：只提供 .get（非 dict 子类）的取值面。"""

    def __init__(self, payload):
        self._p = payload

    def get(self, key, default=None):
        return self._p.get(key, default)


class NormalizePokeStandardShapeTests(unittest.TestCase):
    """LLBot v8 / NapCat / OneBot v11 标准形：notice_type=notify, sub_type=poke。"""

    def _llbot_payload(self):
        # 实测同形：LLBot src/onebot11/event/notice/OB11PokeEvent.ts
        return {
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "poke",
            "user_id": 234567,
            "target_id": 123456,
            "group_id": 123456789,
            "raw_info": "{}",
        }

    def test_standard_shape_is_normalized(self):
        n = normalize_poke(self._llbot_payload())
        self.assertIsNotNone(n)
        self.assertEqual(n.poker, "234567")
        self.assertEqual(n.target, "123456")
        self.assertEqual(n.group_id, "123456789")
        self.assertTrue(n.is_group)
        self.assertFalse(n.is_recall)
        self.assertEqual(n.notice_type, "notify")
        self.assertEqual(n.sub_type, "poke")

    def test_string_ids_preserved(self):
        p = self._llbot_payload()
        p.update(user_id="234567", target_id="123456", group_id="123456789")
        n = normalize_poke(p)
        self.assertEqual((n.poker, n.target, n.group_id),
                         ("234567", "123456", "123456789"))

    def test_event_like_object_with_get_only(self):
        """aiocqhttp.Event 之外的 Mapping-like 对象也要能读。"""
        n = normalize_poke(_FakeEvent(self._llbot_payload()))
        self.assertIsNotNone(n)
        self.assertEqual(n.poker, "234567")

    def test_private_poke_has_empty_group(self):
        p = self._llbot_payload()
        p.pop("group_id")
        n = normalize_poke(p)
        self.assertIsNotNone(n)
        self.assertFalse(n.is_group)
        self.assertEqual(n.group_id, "")


class NormalizePokeVariantShapeTests(unittest.TestCase):
    """其它实现的变体形状。"""

    def test_notice_type_poke_without_sub_type(self):
        n = normalize_poke({"notice_type": "poke", "user_id": 1, "target_id": 2})
        self.assertIsNotNone(n)
        self.assertEqual(n.sub_type, "poke")  # 补默认值

    def test_sub_type_poke_without_notice_type(self):
        """AstrBot 自身判定 poke 组件时只看 sub_type + target_id，不看 notice_type。"""
        n = normalize_poke({"sub_type": "poke", "user_id": 1, "target_id": 2})
        self.assertIsNotNone(n)
        self.assertEqual(n.notice_type, "notify")  # 补默认值

    def test_poke_recall_is_flagged(self):
        n = normalize_poke({
            "notice_type": "notify", "sub_type": "poke_recall",
            "user_id": 1, "target_id": 2, "group_id": 3,
        })
        self.assertIsNotNone(n)
        self.assertTrue(n.is_recall)


class NormalizePokeRejectionTests(unittest.TestCase):
    """保守优先：认不出就不动作，宁可不错戳。"""

    def test_none_and_empty(self):
        self.assertIsNone(normalize_poke(None))
        self.assertIsNone(normalize_poke({}))

    def test_missing_target_id(self):
        self.assertIsNone(normalize_poke(
            {"notice_type": "notify", "sub_type": "poke", "user_id": 1}))

    def test_missing_user_id(self):
        self.assertIsNone(normalize_poke(
            {"notice_type": "notify", "sub_type": "poke", "target_id": 1}))

    def test_zero_ids_are_invalid(self):
        """LLBot OB11PokeEvent.target_id 默认 0 = 未设置，不可去戳 0 号。"""
        self.assertIsNone(normalize_poke(
            {"notice_type": "notify", "sub_type": "poke", "user_id": 1, "target_id": 0}))
        self.assertIsNone(normalize_poke(
            {"notice_type": "notify", "sub_type": "poke", "user_id": 0, "target_id": 1}))
        self.assertIsNone(normalize_poke(
            {"notice_type": "notify", "sub_type": "poke", "user_id": "1", "target_id": "0"}))

    def test_non_poke_notices_rejected(self):
        for payload in (
            {"notice_type": "group_recall", "sub_type": "normal", "user_id": 1, "target_id": 2},
            {"notice_type": "group_ban", "sub_type": "ban", "user_id": 1, "target_id": 2},
            {"notice_type": "group_admin", "sub_type": "set", "user_id": 1, "target_id": 2},
            {"notice_type": "notify", "sub_type": "title", "user_id": 1, "target_id": 2},
            {"notice_type": "group_increase", "sub_type": "approve", "user_id": 1, "target_id": 2},
        ):
            self.assertIsNone(normalize_poke(payload), payload)

    def test_conflicting_notice_type_rejected(self):
        """notice_type 非 notify/poke 时一律拒绝（防误判）。"""
        self.assertIsNone(normalize_poke(
            {"notice_type": "group_recall", "sub_type": "poke", "user_id": 1, "target_id": 2}))

    def test_boolean_ids_rejected(self):
        self.assertIsNone(normalize_poke(
            {"notice_type": "notify", "sub_type": "poke", "user_id": True, "target_id": 1}))


class PokeActionCallTests(unittest.TestCase):
    """出站通道：LLBot/NapCat 无 poke 段 → 必须走 action。"""

    def test_group_poke_action(self):
        n = PokeNotice(poker="234567", target="123456",
                       group_id="123456789", sub_type="poke", notice_type="notify")
        name, payload = poke_action_call(n)
        self.assertEqual(name, "group_poke")
        self.assertEqual(payload, {"group_id": 123456789, "user_id": 234567})
        self.assertIsInstance(payload["group_id"], int)

    def test_friend_poke_action(self):
        n = PokeNotice(poker="234567", target="123456",
                       group_id="", sub_type="poke", notice_type="notify")
        name, payload = poke_action_call(n)
        self.assertEqual(name, "friend_poke")
        self.assertEqual(payload, {"user_id": 234567})

    def test_non_numeric_uid_falls_back_to_str(self):
        n = PokeNotice(poker="u_abc", target="123456",
                       group_id="123456789", sub_type="poke", notice_type="notify")
        name, payload = poke_action_call(n)
        self.assertEqual(name, "group_poke")
        self.assertEqual(payload["user_id"], "u_abc")


class CapabilitiesTests(unittest.TestCase):
    def test_llbot_has_no_poke_segment(self):
        self.assertFalse(LLBOT.poke_message_segment)
        self.assertTrue(LLBOT.poke_action)

    def test_napcat_has_no_poke_segment(self):
        """NapCat 的 poke 段转换器是空实现桩，历史上同样无效。"""
        self.assertFalse(NAPCAT.poke_message_segment)
        self.assertTrue(NAPCAT.poke_action)

    def test_llbot_channels_are_action_first(self):
        self.assertEqual(poke_channels(LLBOT), (CHANNEL_ACTION,))

    def test_unknown_channels_include_segment_fallback(self):
        self.assertEqual(poke_channels(UNKNOWN), (CHANNEL_ACTION, CHANNEL_SEGMENT))

    def test_lookup_by_name(self):
        self.assertIs(capabilities_for("llbot"), LLBOT)
        self.assertIs(capabilities_for("LLBot"), LLBOT)   # 大小写不敏感
        self.assertIs(capabilities_for("napcat"), NAPCAT)
        self.assertIs(capabilities_for(""), UNKNOWN)
        self.assertIs(capabilities_for("something-else"), UNKNOWN)

    def test_channel_names_are_stable(self):
        """通道名是 main.py 的判别依据，改动即破坏契约。"""
        self.assertEqual(CHANNEL_ACTION, "action")
        self.assertEqual(CHANNEL_SEGMENT, "segment")


if __name__ == "__main__":
    unittest.main()
