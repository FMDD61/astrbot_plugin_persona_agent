# -*- coding: utf-8 -*-
"""S18：离线 A/B 提示词工具（D2，用户 2026-09-15 要求）。

## 用户要求原文

> "提示词：A/B 测试需要专门设计，比如我们需要 `system + 关系图 + 一段群聊消息
>  + RAG`，测试脚本要能支持**控制变量**下的测试，比如只变 system，比较回复质量"

## 设计

一次 A/B = **同一场景、同一上下文骨架，只变一个指定组件**，产出并排对比。

**臂（arm）** 描述一组组件选择：

| 组件 | 含义 |
|---|---|
| `system` | 人格提示词（可换成变体文件 → 这就是"只变 system"） |
| `relations` | 关系图谱块 |
| `examples` | 示例块 |
| `kg` | KG 尾注（含 **RAG 命中原文** —— 已确认 RP 看得到） |
| `rag` | 是否查 RAG（关掉则 kg 只有关系边） |

**控制变量**由"臂只允许改一个组件"来保证：脚本会**报出各臂的差异组件**，
若某臂改了多个，给出 warning（不禁止 —— 有时确实想测组合）。

## 为什么必须离线

改线上配置会**污染生产数据**（session/logs 全混在一起），且一次只能测一个值。
离线可以用同一场景批量试多个变体，且**可复现**。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from tools.ab_prompt import (  # noqa: E402
    Arm,
    arms_diff,
    build_arm_contexts,
    load_arms,
    render_comparison,
)


class TestArm(unittest.TestCase):
    def test_defaults_all_components_on(self):
        a = Arm(name="baseline")
        self.assertTrue(a.use_system)
        self.assertTrue(a.use_relations)
        self.assertTrue(a.use_examples)
        self.assertTrue(a.use_kg)

    def test_from_dict(self):
        a = Arm.from_dict({"name": "no-rag", "rag": False, "kg": False})
        self.assertEqual(a.name, "no-rag")
        self.assertFalse(a.use_rag)
        self.assertFalse(a.use_kg)


class TestLoadArms(unittest.TestCase):
    def test_load_json_list(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "arms.json"
            p.write_text(json.dumps([
                {"name": "a", "system": "base"},
                {"name": "b", "system": "variant2"},
            ], ensure_ascii=False), encoding="utf-8")
            arms = load_arms(p)
            self.assertEqual([a.name for a in arms], ["a", "b"])

    def test_load_returns_empty_on_bad(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.json"
            p.write_text("{坏", encoding="utf-8")
            self.assertEqual(load_arms(p), [])


class TestArmsDiff(unittest.TestCase):
    """控制变量：报出每臂相对 baseline 变了什么。"""

    def test_single_change_detected(self):
        base = Arm(name="base")
        var = Arm(name="v", use_relations=False)
        d = arms_diff(base, var)
        self.assertEqual(d, ["relations"])

    def test_multiple_changes_detected(self):
        base = Arm(name="base")
        var = Arm(name="v", use_relations=False, use_kg=False)
        self.assertEqual(sorted(arms_diff(base, var)), ["kg", "relations"])

    def test_no_change(self):
        self.assertEqual(arms_diff(Arm(name="a"), Arm(name="b")), [])


class TestBuildArmContexts(unittest.TestCase):
    """装配：只放该臂启用的组件。**顺序固定**（恒定在前、易变在尾）。"""

    def _blocks(self):
        return {
            "system": "【人格】",
            "examples": "【示例块】",
            "relations": "【关系图谱】",
            "kg": "【KG尾注含RAG原文】",
            "session": [
                {"role": "user", "content": "甲：你好"},
                {"role": "assistant", "content": "嗨"},
            ],
        }

    def test_full_arm_has_all_blocks_in_order(self):
        ctx = build_arm_contexts(Arm(name="full"), self._blocks())
        joined = [str(m.get("content")) for m in ctx]
        self.assertEqual(joined[0], "【人格】")
        self.assertIn("【示例块】", joined)
        self.assertIn("【关系图谱】", joined)
        self.assertIn("甲：你好", joined)
        self.assertEqual(joined[-1], "【KG尾注含RAG原文】", "KG 尾注在最后")
        # 顺序：system < examples < relations < session < kg
        i_sys, i_ex, i_rel = (joined.index("【人格】"), joined.index("【示例块】"),
                              joined.index("【关系图谱】"))
        i_sess = next(i for i, c in enumerate(joined) if "甲：你好" in c)
        i_kg = joined.index("【KG尾注含RAG原文】")
        self.assertLess(i_sys, i_ex)
        self.assertLess(i_ex, i_rel)
        self.assertLess(i_rel, i_sess)
        self.assertLess(i_sess, i_kg)

    def test_only_system_arm(self):
        """'只变 system' 的场景：其余组件可关掉。"""
        a = Arm(name="sys-only", use_relations=False, use_examples=False,
                use_kg=False)
        ctx = build_arm_contexts(a, self._blocks())
        joined = [str(m.get("content")) for m in ctx]
        self.assertIn("【人格】", joined)
        self.assertNotIn("【示例块】", joined)
        self.assertNotIn("【关系图谱】", joined)
        self.assertNotIn("【KG尾注含RAG原文】", joined)
        self.assertIn("甲：你好", joined, "session 始终保留")

    def test_system_override_wins(self):
        """臂可覆盖 system 文本 —— 这就是'只变 system'的实现方式。"""
        a = Arm(name="v2", system_override="【人格 v2】")
        ctx = build_arm_contexts(a, self._blocks())
        joined = [str(m.get("content")) for m in ctx]
        self.assertIn("【人格 v2】", joined)
        self.assertNotIn("【人格】", joined)

    def test_missing_blocks_tolerated(self):
        """生产数据可能缺块（如关系图谱为空）→ 不得抛。"""
        ctx = build_arm_contexts(Arm(name="x"), {"system": "S", "session": []})
        self.assertEqual(len(ctx), 1)

    def test_empty_arm_returns_session_only(self):
        a = Arm(name="x", use_system=False, use_relations=False,
                use_examples=False, use_kg=False)
        ctx = build_arm_contexts(a, self._blocks())
        self.assertEqual([str(m.get("content")) for m in ctx], ["甲：你好", "嗨"])


class TestRenderComparison(unittest.TestCase):
    """并排对比输出 —— 人工判读用（与 D1 的导出风格一致）。"""

    def test_renders_each_arm(self):
        results = [
            {"arm": "baseline", "output": "回复一", "chars": 3, "ms": 1200},
            {"arm": "v2", "output": "回复二长一点", "chars": 6, "ms": 1500},
        ]
        md = render_comparison(results, scene_id="s1",
                               changed={"v2": ["system"]})
        self.assertIn("baseline", md)
        self.assertIn("回复一", md)
        self.assertIn("v2", md)
        self.assertIn("system", md, "要显示各臂变了什么（控制变量）")

    def test_marks_failures(self):
        results = [{"arm": "a", "output": "", "error": "HTTP 400"}]
        md = render_comparison(results, scene_id="s1", changed={})
        self.assertIn("HTTP 400", md)

    def test_empty_results(self):
        self.assertIn("无", render_comparison([], scene_id="s", changed={}))


if __name__ == "__main__":
    unittest.main()
