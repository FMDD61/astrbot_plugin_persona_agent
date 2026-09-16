# -*- coding: utf-8 -*-
"""S15：精确 token 对账 + LLM session 分离（用户 2026-09-15 要求）。

## 用户要求

> "由于不涉及共用前缀，因此对缓存命中的分析要求会更加严格，在缓存未被网关
> 抛弃的前提下，我们要能**精确对应上缓存命中和未命中的 tokens**"
> "同时在 LLM 日志内的 session ID 也区分开"

## 为什么需要精确对账

网关只告诉我们 `cached_tokens=N`，但**不告诉我们这 N 对应我们哪一段**。
当不一致时无法区分两种完全不同的原因：

  ① **我们改了历史** → 前缀本就不该命中（我们的错）
  ② **网关丢了缓存**（TTL 过期 / 服务端淘汰）→ 我们没问题

不区分就无法定位。做法：**保存上一次同 lineage 的请求**，与本次逐条比对算出
**公共前导**，再与网关报的 `cached_tokens` 交叉验证：

    公共前导远大于 cached_tokens  → 网关丢缓存（或 TTL 过期）
    公共前导远小于 cached_tokens  → 我们的"前缀"估算偏保守（消息粒度不够细）
    两者接近                      → 账号正常

## 为什么按 lineage 分离

RP 与 Gate 现在**共用同一份历史、各用各的 system**（S14）→ 它们是两条不同的
前缀、各自独立命中。混在一起记录就**无法归属**命中/未命中。
`lineage` = `f"{kind}:{group_id}"`（如 `rp:881438753` / `gate:881438753`）。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.token_accounting import (  # noqa: E402
    LINEAGES,
    common_prefix,
    lineage_of,
    normalize_messages,
    record_and_compare,
)


class TestNormalize(unittest.TestCase):
    def test_only_role_and_content(self):
        """前缀比较只看 role+content —— name 等字段变化也应算"变了"。"""
        got = normalize_messages([{"role": "user", "content": "a", "name": "甲"},
                                  {"role": "assistant", "content": "b"}])
        self.assertEqual(got, [
            {"role": "user", "content": "a", "name": "甲"},
            {"role": "assistant", "content": "b"},
        ])

    def test_tolerates_garbage(self):
        self.assertEqual(normalize_messages(None), [])
        self.assertEqual(normalize_messages([None, 1, "x"]), [])
        self.assertEqual(normalize_messages([{"content": "无role"}]), [])

    def test_strips_internal_keys(self):
        """内部元数据（_mid/_uin）不得参与前缀比较。"""
        got = normalize_messages([{"role": "user", "content": "a",
                                   "_mid": "m1", "_uin": "u1"}])
        self.assertEqual(got, [{"role": "user", "content": "a"}])


class TestCommonPrefix(unittest.TestCase):
    def test_append_only_means_full_prefix(self):
        """🔴 追加语义：新请求以旧请求为前缀 → 公共前导 = 旧请求全长。"""
        prev = [{"role": "system", "content": "人格"},
                {"role": "user", "content": "甲"}]
        cur = prev + [{"role": "assistant", "content": "回复"},
                      {"role": "user", "content": "乙"}]
        n_msgs, n_chars = common_prefix(prev, cur)
        self.assertEqual(n_msgs, 2)
        self.assertEqual(n_chars,
                         sum(len("人格") + len("甲"), ) if False else
                         len("人格") + len("甲"))

    def test_change_in_middle_truncates(self):
        """中间改了 → 公共前导止于改动点之前。"""
        prev = [{"role": "system", "content": "人格"},
                {"role": "user", "content": "甲"},
                {"role": "user", "content": "乙"}]
        cur = [{"role": "system", "content": "人格改过"},
                {"role": "user", "content": "甲"},
                {"role": "user", "content": "乙"}]
        n_msgs, n_chars = common_prefix(prev, cur)
        self.assertEqual(n_msgs, 0, "第一条就不同 → 公共前导为 0")
        self.assertEqual(n_chars, 0)

    def test_identical_requests(self):
        prev = [{"role": "user", "content": "abc"}]
        self.assertEqual(common_prefix(prev, prev), (1, 3))

    def test_empty_prev(self):
        self.assertEqual(common_prefix([], [{"role": "user", "content": "x"}]),
                         (0, 0))

    def test_partial_last_message_not_counted(self):
        """只比整条 —— 同一角色但内容不同即止。"""
        prev = [{"role": "user", "content": "abcdef"}]
        cur = [{"role": "user", "content": "abcXYZ"}]
        self.assertEqual(common_prefix(prev, cur), (0, 0))


class TestLineage(unittest.TestCase):
    def test_lineage_format(self):
        self.assertEqual(lineage_of("rp", "881438753"), "rp:881438753")
        self.assertEqual(lineage_of("gate", "881438753"), "gate:881438753")

    def test_rp_and_gate_are_distinct_lineages(self):
        """🔴 用户要求：LLM 日志的 session ID 必须区分开。"""
        self.assertNotEqual(lineage_of("rp", "g"), lineage_of("gate", "g"))
        self.assertIn("rp", LINEAGES)
        self.assertIn("gate", LINEAGES)


class TestRecordAndCompare(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def _rec(self, **kw):
        kw.setdefault("kind", "rp")
        kw.setdefault("group_id", "g1")
        return record_and_compare(self.dir, **kw)

    def test_first_call_has_no_common_prefix(self):
        out = self._rec(messages=[{"role": "user", "content": "甲"}])
        self.assertEqual(out["common_prefix_msgs"], 0)
        self.assertEqual(out["common_prefix_chars"], 0)
        self.assertTrue(out["first_of_lineage"])

    def test_appended_call_reports_full_prefix(self):
        base = [{"role": "system", "content": "人格"},
                {"role": "user", "content": "甲"}]
        self._rec(messages=base)
        out = self._rec(messages=base + [{"role": "assistant", "content": "回"},
                                         {"role": "user", "content": "乙"}])
        self.assertEqual(out["common_prefix_msgs"], 2)
        self.assertEqual(out["common_prefix_chars"], len("人格") + len("甲"))
        self.assertFalse(out["first_of_lineage"])

    def test_lineages_tracked_separately(self):
        """🔴 RP 与 Gate 各记各的 —— 混记会让归属错误。"""
        base = [{"role": "user", "content": "甲"}]
        self._rec(kind="rp", messages=base)
        # Gate 首见：即便内容相同，也应是"首次"（不同 lineage）
        out = self._rec(kind="gate", messages=base)
        self.assertTrue(out["first_of_lineage"],
                        "不同 lineage 不得互相污染")
        # RP 第二次仍能正确比对
        out2 = self._rec(kind="rp", messages=base + [{"role": "user", "content": "乙"}])
        self.assertEqual(out2["common_prefix_msgs"], 1)

    def test_state_survives_new_instance(self):
        """状态落盘 —— 重启后仍能对账（否则每次都当"首次"）。"""
        base = [{"role": "user", "content": "甲"}]
        self._rec(messages=base)
        out = record_and_compare(self.dir, kind="rp", group_id="g1",
                                 messages=base + [{"role": "user", "content": "乙"}])
        self.assertFalse(out["first_of_lineage"])
        self.assertEqual(out["common_prefix_msgs"], 1)

    def test_gateway_vs_ours_verdict(self):
        """交叉验证：网关 cached 与我们算的前缀是否一致。"""
        base = [{"role": "user", "content": "x" * 400}]
        self._rec(messages=base)
        cur = base + [{"role": "user", "content": "y" * 10}]
        # 网关说缓存了 400 字符对应的量
        out = self._rec(messages=cur, cached_tokens=100, prompt_tokens=110)
        self.assertEqual(out["common_prefix_chars"], 400)
        self.assertEqual(out["cached_tokens"], 100)
        self.assertEqual(out["uncached_tokens"], 10, "未命中 = prompt - cached")
        self.assertIn("verdict", out)

    def test_verdict_gateway_dropped(self):
        """🔴 唯一无歧义的信号：前缀没动（公共前导≥200 字符）却 cached=0
        → 只能是网关侧没给缓存。**不依赖字符→token 换算**，故可靠。"""
        base = [{"role": "user", "content": "x" * 400}]
        self._rec(messages=base)
        out = self._rec(messages=base + [{"role": "user", "content": "y"}],
                        cached_tokens=0, prompt_tokens=401)
        self.assertEqual(out["common_prefix_chars"], 400)
        self.assertEqual(out["verdict"], "gateway_dropped")

    def test_short_prefix_zero_cached_is_not_conclusive(self):
        """前缀太短时 cached=0 不足以断定是网关问题（可能是别的因素）。"""
        base = [{"role": "user", "content": "x" * 20}]
        self._rec(messages=base)
        out = self._rec(messages=base + [{"role": "user", "content": "y"}],
                        cached_tokens=0, prompt_tokens=21)
        self.assertNotEqual(out["verdict"], "gateway_dropped")

    def test_verdict_we_changed_history(self):
        """公共前导为 0 且网关也没缓存 → 是我们改了历史（预期内）。"""
        self._rec(messages=[{"role": "user", "content": "旧"}])
        out = self._rec(messages=[{"role": "user", "content": "全新"}],
                        cached_tokens=0, prompt_tokens=10)
        self.assertEqual(out["verdict"], "prefix_changed")

    def test_prefix_reused(self):
        """有公共前导 + 网关给了非零 cached → 报"复用"（不下"好/坏"结论）。"""
        base = [{"role": "user", "content": "x" * 400}]
        self._rec(messages=base)
        out = self._rec(messages=base + [{"role": "user", "content": "y"}],
                        cached_tokens=100, prompt_tokens=105)
        self.assertEqual(out["verdict"], "prefix_reused")

    def test_no_usage_when_gateway_omits_it(self):
        base = [{"role": "user", "content": "x" * 400}]
        self._rec(messages=base)
        out = self._rec(messages=base + [{"role": "user", "content": "y"}])
        self.assertEqual(out["verdict"], "no_usage")

    def test_never_raises_on_bad_input(self):
        self.assertIsInstance(self._rec(messages=None), dict)
        self.assertIsInstance(self._rec(messages=[None, 1]), dict)

    def test_state_file_is_bounded(self):
        """状态文件只留每个 lineage 的上一次请求 —— 不能无限增长。"""
        for i in range(30):
            self._rec(kind="rp", messages=[{"role": "user", "content": f"m{i}"}])
        p = self.dir / "token_accounting_state.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        self.assertLessEqual(len(d.get("lineages") or {}), len(LINEAGES) * 2)


if __name__ == "__main__":
    unittest.main()
