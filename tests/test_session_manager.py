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
            # S15: 发言人已固化进 content 前缀；name 与内部键都不在对外出口
            self.assertIn('小明', str(ctx[0].get('content') or ''))
            self.assertNotIn('name', ctx[0], "对外出口应剥掉 name（避免重复标识）")
            self.assertIn('早上好', str(ctx[0].get('content') or ''))

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


class ArchivePayloadTests(unittest.TestCase):
    """B-025（2026-09-17 验证）：轮转归档必须带上会话状态。

    `_save()` 写 8 键（含 S14 新增的 system_prompt / sys_blocks / next_block_id），
    而 `rotate_if_day_changed()` 只写 5 键 → 归档日**人格整条消失**：
    实测同一导出工具，归档文件命中人格标记 0 次、在写文件 1 次。
    可变块标记（`__sys_block__`）也会因缺 `sys_blocks` 而还原不出内容。
    """

    def _pin(self, iso):
        orig = time.time
        time.time = lambda: cst(iso)
        return orig

    def test_rotation_archive_keeps_session_state(self):
        with tempfile.TemporaryDirectory() as td:
            sm = SessionManager(data_dir=td, max_messages=None,
                                rotation_hour=2, tz_offset_hours=8)
            prompt = "你是这个 QQ 群里的一名活跃群员，模仿某群友的风格说话。"
            orig = self._pin("2026-08-23 10:00")
            try:
                sm.ensure_system_prompt('grp', prompt)
                sm.append_system_update('grp', "［设定更新］以此为准：新口癖")
                sm.append('grp', 'user', '早上好', name='小明')
            finally:
                time.time = orig
            orig = self._pin("2026-08-24 03:00")
            try:
                old = sm.rotate_if_day_changed('grp')
            finally:
                time.time = orig
            self.assertEqual(len(old), 2)
            path = os.path.join(td, 'session_grp_2026-08-23.json')
            payload = json.load(open(path, encoding='utf-8'))
            self.assertEqual(payload.get("system_prompt"), prompt,
                             "归档缺 system_prompt → 导出历史日人格消失（B-025）")
            self.assertTrue(payload.get("sys_blocks"),
                            "归档缺 sys_blocks → 可变块内容还原不出（B-025）")
            self.assertIn("next_block_id", payload)

    def test_rotation_archive_round_trips_through_load_all(self):
        """归档 → 重新 load_all 后，可变块仍能还原成 system 消息。"""
        with tempfile.TemporaryDirectory() as td:
            sm = SessionManager(data_dir=td, max_messages=None,
                                rotation_hour=2, tz_offset_hours=8)
            orig = self._pin("2026-08-23 10:00")
            try:
                sm.ensure_system_prompt('grp', "人格正文")
                sm.append_system_update('grp', "［设定更新］以此为准：块正文")
                sm.append('grp', 'user', '历史')
            finally:
                time.time = orig
            orig = self._pin("2026-08-24 03:00")
            try:
                sm.rotate_if_day_changed('grp')
            finally:
                time.time = orig
            # 归档文件是"那一天"的内容；load_all 取最新 day 文件 = 归档日
            sm2 = SessionManager(data_dir=td, max_messages=None,
                                 rotation_hour=2, tz_offset_hours=8)
            sm2.load_all()
            ctx = [str(m.get("content") or "") for m in sm2.get_contexts('grp')]
            self.assertIn("人格正文", ctx, "恢复后 system prompt 丢失（B-025）")
            self.assertTrue(any("块正文" in c for c in ctx),
                            "恢复后可变块内容丢失（B-025）")


