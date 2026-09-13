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
                sm.append('grp', 'user', '早上好', name='小明')
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
            self.assertEqual(ctx[0]['name'], '小明')
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


class TestEmptyContentSelfHeal(unittest.TestCase):
    """B-002：会话里的空 content 条目曾让 LLM 请求 400 → 永久静默不回复。

    实测 2026-09-12：trace 显示 hard_gate=reply 但 raw_generation 为空，
    网关报 ``user message must have content``（param=messages.6.content）。
    唯一收口点必须在**进 LLM 之前**过滤，且不能改变正常会话的行为。
    """

    def test_get_contexts_filters_empty(self):
        sm = SessionManager(data_dir=None, max_messages=None)
        sm.append('g', 'user', '正常消息')
        sm.append('g', 'user', '')          # 脏数据
        sm.append('g', 'assistant', '   ')  # 纯空白也算空
        sm.append('g', 'user', '正常消息2')
        ctx = sm.get_contexts('g')
        self.assertEqual([m['content'] for m in ctx], ['正常消息', '正常消息2'])
        # 原始条目仍在（不销毁数据，只在外出口过滤）
        self.assertEqual(sm.size('g'), 4)
        self.assertEqual(sm.dropped_empty('g'), 2)

    def test_normal_session_unchanged(self):
        sm = SessionManager(data_dir=None, max_messages=None)
        for i in range(5):
            sm.append('g', 'user', f'm{i}')
            sm.append('g', 'assistant', f'r{i}')
        self.assertEqual(len(sm.get_contexts('g')), 10)
        self.assertEqual(sm.dropped_empty('g'), 0)

    def test_recent_shares_the_same_filter(self):
        # recent() 供 emotion/gate 用；若它看到空条目，两边口径就不一致了
        sm = SessionManager(data_dir=None, max_messages=None)
        sm.append('g', 'user', 'a')
        sm.append('g', 'user', '')
        sm.append('g', 'assistant', 'b')
        self.assertEqual([m['content'] for m in sm.recent('g', 10)], ['a', 'b'])

    def test_load_drops_empty_and_counts(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'session_g_2026-01-01.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({
                    'version': 2, 'group_id': 'g', 'day': '2026-01-01',
                    'saved_at': time.time(),
                    'messages': [
                        {'role': 'user', 'content': 'ok1'},
                        {'role': 'user', 'content': ''},
                        {'role': 'assistant', 'content': 'ok2'},
                    ],
                }, f, ensure_ascii=False)
            sm = SessionManager(data_dir=td, max_messages=None)
            sm.load_all()
            self.assertEqual([m['content'] for m in sm.get_contexts('g')], ['ok1', 'ok2'])
            self.assertEqual(sm.empty_dropped_on_load().get('g'), 1)


class TestQuoteSnapshotMeta(unittest.TestCase):
    """B-001：message_id 必须随条目入 session，且**绝不泄漏进 LLM 请求**。"""

    def test_internal_keys_never_reach_contexts(self):
        sm = SessionManager(data_dir=None, max_messages=None)
        sm.append('g', 'user', '你好', name='小明', message_id='m1', sender_uin='u1')
        ctx = sm.get_contexts('g')
        self.assertEqual(ctx, [{'role': 'user', 'content': '你好', 'name': '小明'}])
        self.assertNotIn('_mid', ctx[0])
        self.assertNotIn('_uin', ctx[0])

    def test_quote_snapshot_aligns_with_contexts(self):
        sm = SessionManager(data_dir=None, max_messages=None)
        sm.append('g', 'user', 'a', name='甲', message_id='m1', sender_uin='u1')
        sm.append('g', 'assistant', 'bot 回复')                  # 无 id：占位不可引用
        sm.append('g', 'user', 'b', name='乙', message_id='m2', sender_uin='u2')
        qi = sm.quote_snapshot('g')
        # 编号基长度 == 进 LLM 的上下文条数（B-001 次要成因：两者必须同源）
        self.assertEqual(len(qi), len(sm.get_contexts('g')))
        self.assertEqual(qi.resolve(1).message_id, 'm2')
        self.assertEqual(qi.resolve(1).alias, '乙')
        self.assertIsNone(qi.resolve(2))          # bot 自己的回复
        self.assertEqual(qi.resolve(3).message_id, 'm1')

    def test_quote_snapshot_filters_empty_like_contexts(self):
        # 空条目被 get_contexts 丢弃 → 编号基必须同样丢弃，否则整体错位
        sm = SessionManager(data_dir=None, max_messages=None)
        sm.append('g', 'user', 'a', message_id='m1')
        sm.append('g', 'user', '', message_id='m_bad')
        sm.append('g', 'user', 'b', message_id='m2')
        qi = sm.quote_snapshot('g')
        self.assertEqual(len(qi), len(sm.get_contexts('g')))
        self.assertEqual(qi.resolve(1).message_id, 'm2')
        self.assertEqual(qi.resolve(2).message_id, 'm1')

    def test_metadata_survives_rotation_archive(self):
        with tempfile.TemporaryDirectory() as td:
            sm = SessionManager(data_dir=td, max_messages=None, rotation_hour=2, tz_offset_hours=8)
            orig = time.time
            time.time = lambda: cst('2026-08-23 10:00')
            try:
                sm.append('g', 'user', 'a', message_id='m1', sender_uin='u1')
            finally:
                time.time = orig
            time.time = lambda: cst('2026-08-24 10:00')
            try:
                old = sm.rotate_if_day_changed('g')
            finally:
                time.time = orig
            self.assertTrue(old)
            # 归档条目保真（_mid 保留供离线复盘；diary 只读 content）
            self.assertEqual(old[0].get('_mid'), 'm1')

    def test_metadata_survives_restore(self):
        with tempfile.TemporaryDirectory() as td:
            sm = SessionManager(data_dir=td, max_messages=None)
            sm.append('g', 'user', 'a', message_id='m1', sender_uin='u1')
            sm._save('g')
            sm2 = SessionManager(data_dir=td, max_messages=None)
            sm2.load_all()
            qi = sm2.quote_snapshot('g')
            self.assertEqual(qi.resolve(1).message_id, 'm1')
            self.assertEqual(qi.resolve(1).sender_uin, 'u1')
            self.assertEqual(sm2.get_contexts('g')[0].get('_mid'), None)


if __name__ == "__main__":
    unittest.main()
