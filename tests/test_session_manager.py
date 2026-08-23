# -*- coding: utf-8 -*-
import datetime
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.session_manager import SessionManager

TZ8 = datetime.timezone(datetime.timedelta(hours=8))


def cst(iso: str) -> float:
    return datetime.datetime.strptime(iso, "%Y-%m-%d %H:%M").replace(tzinfo=TZ8).timestamp()


class TestDayKey(unittest.TestCase):
    def setUp(self):
        self.sm = SessionManager(data_dir=None, max_messages=None, rotation_hour=2, tz_offset_hours=8)

    def test_rollover_at_0200(self):
        self.assertEqual(self.sm.day_key(cst("2026-08-23 01:59")), "2026-08-22")
        self.assertEqual(self.sm.day_key(cst("2026-08-23 02:00")), "2026-08-23")
        self.assertEqual(self.sm.day_key(cst("2026-08-23 23:59")), "2026-08-23")


class TestRotation(unittest.TestCase):
    def test_rotate_archives_and_restores(self):
        with tempfile.TemporaryDirectory() as td:
            sm = SessionManager(data_dir=td, max_messages=None, rotation_hour=2, tz_offset_hours=8)
            orig = time.time
            time.time = lambda: cst("2026-08-23 10:00")  # pin append day deterministically
            try:
                sm.append('grp', 'user', '早上好', name='焦糖')
                sm.append('grp', 'assistant', '枣商蚝~')
            finally:
                time.time = orig
            time.time = lambda: cst("2026-08-24 03:00")
            try:
                old = sm.rotate_if_day_changed('grp')
            finally:
                time.time = orig
            self.assertEqual(len(old), 2)
            self.assertEqual(sm.size('grp'), 0)
            files = os.listdir(td)
            self.assertIn('session_grp_2026-08-23.json', files)
            sm2 = SessionManager(data_dir=td, max_messages=None, rotation_hour=2, tz_offset_hours=8)
            restored = sm2.load_all()
            self.assertEqual(restored.get('grp'), 2)
            ctx = sm2.get_contexts('grp')
            self.assertEqual(ctx[0]['name'], '焦糖')
            self.assertEqual(ctx[0]['content'], '早上好')

    def test_recent_files_only_restored(self):
        with tempfile.TemporaryDirectory() as td:
            for day in ('2026-08-20', '2026-08-22'):
                payload = {"version": 2, "group_id": "g", "day": day,
                           "messages": [{"role": "user", "content": f"m-{day}"}]}
                with open(os.path.join(td, f"session_g_{day}.json"), 'w', encoding='utf-8') as f:
                    json.dump(payload, f)
            sm = SessionManager(data_dir=td, max_messages=None, rotation_hour=2, tz_offset_hours=8)
            restored = sm.load_all()
            ctx = sm.get_contexts('g')
            self.assertEqual(ctx[0]['content'], 'm-2026-08-22')

    def test_legacy_v1_file(self):
        with tempfile.TemporaryDirectory() as td:
            payload = {"version": 1, "group_id": "g",
                       "messages": [{"role": "user", "content": "旧格式"}]}
            with open(os.path.join(td, "session_g.json"), 'w', encoding='utf-8') as f:
                json.dump(payload, f)
            sm = SessionManager(data_dir=td, max_messages=None, rotation_hour=2, tz_offset_hours=8)
            restored = sm.load_all()
            self.assertEqual(restored.get('g'), 1)
            self.assertEqual(sm.get_contexts('g')[0]['content'], '旧格式')

    def test_unbounded(self):
        with tempfile.TemporaryDirectory() as td:
            sm = SessionManager(data_dir=td, max_messages=None)
            for i in range(350):
                sm.append('g', 'user', f'm{i}')
            self.assertEqual(sm.size('g'), 350)

    def test_clear_removes_day_files(self):
        with tempfile.TemporaryDirectory() as td:
            sm = SessionManager(data_dir=td, max_messages=None, rotation_hour=2, tz_offset_hours=8)
            sm.append('g', 'user', 'x')
            sm._save('g')
            self.assertTrue(os.listdir(td))
            sm.clear('g')
            self.assertEqual(os.listdir(td), [])


if __name__ == "__main__":
    unittest.main()
