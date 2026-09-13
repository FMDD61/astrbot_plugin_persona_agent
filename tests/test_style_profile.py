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
