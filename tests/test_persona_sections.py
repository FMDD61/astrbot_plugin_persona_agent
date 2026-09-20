# -*- coding: utf-8 -*-
"""C7 / C10：八段人格装配的回归测试。

**这一批测试的存在理由**（`BUGS.md` B-037 的四条盲区，见
`scratch/prompt_v2_recon/R1_persona_assembly.md` §6.2）：

1. 旧测试**没有任何"键覆盖率"断言**，全是反向约束（"不得含 X"）→
   22 键丢了 14 个、持续数月无人发现；
2. 夹具照抄了实现里的过时键元组 → 夹具与实现**同源漂移、互证"正确"**；
3. 启动自检只量长度（910 → 6145 都不告警）。

所以本文件正着写：**每个段都必须真的在卡里**，缺了必须在 manifest 里点名。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services import persona as P  # noqa: E402
from services.style_profile import StyleProfile  # noqa: E402


def _mk(td, sections=None, fragments=None):
    """建一个最小数据目录。sections: {sid: text} 写进 persona/ 覆盖层。"""
    pdir = os.path.join(td, "persona")
    os.makedirs(pdir, exist_ok=True)
    for sid, text in (sections or {}).items():
        with open(os.path.join(pdir, f"{sid}.md"), "w", encoding="utf-8") as f:
            f.write(text)
    if fragments is not None:
        with open(os.path.join(td, "system_prompt_fragments.json"), "w",
                  encoding="utf-8") as f:
            json.dump(fragments, f, ensure_ascii=False)
    return StyleProfile(td)


class TestAssemblyCoverage(unittest.TestCase):
    """正着断言：段 → 卡。这一条就是 B-037 的回归。"""

    def test_builtin_defaults_are_all_wired(self):
        """内置默认必须全部进卡（s3 无记忆正文 → 整段省略，属设计）。"""
        with tempfile.TemporaryDirectory() as td:
            sp = _mk(td)
            card = sp.system_prompt()
            for sid, text in P.SECTION_TEXT.items():
                if sid == "s3_memory":
                    continue          # 无记忆正文时整段不出现（§4.4）
                marker = text.splitlines()[0]      # 段首【标题】
                self.assertIn(marker, card, f"段 {sid} 未进人格卡（B-037 复发）")
            self.assertIn("【规则】", card)
            self.assertIn("[r]", card, "§8 的引用打标教学必须在卡里")

    def test_eight_sections_present_in_order(self):
        with tempfile.TemporaryDirectory() as td:
            sp = _mk(td)
            card = sp.system_prompt()
            heads = ["【我是谁】", "【我在群里想做什么】", "【我待的这个地方】",
                     "【行为反应】", "【我怎么说话】", "【规则】"]
            pos = [card.index(h) for h in heads]
            self.assertEqual(pos, sorted(pos), "段序必须与 RP_ORDER 一致")

    def test_missing_section_is_enumerated_not_silent(self):
        """段缺失必须在 manifest 里点名（降级必须可见）。"""
        with tempfile.TemporaryDirectory() as td:
            sp = _mk(td, sections={"s6_behavior": ""})   # 空文件 = 无覆盖 → 仍用默认
            m = sp.persona_manifest()
            self.assertIn("s6_behavior", m["used"], "空覆盖文件应回落到内置默认")
            sp2 = _mk(td, sections={"s7_style": "   "})
            self.assertIn("s7_style", sp2.persona_manifest()["used"])
            # 真正缺的（无内置默认且无文件）只有 s3（无记忆时）
            self.assertIn("s3_memory", m["missing"])

    def test_unreadable_corrupt_file_falls_back_to_default(self):
        """覆盖文件是目录/不可读 → 回落内置默认，而不是让整段消失。"""
        with tempfile.TemporaryDirectory() as td:
            sp = _mk(td)
            os.makedirs(os.path.join(td, "persona", "s4_world.md"), exist_ok=True)
            card = sp.system_prompt()
            self.assertIn("【我待的这个地方】", card)


class TestSectionOverrides(unittest.TestCase):
    def test_file_overrides_default_and_delete_reverts(self):
        with tempfile.TemporaryDirectory() as td:
            sp = _mk(td, sections={"s1_who": "【自定义】我是测试人格"})
            self.assertIn("我是测试人格", sp.system_prompt())
            self.assertNotIn("成员丁", sp.system_prompt())
            os.remove(os.path.join(td, "persona", "s1_who.md"))
            self.assertIn("成员丁", sp.system_prompt(), "删文件应回到内置默认")

    def test_override_is_hot_reloaded(self):
        with tempfile.TemporaryDirectory() as td:
            sp = _mk(td, sections={"s1_who": "第一版"})
            self.assertIn("第一版", sp.system_prompt())
            path = os.path.join(td, "persona", "s1_who.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write("第二版")
            os.utime(path, (0, 0))          # 保证 mtime 变化（同秒写入）
            self.assertIn("第二版", sp.system_prompt(), "段文件必须热重载")

    def test_manifest_reports_overrides(self):
        with tempfile.TemporaryDirectory() as td:
            sp = _mk(td, sections={"s2_goal": "改过的目标"})
            self.assertEqual(sp.persona_manifest()["overridden"], ["s2_goal"])

    def test_schedule_comes_from_legacy_fragments(self):
        """作息段没有内置默认：沿用旧人格文件的 schedule 键（丢了就是静默能力回退）。"""
        with tempfile.TemporaryDirectory() as td:
            sp = _mk(td, fragments={"schedule": "大学生作息，凌晨不发言。"})
            self.assertIn("大学生作息", sp.system_prompt())
            m = sp.persona_manifest()
            self.assertIn("sched", m["used"])
            self.assertNotIn("schedule", m["superseded_keys"], "schedule 仍在使用中")

    def test_no_schedule_means_section_omitted(self):
        with tempfile.TemporaryDirectory() as td:
            sp = _mk(td, fragments={"identity": "旧人格"})
            self.assertNotIn("sched", sp.persona_manifest()["used"])


class TestMemorySection(unittest.TestCase):
    def test_layers_render_and_empty_layers_omitted(self):
        block = P.render_memory_block({
            "recent_days": ["09-17 拔智齿", "09-16 下雨"],
            "recent_months": ["2026-08 开学"],
        })
        self.assertIn("这几天：", block)
        self.assertIn("- 09-17 拔智齿", block)
        self.assertIn("前几个月：", block)
        self.assertNotIn("前几周：", block, "空层整段不出现（§4.4）")
        self.assertLess(block.index("这几天："), block.index("前几个月："))

    def test_memory_from_digest_file(self):
        with tempfile.TemporaryDirectory() as td:
            sp = _mk(td)
            with open(os.path.join(td, "memory_digest.json"), "w", encoding="utf-8") as f:
                json.dump({"layers": {"recent_days": ["09-17 成员戊拔智齿"]}}, f,
                          ensure_ascii=False)
            card = sp.system_prompt()
            self.assertIn("【历史群聊摘要】", card)
            self.assertIn("- 09-17 成员戊拔智齿", card)

    def test_no_digest_means_section_absent(self):
        with tempfile.TemporaryDirectory() as td:
            card = _mk(td).system_prompt()
            self.assertNotIn("【历史群聊摘要】", card, "无内容时不留空壳标题")

    def test_corrupt_digest_is_tolerated(self):
        with tempfile.TemporaryDirectory() as td:
            sp = _mk(td)
            with open(os.path.join(td, "memory_digest.json"), "w", encoding="utf-8") as f:
                f.write("{不是 json")
            self.assertNotIn("【历史群聊摘要】", sp.system_prompt())


class TestGateAndSummaryViews(unittest.TestCase):
    def test_gate_head_has_1_2_4_and_decision_only(self):
        with tempfile.TemporaryDirectory() as td:
            head = _mk(td).gate_system_prompt()
            self.assertIn("【我是谁】", head)
            self.assertIn("【我在群里想做什么】", head)
            self.assertIn("【我待的这个地方】", head)
            self.assertIn("【我什么时候会接话】", head)
            # D27/D32/C22：语言风格、行为反应、规则不进 Gate
            self.assertNotIn("【我怎么说话】", head)
            self.assertNotIn("【行为反应】", head)
            self.assertNotIn("【规则】", head)
            self.assertNotIn("【贴贴的说法】", head)

    def test_gate_head_differs_from_rp_card_from_first_bytes(self):
        """C22：两边头部必然分叉 —— 不追求跨调用共享前缀（prompt_v2_gate §2）。"""
        with tempfile.TemporaryDirectory() as td:
            sp = _mk(td)
            self.assertNotEqual(sp.system_prompt(), sp.gate_system_prompt())

    def test_summary_head_is_identity_only(self):
        with tempfile.TemporaryDirectory() as td:
            head = _mk(td).summary_system_prompt()
            self.assertIn("【我是谁】", head)
            self.assertIn("【我在群里想做什么】", head)
            self.assertNotIn("【我怎么说话】", head, "C32：语言风格不进总结 system")
            self.assertNotIn("[emote:", head, "工具标记不得进总结 system（会被照抄）")
            self.assertNotIn("【规则】", head)


class TestLegacyMode(unittest.TestCase):
    def test_legacy_mode_uses_old_key_tuple(self):
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "persona"), exist_ok=True)
            with open(os.path.join(td, "system_prompt_fragments.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"identity": "旧身份", "tone": "旧语气",
                           "rules": ["旧规则"]}, f, ensure_ascii=False)
            sp = StyleProfile(td, sections_mode="legacy")
            card = sp.system_prompt()
            self.assertIn("旧身份", card)
            self.assertIn("旧语气", card)
            self.assertIn("旧规则", card)
            self.assertEqual(sp.persona_manifest()["mode"], "legacy")

    def test_unknown_mode_falls_back_to_v2(self):
        with tempfile.TemporaryDirectory() as td:
            sp = StyleProfile(td, sections_mode="乱写的")
            self.assertIn("【我是谁】", sp.system_prompt())

    def test_empty_v2_assembly_falls_back_with_trace(self):
        """所有段都空 → 退回旧装配，且 manifest 记 fallback（不静默）。"""
        with tempfile.TemporaryDirectory() as td:
            sp = _mk(td, fragments={"identity": "兜底身份"})
            # 把所有内置段文件清空 + 不提供 schedule → v2 装配为空
            sp._sections_mode = "v2"
            sp.persona_section_texts = lambda: {sid: "" for sid in P.ALL_SECTIONS}
            card = sp.system_prompt()
            self.assertIn("兜底身份", card)
            self.assertEqual(sp.last_persona_report.get("fallback"), "empty_assembly")


class TestMaterializeAndSelfCheck(unittest.TestCase):
    def test_ensure_persona_files_writes_once_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as td:
            sp = _mk(td, fragments={"schedule": "作息文本"})
            created = sp.ensure_persona_files()
            self.assertIn("s1_who.md", created)
            self.assertIn("sched.md", created)
            self.assertIn("README.md", created)
            path = os.path.join(td, "persona", "s1_who.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write("我改过的")
            created2 = sp.ensure_persona_files()
            self.assertNotIn("s1_who.md", created2, "已存在不得覆盖")
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "我改过的")
            self.assertIn("我改过的", sp.system_prompt())

    def test_materialized_files_reproduce_builtin_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            sp = _mk(td)
            before = sp.system_prompt()
            sp.ensure_persona_files()
            self.assertEqual(before, sp.system_prompt(),
                             "物化不得改变装配结果（默认==文件）")

    def test_manifest_counts_superseded_keys(self):
        """B-037 的另一半：旧键不再被读取，这件事必须能枚举出来。"""
        with tempfile.TemporaryDirectory() as td:
            sp = _mk(td, fragments={
                "identity": "x" * 10, "tone": "y" * 20,
                "expression_dna": {"a": "b"}, "schedule": "作息",
            })
            m = sp.persona_manifest()
            self.assertIn("identity", m["superseded_keys"])
            self.assertIn("expression_dna", m["superseded_keys"])
            self.assertNotIn("schedule", m["superseded_keys"])
            self.assertGreaterEqual(m["superseded_chars"], 30)

    def test_prompt_is_stable_across_hours(self):
        """冻结契约：人格文本不得含随时间变化的内容（否则每轮击穿前缀缓存）。"""
        with tempfile.TemporaryDirectory() as td:
            sp = _mk(td)
            a = sp.system_prompt(local_hour=3)
            b = sp.system_prompt(local_hour=18)
            self.assertEqual(a, b)


class TestPureAssemblyHelpers(unittest.TestCase):
    def test_compose_returns_used_and_missing_via_manifest(self):
        texts = {"s1_who": "A", "s2_goal": "", "sched": "S"}
        text, used = P.compose(texts, ("s1_who", "sched", "s2_goal"))
        self.assertEqual(used, ("s1_who", "sched"))
        self.assertNotIn("s2_goal", text)

    def test_manifest_lists_missing(self):
        m = P.manifest({"s1_who": "A"}, ("s1_who", "s2_goal"))
        self.assertEqual(m["missing"], ["s2_goal"])
        self.assertEqual(m["chars"]["s2_goal"], 0)


if __name__ == "__main__":
    unittest.main()
