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


if __name__ == "__main__":
    unittest.main()
