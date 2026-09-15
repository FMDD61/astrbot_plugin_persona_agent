# -*- coding: utf-8 -*-
"""熟悉度体系（S12）：只升不降 / 提案解析 / 批准决策 / 序号解析。

用户定稿规则（2026-09-14/15）：
- **只升不降**；新成员入列自动（new），其余升级必须人工批准
- 判断依据 = **日记** + 必须附**旧版关系图谱**（否则会提议已经是 close 的人）
- 提案**只在周报任务内产出**、生命周期一周、**新批覆盖旧批**（不拼接）
  ⇒ 单批内序号稳定，**不需要"序号→稳定 id"翻译层**
- 人工可绕过所有限制（直接改 member_relations.json，mtime 热重载）
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.familiarity import (  # noqa: E402
    CLOSENESS_ORDER, Proposal, ProposalBatch, ProposalStore, build_proposal_prompt,
    closeness_rank, decide, is_upgrade, parse_indices, parse_proposals,
)


class TestOnlyUpgrade(unittest.TestCase):
    def test_rank_order(self):
        self.assertLess(closeness_rank("new"), closeness_rank("known"))
        self.assertLess(closeness_rank("known"), closeness_rank("close"))

    def test_unknown_rank_is_lowest(self):
        """未知等级当最低档 —— 保守方向：不会被误判为"可升"。"""
        self.assertLess(closeness_rank("bogus"), closeness_rank("new"))
        self.assertLess(closeness_rank(""), closeness_rank("new"))

    def test_is_upgrade(self):
        self.assertTrue(is_upgrade("new", "known"))
        self.assertTrue(is_upgrade("new", "close"))
        self.assertTrue(is_upgrade("known", "close"))
        self.assertFalse(is_upgrade("close", "known"), "降级必须为假")
        self.assertFalse(is_upgrade("known", "new"), "降级必须为假")
        self.assertFalse(is_upgrade("known", "known"), "同级必须为假")

    def test_no_downgrade_in_order_table(self):
        """等级表里只有三档，且序严格递增（防止以后误加负序值）。"""
        self.assertEqual(sorted(CLOSENESS_ORDER.values()), [0, 1, 2])


class TestParseProposals(unittest.TestCase):
    CUR = {"1": "new", "2": "known", "3": "close"}

    def _p(self, obj):
        return parse_proposals(json.dumps(obj, ensure_ascii=False), self.CUR)

    def test_keeps_only_real_upgrades(self):
        got = self._p({"proposals": [
            {"uin": "1", "to": "known", "reason": "a"},      # ✅
            {"uin": "2", "to": "close", "reason": "b"},      # ✅
            {"uin": "3", "to": "close", "reason": "c"},      # ❌ 非升级
        ]})
        self.assertEqual([(p.uin, p.from_closeness, p.to_closeness) for p in got],
                         [("1", "new", "known"), ("2", "known", "close")])

    def test_drops_downgrade(self):
        got = self._p({"proposals": [{"uin": "2", "to": "new", "reason": "x"}]})
        self.assertEqual(got, [], "降级提议必须被丢弃")

    def test_drops_hallucinated_uin(self):
        got = self._p({"proposals": [{"uin": "99999", "to": "close", "reason": "x"}]})
        self.assertEqual(got, [], "不在成员表里的 uin 必须被丢弃")

    def test_drops_unknown_target_level(self):
        got = self._p({"proposals": [{"uin": "1", "to": "bestie", "reason": "x"}]})
        self.assertEqual(got, [])

    def test_from_is_taken_from_current_not_llm(self):
        """`from` 必须用**服务端真实当前值**，不采信模型自报。"""
        got = self._p({"proposals": [{"uin": "1", "from": "close", "to": "known",
                                      "reason": "x"}]})
        # 模型自称 from=close（降级），但真实是 new → 这是升级，应保留
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].from_closeness, "new")

    def test_indexes_are_one_based_and_sequential(self):
        got = self._p({"proposals": [
            {"uin": "1", "to": "known"}, {"uin": "2", "to": "close"}]})
        self.assertEqual([p.index for p in got], [1, 2])

    def test_tolerates_fence_and_chatter(self):
        raw = '```json\n{"proposals": [{"uin": "1", "to": "known"}]}\n```'
        self.assertEqual(len(parse_proposals(raw, self.CUR)), 1)
        raw2 = '好的：{"proposals": [{"uin": "1", "to": "known"}]} 以上'
        self.assertEqual(len(parse_proposals(raw2, self.CUR)), 1)

    def test_garbage_returns_empty_not_guess(self):
        for raw in ("", "   ", "不是 JSON", "{}", '{"proposals": "x"}',
                    '{"proposals": [1, 2]}', None):
            self.assertEqual(parse_proposals(raw, self.CUR), [],
                             f"垃圾输入必须返回空表: {raw!r}")

    def test_reason_truncated(self):
        got = self._p({"proposals": [{"uin": "1", "to": "known",
                                      "reason": "很" * 200}]})
        self.assertLessEqual(len(got[0].reason), 80)


class TestPromptIncludesCurrentGraph(unittest.TestCase):
    def test_prompt_has_relations_block(self):
        """🔴 必须附**旧版关系图谱** —— 否则模型会提议已经是 close 的人。"""
        p = build_proposal_prompt("【熟人】\\n  1: 甲 [熟人]", [{"day": "d", "summary": "s"}],
                                  {"1": "close"})
        self.assertIn("旧版群友关系图谱", p)
        self.assertIn("甲", p)
        self.assertIn("本周日记", p)

    def test_prompt_handles_empty_graph(self):
        p = build_proposal_prompt("", [{"day": "d", "summary": "s"}], {})
        self.assertIn("（空）", p)


class TestParseIndices(unittest.TestCase):
    """用户确认：支持多个空格分隔序号；**不支持区间**（少一种语法少一种误用）。"""

    def test_single(self):
        self.assertEqual(parse_indices("1", 3), ([1], ""))

    def test_multiple(self):
        self.assertEqual(parse_indices("1 3", 3), ([1, 3], ""))

    def test_duplicates_deduped_silently(self):
        self.assertEqual(parse_indices("1 1 2", 3), ([1, 2], ""))

    def test_range_syntax_rejected(self):
        """区间不是"非法"，而是**未支持** —— 报错要说清是哪个 token 有问题。"""
        idx, err = parse_indices("1-3", 5)
        self.assertEqual(idx, [])
        self.assertIn("1-3", err)

    def test_non_numeric_names_the_token(self):
        idx, err = parse_indices("1 abc", 5)
        self.assertEqual(idx, [])
        self.assertIn("abc", err)

    def test_out_of_range_reports_bounds(self):
        for tok in ("0", "4", "99", "-1"):
            idx, err = parse_indices(tok, 3)
            self.assertEqual(idx, [], f"{tok} 应被拒")
            self.assertIn("超出范围", err)

    def test_empty_gives_usage(self):
        idx, err = parse_indices("", 3)
        self.assertEqual(idx, [])
        self.assertIn("apply", err)

    def test_whitespace_tolerated(self):
        self.assertEqual(parse_indices("  1   2  ", 3), ([1, 2], ""))


class TestDecide(unittest.TestCase):
    def _batch(self, *specs):
        b = ProposalBatch(period="2026-W37")
        for i, (uin, f, t, st) in enumerate(specs, 1):
            b.proposals.append(Proposal(index=i, uin=uin, alias=f"人{uin}",
                                        from_closeness=f, to_closeness=t, status=st))
        return b

    def test_approve_marks_and_reports(self):
        b = self._batch(("1", "new", "known", "pending"))
        ok, msg, p = decide(b, 1, "approve", {"1": "new"})
        self.assertTrue(ok)
        self.assertEqual(p.status, "approved")
        self.assertTrue(p.decided_at)

    def test_reject_marks(self):
        b = self._batch(("1", "new", "known", "pending"))
        ok, _, p = decide(b, 1, "reject", {"1": "new"})
        self.assertTrue(ok)
        self.assertEqual(p.status, "rejected")

    def test_idempotent_reapprove(self):
        """重复批准 → 成功且明确说"未重复执行"（用户重复输入是常态，不该报错）。"""
        b = self._batch(("1", "new", "known", "approved"))
        ok, msg, _ = decide(b, 1, "approve", {"1": "known"})
        self.assertTrue(ok)
        self.assertIn("未重复执行", msg)

    def test_cannot_flip_decided(self):
        b = self._batch(("1", "new", "known", "approved"))
        ok, msg, _ = decide(b, 1, "reject", {"1": "known"})
        self.assertFalse(ok)
        self.assertIn("已", msg)

    def test_out_of_range(self):
        b = self._batch(("1", "new", "known", "pending"))
        for n in (0, 2, 99):
            ok, msg, _ = decide(b, n, "approve", {"1": "new"})
            self.assertFalse(ok)
            self.assertIn("超出范围", msg)

    def test_stale_proposal_no_longer_an_upgrade(self):
        """🔴 二次校验：提案生成后、批准前，人工把等级改了 → 不得降级。

        例如提案是 new→known，但人工已把他升到 close：
        此时 known 不再是升级，必须拒绝而不是"降回 known"。
        """
        b = self._batch(("1", "new", "known", "pending"))
        ok, msg, p = decide(b, 1, "approve", {"1": "close"})
        self.assertFalse(ok)
        self.assertIn("不是升级", msg)
        self.assertEqual(p.status, "pending", "拒绝后状态不应改变")

    def test_already_at_target_is_reported_as_noop_success(self):
        """人工已经升过了 → 视作成功（幂等），避免"看起来失败其实已达成"。"""
        b = self._batch(("1", "new", "known", "pending"))
        ok, msg, p = decide(b, 1, "approve", {"1": "known"})
        self.assertTrue(ok)
        self.assertIn("无需重复提升", msg)
        self.assertEqual(p.status, "approved")

    def test_member_removed(self):
        b = self._batch(("1", "new", "known", "pending"))
        ok, msg, _ = decide(b, 1, "approve", {})
        self.assertFalse(ok)
        self.assertIn("不在成员表", msg)

    def test_unknown_action(self):
        b = self._batch(("1", "new", "known", "pending"))
        ok, msg, _ = decide(b, 1, "delete", {"1": "new"})
        self.assertFalse(ok)


class TestProposalStore(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.td.cleanup()

    def test_roundtrip(self):
        st = ProposalStore(self.td.name)
        st.replace([Proposal(index=1, uin="1", alias="甲", from_closeness="new",
                             to_closeness="known", reason="r")], period="2026-W37")
        b = st.load()
        self.assertEqual(b.period, "2026-W37")
        self.assertEqual(len(b.proposals), 1)
        self.assertEqual(b.proposals[0].to_closeness, "known")

    def test_replace_overwrites_not_appends(self):
        """用户明确：**新批覆盖旧批，不拼接**（否则序号会变，需翻译层）。"""
        st = ProposalStore(self.td.name)
        st.replace([Proposal(index=1, uin="1", to_closeness="known")], period="W1")
        st.replace([Proposal(index=1, uin="2", to_closeness="close")], period="W2")
        b = st.load()
        self.assertEqual(len(b.proposals), 1)
        self.assertEqual(b.proposals[0].uin, "2")
        self.assertEqual(b.period, "W2")

    def test_missing_file_is_empty_batch(self):
        b = ProposalStore(self.td.name).load()
        self.assertEqual(b.proposals, [])

    def test_corrupt_file_is_empty_batch(self):
        Path(self.td.name, "familiarity_proposals.json").write_text("{坏",
                                                                   encoding="utf-8")
        self.assertEqual(ProposalStore(self.td.name).load().proposals, [])


class TestBotMemberDetection(unittest.TestCase):
    """`notes` 让给"描述"后，bot 标记迁到 `kind` —— 必须**双字段兼容**。

    成员表里有 2 个 bot 账号混在真人中间，漏判会让它们被当成真人暴露给 LLM。
    """

    def _sp(self, td, members):
        with open(os.path.join(td, "member_relations.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"members": members}, f, ensure_ascii=False)
        from services.style_profile import StyleProfile
        return StyleProfile(td)

    def test_all_three_forms_detected(self):
        with tempfile.TemporaryDirectory() as td:
            sp = self._sp(td, [
                {"uin": "1", "alias": "真人", "closeness": "close"},
                {"uin": "2", "alias": "旧标记", "notes": "bot"},
                {"uin": "3", "alias": "新标记", "kind": "bot"},
                {"uin": "4", "alias": "双写", "notes": "bot", "kind": "bot"},
            ])
            for uin in ("2", "3", "4"):
                m = next(x for x in sp._iter_members() if x["uin"] == uin)
                self.assertTrue(sp.is_bot_member(m), f"uin={uin} 应判定为 bot")
            real = next(x for x in sp._iter_members() if x["uin"] == "1")
            self.assertFalse(sp.is_bot_member(real))

    def test_bots_excluded_from_relations_block(self):
        with tempfile.TemporaryDirectory() as td:
            sp = self._sp(td, [
                {"uin": "1", "alias": "真人", "closeness": "close"},
                {"uin": "2", "alias": "星野", "notes": "bot"},
                {"uin": "3", "alias": "苗爷", "kind": "bot"},
            ])
            blk = sp.relations_block()
            self.assertIn("真人", blk)
            self.assertNotIn("星野", blk, "bot 账号不得进关系图谱")
            self.assertNotIn("苗爷", blk, "bot 账号不得进关系图谱")

    def test_notes_is_free_for_description(self):
        """`notes` 现在可以承载**描述文本**，不应再被当成 bot 标记。"""
        with tempfile.TemporaryDirectory() as td:
            sp = self._sp(td, [{"uin": "1", "alias": "花鱼", "closeness": "close",
                                "notes": "爱发芳乃图，嘴上带刺但在捧场"}])
            m = sp._iter_members()[0]
            self.assertFalse(sp.is_bot_member(m), "描述文本不应被误判为 bot 标记")
            self.assertIn("花鱼", sp.relations_block())


class TestMigrationTool(unittest.TestCase):
    def test_plan_and_apply(self):
        from tools.migrate_member_fields import apply_migration, plan
        ms = [{"uin": "1", "alias": "真人", "notes": ""},
              {"uin": "2", "alias": "星野", "notes": "bot"},
              {"uin": "3", "alias": "苗爷", "notes": "bot", "kind": "bot"}]
        self.assertEqual(len(plan(ms)), 1, "已迁的不该重复")
        ms2, n = apply_migration([dict(m) for m in ms])
        self.assertEqual(n, 1)
        self.assertEqual(next(m for m in ms2 if m["uin"] == "2")["kind"], "bot")
        # 双写期：notes 必须保留（否则旧读取点会漏掉 bot）
        self.assertEqual(next(m for m in ms2 if m["uin"] == "2")["notes"], "bot")

    def test_apply_is_idempotent(self):
        from tools.migrate_member_fields import apply_migration, plan
        ms = [{"uin": "2", "alias": "星野", "notes": "bot"}]
        ms, _ = apply_migration(ms)
        self.assertEqual(plan(ms), [], "二次执行不应再改")


if __name__ == "__main__":
    unittest.main()


class TestSetClosenessWriteGuard(unittest.TestCase):
    """写入层的"只升不降" —— 三处共同保证中的最后一道。

    1. 提案生成时过滤（`parse_proposals`）
    2. 批准时二次校验（`decide`）
    3. **写入配置时再判一次**（`StyleProfile.set_closeness`）—— 本测试
    """

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.td.cleanup()

    def _mk(self, members):
        with open(os.path.join(self.td.name, "member_relations.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"members": members}, f, ensure_ascii=False)
        import time as _t
        p = os.path.join(self.td.name, "member_relations.json")
        _t.sleep(0)
        fut = _t.time() + random_offset()
        os.utime(p, (fut, fut))
        from services.style_profile import StyleProfile
        return StyleProfile(self.td.name)

    def _read(self):
        with open(os.path.join(self.td.name, "member_relations.json"),
                  encoding="utf-8") as f:
            return json.load(f)["members"]

    def test_upgrade_allowed(self):
        import time as _t
        sp = self._mk([{"uin": "1", "alias": "甲", "closeness": "new"}])
        self.assertTrue(sp.set_closeness("1", "known"))
        _t.sleep(0)
        self.assertEqual(self._read()[0]["closeness"], "known")

    def test_downgrade_rejected(self):
        sp = self._mk([{"uin": "1", "alias": "甲", "closeness": "known"}])
        self.assertFalse(sp.set_closeness("1", "new"), "降级必须被拒")
        self.assertEqual(self._read()[0]["closeness"], "known")

    def test_force_allows_downgrade_for_human_path(self):
        """人工可绕过所有限制 —— `force=True` 是那条路径。"""
        import time as _t
        sp = self._mk([{"uin": "1", "alias": "甲", "closeness": "close"}])
        self.assertTrue(sp.set_closeness("1", "new", force=True))
        _t.sleep(0)
        self.assertEqual(self._read()[0]["closeness"], "new")

    def test_same_value_is_noop(self):
        sp = self._mk([{"uin": "1", "alias": "甲", "closeness": "known"}])
        self.assertFalse(sp.set_closeness("1", "known"), "同级不算改动")

    def test_unknown_uin_and_invalid_level(self):
        sp = self._mk([{"uin": "1", "alias": "甲", "closeness": "new"}])
        self.assertFalse(sp.set_closeness("999", "close"))
        self.assertFalse(sp.set_closeness("1", "bestie"))
        self.assertFalse(sp.set_closeness("", "close"))

    def test_other_fields_untouched(self):
        """只改目标条目的 `closeness`，不动其他字段（尤其人工写的 notes）。"""
        import time as _t
        sp = self._mk([{"uin": "1", "alias": "花鱼", "closeness": "new",
                        "notes": "人工写的描述", "other_names": ["花心"]}])
        sp.set_closeness("1", "close")
        _t.sleep(0)
        m = self._read()[0]
        self.assertEqual(m["notes"], "人工写的描述")
        self.assertEqual(m["other_names"], ["花心"])
        self.assertEqual(m["alias"], "花鱼")


def random_offset() -> float:
    """给 mtime 一个单调的小偏移 —— StyleProfile 按 mtime 热重载，
    同一秒内写两次可能不触发重载（实测踩过）。"""
    import time as _t
    return 10.0 + (_t.time() % 5.0)