class RelationsBlockFreezeTests(unittest.TestCase):
    """B-022（2026-09-17 验证）：关系图谱头部块必须**冻结**。

    生产实测 13 次「图谱块变更」把整段会话前缀（4–9 万 token）打成全价，
    占全部全价 token 的 32.6% —— 因为 `_assemble_base` 每轮现算活块，
    而 S10 的"旧块留在前缀里不动"只做到了"往尾部追加增量"。
    """

    def test_freeze_returns_first_content(self):
        sm = SessionManager(data_dir=None, max_messages=None)
        first = sm.freeze_relations_block("g1", "【关系图谱】v1")
        second = sm.freeze_relations_block("g1", "【关系图谱】v2 新成员")
        self.assertEqual(first, "【关系图谱】v1")
        self.assertEqual(second, "【关系图谱】v1",
                         "冻结后不得再返回新内容（否则前缀失效 → B-022）")

    def test_freeze_with_empty_content_is_noop(self):
        sm = SessionManager(data_dir=None, max_messages=None)
        self.assertEqual(sm.freeze_relations_block("g1", ""), "")
        self.assertEqual(sm.freeze_relations_block("g1", "【关系图谱】v1"), "【关系图谱】v1")

    def test_frozen_block_survives_restart(self):
        with tempfile.TemporaryDirectory() as td:
            sm = SessionManager(data_dir=td, max_messages=None,
                                rotation_hour=2, tz_offset_hours=8)
            sm.freeze_relations_block("g1", "【关系图谱】v1")
            sm.append("g1", "user", "历史")
            sm._save("g1")
            sm2 = SessionManager(data_dir=td, max_messages=None,
                                 rotation_hour=2, tz_offset_hours=8)
            sm2.load_all()
            self.assertEqual(sm2.freeze_relations_block("g1", "【关系图谱】v2"),
                             "【关系图谱】v1", "重启后冻结块丢失 → 前缀失效（B-022）")

    def test_frozen_getter_and_force_refreeze(self):
        """B-032：force=True 用于修复态（状态被删时让头部与 known 对齐）。"""
        sm = SessionManager(data_dir=None, max_messages=None)
        self.assertEqual(sm.frozen_relations_block("g1"), "", "无会话时返回空串")
        self.assertEqual(sm.freeze_relations_block("g1", "v1"), "v1")
        self.assertEqual(sm.frozen_relations_block("g1"), "v1")
        self.assertEqual(sm.freeze_relations_block("g1", "v2"), "v1",
                         "默认不覆盖（冻结语义）")
        self.assertEqual(sm.freeze_relations_block("g1", "v2", force=True), "v2",
                         "force=True 必须覆盖（修复态）")
        self.assertEqual(sm.frozen_relations_block("g1"), "v2")

    def test_load_all_tolerates_dirty_block_keys(self):
        """B-031 之后 sys_blocks 非空成为常态 → 脏 key 不得让 load_all 整体抛错。

        独立核验回归风险 3：`int(k)` 与 `int(next_block_id)` 不在 per-file try 内，
        一份写坏的 session 文件（非数字 key）会让**整个**恢复流程失败。
        """
        with tempfile.TemporaryDirectory() as td:
            payload = {"version": 2, "group_id": "g", "day": "2026-09-17",
                       "messages": [{"role": "user", "content": "你好"},
                                    {"role": "system", "__sys_block__": 0}],
                       "system_prompt": "人格",
                       "sys_blocks": {"0": "［设定更新］以此为准：块正文",
                                      "oops": "脏 key", "": "空 key"},
                       "next_block_id": "not-a-number",
                       "relations_block": "【图谱】"}
            with open(os.path.join(td, "session_g_2026-09-17.json"), "w",
                      encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            sm = SessionManager(data_dir=td, max_messages=None,
                                rotation_hour=2, tz_offset_hours=8)
            restored = sm.load_all()          # 修复前：ValueError 直接冒出去
            self.assertEqual(restored.get("g"), 2)
            ctx = [str(m.get("content") or "") for m in sm.get_contexts("g")]
            self.assertIn("人格", ctx)
            self.assertTrue(any("块正文" in c for c in ctx), "合法块必须仍能还原")
            self.assertEqual(sm.frozen_relations_block("g"), "【图谱】")

    def test_rotation_resets_frozen_block(self):
        with tempfile.TemporaryDirectory() as td:
            sm = SessionManager(data_dir=td, max_messages=None,
                                rotation_hour=2, tz_offset_hours=8)
            orig = time.time
            time.time = lambda: cst("2026-08-23 10:00")
            try:
                sm.freeze_relations_block("g1", "【关系图谱】v1")
                sm.append("g1", "user", "历史")
            finally:
                time.time = orig
            time.time = lambda: cst("2026-08-24 03:00")
            try:
                sm.rotate_if_day_changed("g1")
            finally:
                time.time = orig
            self.assertEqual(sm.freeze_relations_block("g1", "【关系图谱】v2"),
                             "【关系图谱】v2", "轮转 = 新会话，冻结块应重置")


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
        # S15: content 带发言前缀；name 与内部键都不出现在出口
        self.assertEqual(ctx, [{'role': 'user', 'content': '小明：你好'}])
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

    def test_quote_snapshot_aligns_when_system_block_present(self):
        """🔴 B-026（2026-09-17 验证）：可变块（［设定更新］）也必须占一个编号位。

        块会被 `get_contexts()` 物化成一条 system 消息（LLM 看得见），
        而 `quote_entries()` 此前把它跳过 → 编号基比 LLM 所见**少一条** →
        `[r:-N]`（N≥2）整体偏移一位 → **静默引用错人**。
        生产上没炸只是因为当前的关系增量是以普通 system 条目追加的；
        一旦走 `append_system_update`（人格提示词变更就会走），偏移立刻生效。
        """
        sm = SessionManager(data_dir=None, max_messages=None)
        sm.ensure_system_prompt('g', '人格')       # 首条 system 单独传参，不占编号
        sm.append('g', 'user', 'a', name='甲', message_id='m1', sender_uin='u1')
        sm.append_system_update('g', '［设定更新］以此为准：新设定')
        sm.append('g', 'user', 'b', name='乙', message_id='m2', sender_uin='u2')
        ctx = sm.get_contexts('g')
        qi = sm.quote_snapshot('g')
        self.assertEqual(len(qi), len(ctx) - 1,
                         "编号基必须与 LLM 所见逐条对齐（块也要占位）—— B-026")
        self.assertEqual(qi.resolve(1).message_id, 'm2')
        self.assertIsNone(qi.resolve(2))           # 块占位：不可引用（既有设计）
        self.assertEqual(qi.resolve(3).message_id, 'm1',
                         "块之后的老消息编号不得塌陷（否则静默引用错人）")

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
