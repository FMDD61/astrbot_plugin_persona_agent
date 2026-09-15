# -*- coding: utf-8 -*-
"""S14：system prompt 持久化进会话 + 追加式更新（用户 2026-09-15 设计）。

## 设计（务必先读懂，这是全部测试的依据）

**核心机制**：system prompt 是**会话状态的一部分**（日志第一条），
**不是每轮热加载的参数**。

    会话日志(持久):
      [0]     system: P1                 ← 写进日志，此后每轮原样加载
      [1..k]  群消息段_0
      [k+1]   system: ［设定更新］P2      ← prompt 变更时**追加**
      [k+2..] 群消息段_1

    每轮请求 = 日志全量（逐字节不变）+ 本轮新消息
                ↑ 前缀永远稳定 → **追加块不破坏前缀缓存**

**为什么必然追加**：prompt 变了**不能改 [0]**（改了 = 破坏全量前缀缓存），
所以只能往后加。

**与旧实现的本质差异**：

| | 旧 | 新 |
|---|---|---|
| system prompt | `llm_generate(system_prompt=…)` 每轮传入 | 写进 session 第一条 |
| prompt 变更 | 整体换新版 → **全部缓存失效** | 追加块，[0] 不动 |
| 首轮 | 每次重新生成 | 持久（与会话同生命） |

实测旧实现的代价：`distinct_sys_hashes` 一天 8–11 个，每次全量重算
（单次 61k–99k 全价 token）。

**用户拍板**：追加块**累积保留、不清理**（清理 = 改历史 = 破缓存）。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.session_manager import SessionManager  # noqa: E402


def _mk_sm(max_messages=None):
    return SessionManager(data_dir=None, max_messages=max_messages)


class TestSystemPromptPersisted(unittest.TestCase):
    """T1/T2：system prompt 写入日志 [0]，且此后**从日志加载**（不重新生成）。"""

    def test_first_build_writes_system_at_position_zero(self):
        sm = _mk_sm()
        sm.append("g1", "user", "群友A说话", name="A", message_id="m1", sender_uin="u1")
        written = sm.ensure_system_prompt("g1", "【人格 P1】")
        self.assertTrue(written, "首次应写入")
        msgs = sm.get_contexts("g1")
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[0]["content"], "【人格 P1】")

    def test_second_call_does_not_rewrite_even_if_prompt_changed(self):
        """🔴 核心：prompt 文件变了，`[0]` **也不动** —— 这是缓存稳定的前提。"""
        sm = _mk_sm()
        sm.append("g1", "user", "甲", name="A", message_id="m1", sender_uin="u1")
        sm.ensure_system_prompt("g1", "【人格 P1】")
        sm.append("g1", "user", "乙", name="B", message_id="m2", sender_uin="u2")
        written = sm.ensure_system_prompt("g1", "【人格 P2】")   # 传入新版
        self.assertFalse(written, "已存在则不应重写")
        msgs = sm.get_contexts("g1")
        self.assertEqual(msgs[0]["content"], "【人格 P1】",
                         "旧 prompt 必须原样保留（改了会破坏全量前缀缓存）")

    def test_empty_prompt_is_not_written(self):
        sm = _mk_sm()
        self.assertFalse(sm.ensure_system_prompt("g1", ""))
        self.assertFalse(sm.ensure_system_prompt("g1", "   "))
        msgs = sm.get_contexts("g1")
        self.assertTrue(all(m.get("role") != "system" for m in msgs))

    def test_system_prompt_survives_reload(self):
        """持久化：从磁盘恢复后 `[0]` 仍在（否则每轮都会重写 → 破缓存）。"""
        with tempfile.TemporaryDirectory() as td:
            sm = SessionManager(data_dir=td, max_messages=None)
            sm.append("g1", "user", "甲", name="A", message_id="m1", sender_uin="u1")
            sm.ensure_system_prompt("g1", "【人格 P1】")
            sm.save_all()

            sm2 = SessionManager(data_dir=td, max_messages=None)
            sm2.load_all()
            msgs = sm2.get_contexts("g1")
            self.assertEqual(msgs[0]["role"], "system")
            self.assertEqual(msgs[0]["content"], "【人格 P1】")
            # 恢复后不应重写
            self.assertFalse(sm2.ensure_system_prompt("g1", "【人格 P2】"))

    def test_system_prompt_not_in_quote_basis(self):
        """🔴 `[r:-N]` 数的是**群消息**，system prompt 不该占编号位。

        它在所有群消息**之前**，所以"倒数第 N 条"不受它影响；
        而把它算进编号基会让 `[r:-1]` 指向 system 块（无 mid）→ 引用失效。
        """
        sm = _mk_sm()
        sm.ensure_system_prompt("g1", "【人格】")
        sm.append("g1", "user", "甲", name="A", message_id="m1", sender_uin="u1")
        sm.append("g1", "user", "乙", name="B", message_id="m2", sender_uin="u2")
        q = sm.quote_snapshot("g1")
        self.assertEqual(len(q), 2, "编号基只含 2 条群消息")
        self.assertEqual(q.resolve(1).message_id, "m2", "[r:-1] 应指最新群消息")
        self.assertEqual(q.resolve(2).message_id, "m1")


class TestAppendOnlyUpdate(unittest.TestCase):
    """T3/T4：prompt 变更 → 追加块（带声明），且**前缀逐字节不变**。"""

    def test_update_appends_with_declaration(self):
        sm = _mk_sm()
        sm.append("g1", "user", "甲", name="A", message_id="m1", sender_uin="u1")
        sm.ensure_system_prompt("g1", "【人格 P1】")
        sm.append("g1", "user", "乙", name="B", message_id="m2", sender_uin="u2")
        # 声明由**调用方**包装（会话层只管存取），这里模拟调用方的形态
        sm.append_system_update("g1", "［设定更新］以下为最新设定，"
                                      "与此前冲突之处以此为准：\n【人格 P2】")
        msgs = sm.get_contexts("g1")
        self.assertEqual(msgs[-1]["role"], "system")
        self.assertIn("【人格 P2】", msgs[-1]["content"])
        # 声明必须存在，否则模型面对 P1/P2 冲突会摇摆
        self.assertIn("为准", msgs[-1]["content"])

    def test_update_preserves_prefix_byte_for_byte(self):
        """🔴 追加式的全部意义：前缀逐字节不变 → 缓存可复用。"""
        sm = _mk_sm()
        sm.append("g1", "user", "甲", name="A", message_id="m1", sender_uin="u1")
        sm.ensure_system_prompt("g1", "【人格 P1】")
        sm.append("g1", "user", "乙", name="B", message_id="m2", sender_uin="u2")
        before = [dict(m) for m in sm.get_contexts("g1")]

        sm.append_system_update("g1", "【人格 P2】")
        after = sm.get_contexts("g1")

        self.assertEqual(before, after[:len(before)],
                         "追加块之前的每一条都必须逐字节相同")
        self.assertEqual(len(after), len(before) + 1)

    def test_updates_accumulate_not_replaced(self):
        """用户拍板：**累积保留、不清理**（清理 = 改历史 = 破缓存）。"""
        sm = _mk_sm()
        sm.ensure_system_prompt("g1", "P1")
        sm.append_system_update("g1", "P2")
        sm.append_system_update("g1", "P3")
        msgs = sm.get_contexts("g1")
        systems = [str(m.get("content")) for m in msgs if m.get("role") == "system"]
        self.assertEqual(len(systems), 3, "P1/P2/P3 应全部保留")
        self.assertIn("P2", systems[1])
        self.assertIn("P3", systems[2])

    def test_no_update_when_prompt_unchanged(self):
        sm = _mk_sm()
        sm.ensure_system_prompt("g1", "P1")
        self.assertFalse(sm.append_system_update("g1", "P1"),
                         "内容相同不应产生追加块（否则每轮都破缓存）")

    def test_no_update_for_empty(self):
        sm = _mk_sm()
        sm.ensure_system_prompt("g1", "P1")
        self.assertFalse(sm.append_system_update("g1", ""))
        self.assertFalse(sm.append_system_update("g1", "   "))

    def test_update_without_initial_prompt_is_noop(self):
        """会话还没有 system prompt 时，"更新"没有意义 —— 应走 ensure 而非 update。"""
        sm = _mk_sm()
        sm.append("g1", "user", "甲", name="A", message_id="m1", sender_uin="u1")
        self.assertFalse(sm.append_system_update("g1", "P2"))


class TestNewSessionUsesLatest(unittest.TestCase):
    """T5：新会话用**最新** prompt，不含旧追加块。"""

    def test_rotation_resets_to_latest(self):
        sm = _mk_sm()
        sm.ensure_system_prompt("g1", "P1")
        sm.append_system_update("g1", "P2")
        self.assertEqual(len([m for m in sm.get_contexts("g1")
                              if m.get("role") == "system"]), 2)
        sm.clear("g1")
        written = sm.ensure_system_prompt("g1", "P3")
        self.assertTrue(written, "轮转后应重新写入")
        msgs = sm.get_contexts("g1")
        systems = [str(m.get("content")) for m in msgs if m.get("role") == "system"]
        self.assertEqual(len(systems), 1, "新会话不得携带旧追加块")
        self.assertEqual(systems[0], "P3")

    def test_rotation_via_day_change(self):
        """日旋转（跨日）后同样用最新 prompt，且不带旧块。"""
        sm = _mk_sm()
        sm.append("g1", "user", "甲", name="A", message_id="m1", sender_uin="u1")
        sm.ensure_system_prompt("g1", "P1")
        sm.append_system_update("g1", "P2")
        # 强制跨日
        sess = sm._sessions["g1"]
        sess.day = "2000-01-01"
        old = sm.rotate_if_day_changed("g1")
        self.assertTrue(old)
        self.assertTrue(sm.ensure_system_prompt("g1", "P3"))
        systems = [str(m.get("content")) for m in sm.get_contexts("g1")
                   if m.get("role") == "system"]
        self.assertEqual(systems, ["P3"])


class TestCapacityAccounting(unittest.TestCase):
    """容量：追加块会占位置，不得把 system prompt 挤掉。"""

    def test_system_prompt_survives_capacity_pressure(self):
        sm = _mk_sm(max_messages=10)
        sm.ensure_system_prompt("g1", "P1")
        for i in range(50):
            sm.append("g1", "user", f"消息{i}", name="A",
                      message_id=f"m{i}", sender_uin="u1")
        msgs = sm.get_contexts("g1")
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[0]["content"], "P1", "system prompt 不得被挤出")

    def test_updates_also_survive_in_order(self):
        sm = _mk_sm(max_messages=12)
        sm.ensure_system_prompt("g1", "P1")
        sm.append_system_update("g1", "［设定更新］以此为准：P2")
        for i in range(30):
            sm.append("g1", "user", f"消息{i}", name="A",
                      message_id=f"m{i}", sender_uin="u1")
        systems = [str(m.get("content")) for m in sm.get_contexts("g1")
                   if m.get("role") == "system"]
        self.assertTrue(any("P1" in s for s in systems))
        self.assertTrue(any("P2" in s for s in systems),
                        "追加块不应被容量挤出（它是设定的一部分）")
        # 顺序必须保持 P1 在 P2 之前（"以最新为准"依赖顺序）
        i1 = next(i for i, m in enumerate(sm.get_contexts("g1"))
                  if m.get("role") == "system" and "P1" in str(m.get("content")))
        i2 = next(i for i, m in enumerate(sm.get_contexts("g1"))
                  if m.get("role") == "system" and "P2" in str(m.get("content")))
        self.assertLess(i1, i2)


if __name__ == "__main__":
    unittest.main()


class TestGateRpSystemSeparation(unittest.TestCase):
    """T7：RP 与 Gate 的 system **必须分离**（S14，用户要求"不再共用缓存"）。

    ## 为什么（S4 的实测事故）

    S4 曾把 **RP 的人格提示词**当 Gate 的 system 传过去（为"共享前缀省缓存"）。
    结果：模型看到"你是一个 QQ 群里的活跃群员…禁止换行、禁止列表"，
    **就不再认为自己是裁判，开始参与聊天**：

        gate_degraded: "parse_failed: '笑什么呢车车，说出来让我也乐一乐~'"

    实测解析失败率 **0% → 40~60%**，244 条决策退化为保守静默 → 压制发言频率。

    ## 现行设计

    两者**共用同一份历史**（Gate 也要看群聊），但：
      - RP：人格在**会话首条**（session 状态），随会话 `init`/追加更新
      - Gate：system = `GATE_SYSTEM_PROMPT`（裁判身份），且其上下文**掉掉人格首条**
    前缀因此不同 → 各自独立命中缓存，符合"不再共用"。
    """

    def _mk(self, td):
        import json as _json, os as _os
        with open(_os.path.join(td, "system_prompt_fragments.json"), "w",
                  encoding="utf-8") as f:
            _json.dump({"identity": "你是群员夕化炭"}, f, ensure_ascii=False)
        with open(_os.path.join(td, "member_relations.json"), "w",
                  encoding="utf-8") as f:
            _json.dump({"members": [{"uin": "1", "alias": "花鱼",
                                     "closeness": "close"}]}, f, ensure_ascii=False)

    def _pipeline(self, td, sm):
        from services.style_profile import StyleProfile
        from services.pipeline import PersonaPipeline
        sp = StyleProfile(td)
        sm.sync_system_prompt("g1", sp.system_prompt())
        p = PersonaPipeline(
            style=sp, rag=None, interjection=None, emotion=None, gate=None,
            session_mgr=sm, kg_provider=None, buffer=None,
            generate=lambda *a: None,
            examples_block=lambda: "【示例块】",
            relations_block=sp.relations_block,
            system_prompt=sp.system_prompt,
            session_append=lambda *a: None, debounce_sec=0.0)
        return p, sp

    def _sm(self):
        sm = SessionManager(data_dir=None, max_messages=None)
        sm.append("g1", "user", "甲说话", name="A", message_id="m1", sender_uin="u1")
        sm.append("g1", "assistant", "机器人的回复")
        return sm

    def test_rp_persona_is_first(self):
        with tempfile.TemporaryDirectory() as td:
            self._mk(td)
            p, sp = self._pipeline(td, self._sm())
            base = p._assemble_base("g1")
            self.assertEqual(base[0]["role"], "system")
            self.assertIn("群员", str(base[0]["content"]),
                          "RP 的人格必须是整个数组的第一条")

    def test_gate_context_has_no_persona(self):
        with tempfile.TemporaryDirectory() as td:
            self._mk(td)
            p, _ = self._pipeline(td, self._sm())
            gate = p.shared_context("g1")
            self.assertFalse(
                any(m.get("role") == "system" and "群员" in str(m.get("content"))
                    for m in gate),
                "Gate 上下文**不得**含 RP 人格（否则模型会去聊天而非判断）")

    def test_gate_keeps_history_and_other_blocks(self):
        """掉掉的只是人格首条 —— 历史、示例块、关系图谱都要保留（Gate 也需要看群聊）。"""
        with tempfile.TemporaryDirectory() as td:
            self._mk(td)
            p, _ = self._pipeline(td, self._sm())
            gate = p.shared_context("g1")
            joined = json.dumps(gate, ensure_ascii=False)
            self.assertIn("甲说话", joined, "Gate 必须看得到群聊历史")
            self.assertIn("机器人的回复", joined)
            self.assertIn("【示例块】", joined)
            self.assertIn("花鱼", joined, "Gate 要看得到关系图谱")

    def test_gate_and_rp_prefixes_differ(self):
        """用户要求"不再共用缓存" → 两者前缀必须不同。"""
        with tempfile.TemporaryDirectory() as td:
            self._mk(td)
            p, _ = self._pipeline(td, self._sm())
            rp = [str(m.get("content")) for m in p._assemble_base("g1")]
            gate = [str(m.get("content")) for m in p.shared_context("g1")]
            self.assertNotEqual(rp[0], gate[0], "首条必须不同（人格 vs 非人格）")

    def test_gate_service_uses_judge_system(self):
        """GateService 不传 shared_system_prompt 时，system 必须是裁判身份。"""
        from services.gate import GATE_SYSTEM_PROMPT, GateService
        captured = {}

        async def llm(prompt=None, *, messages=None, system_prompt=None):
            captured["sp"] = system_prompt
            return '{"reply": false, "conflict": false, "reason": "x"}'

        gs = GateService(llm, timeout=5)
        import asyncio
        asyncio.run(gs.decide("g", [], "甲", "hi",
                              contexts=[{"role": "user", "content": "历史"}]))
        self.assertEqual(captured["sp"], GATE_SYSTEM_PROMPT)
        self.assertIn("分析师", captured["sp"])
