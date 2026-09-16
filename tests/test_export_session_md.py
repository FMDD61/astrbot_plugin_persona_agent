# -*- coding: utf-8 -*-
"""S16：思维链留存 + session markdown 导出（用户 2026-09-15 要求）。

## 用户要求的原文要点

> "session 日志改为保留完整思维链：长度可接受。可以每月清理…"
> "人工看：我认为这是必须的一环，LLM 总是比人工快，所以关键在提高人工看的效率。
>  脚本化 session 日志导出成 markdown 文件的流程，结构化文档辅助下，人工查看的
>  速度能大涨。人工要求看到 LLM 能看到和不能看到的，包括不同的消息类型
>  （system, user, assistant, tool 等）、思维链、KG 内容等等"

## 🔴 关键区别（决定实现方式）

思维链要**留存供人工看**，但**不应进入发给 LLM 的上下文** —— 已讨论过的理由：
Context Rot（信噪比退化）+ 推理链不忠实（长而详尽的推理链可能不如实反映决策）。

所以：**存储有、发送无**。

    session 文件:  {"role":"assistant","content":"…","_reasoning":"<完整思维链>"}
    发给 LLM:      {"role":"assistant","content":"…"}          ← _reasoning 被剥掉
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.session_manager import SessionManager  # noqa: E402
from tools.export_session_md import (  # noqa: E402
    export_markdown,
    render_message,
)


class TestReasoningPersistedNotSent(unittest.TestCase):
    """思维链：**存进 session、不进 LLM 上下文**。"""

    def test_reasoning_is_stored(self):
        sm = SessionManager(data_dir=None, max_messages=None)
        sm.append("g1", "assistant", "回复正文", reasoning="我先想了 A 又想了 B")
        msgs = sm.get_contexts("g1")
        self.assertEqual(len(msgs), 1)
        # 对外出口不带（否则会进 LLM 请求）
        self.assertNotIn("_reasoning", msgs[0])

    def test_reasoning_readable_by_export(self):
        """导出要能拿到 —— 用专门的只读接口，绕开对外剥离。"""
        sm = SessionManager(data_dir=None, max_messages=None)
        sm.append("g1", "assistant", "回复正文", reasoning="思维链内容 XYZ")
        raw = sm.raw_messages("g1")
        self.assertEqual(len(raw), 1)
        self.assertEqual(raw[0].get("_reasoning"), "思维链内容 XYZ")

    def test_reasoning_survives_save_and_reload(self):
        with tempfile.TemporaryDirectory() as td:
            sm = SessionManager(data_dir=td, max_messages=None)
            sm.append("g1", "assistant", "回复", reasoning="思考过程 ABC")
            sm.save_all()
            sm2 = SessionManager(data_dir=td, max_messages=None)
            sm2.load_all()
            raw = sm2.raw_messages("g1")
            self.assertEqual(raw[0].get("_reasoning"), "思考过程 ABC")

    def test_reasoning_not_in_quote_basis(self):
        """编号基只数消息，思维链不是独立消息。"""
        sm = SessionManager(data_dir=None, max_messages=None)
        sm.append("g1", "assistant", "回复", reasoning="思考", message_id="m1")
        sm.append("g1", "user", "甲：下一句", name="甲",
                  message_id="m2", sender_uin="u1")
        q = sm.quote_snapshot("g1")
        self.assertEqual(len(q), 2)

    def test_empty_reasoning_not_stored(self):
        sm = SessionManager(data_dir=None, max_messages=None)
        sm.append("g1", "assistant", "回复", reasoning="")
        self.assertNotIn("_reasoning", sm.raw_messages("g1")[0])


class TestRenderMessage(unittest.TestCase):
    """markdown 渲染：**人工要看到 LLM 能看到和不能看到的**。"""

    def test_user_message_shows_speaker(self):
        out = render_message({"role": "user", "content": "甲：你好"}, 0)
        self.assertIn("user", out)
        self.assertIn("甲：你好", out)

    def test_assistant_message_with_reasoning(self):
        out = render_message({"role": "assistant", "content": "回复",
                              "_reasoning": "思考内容"}, 1)
        self.assertIn("assistant", out)
        self.assertIn("回复", out)
        self.assertIn("思考内容", out)
        self.assertIn("思维链", out, "必须显式标注，人工才看得见")

    def test_system_message_marked(self):
        out = render_message({"role": "system", "content": "人格设定"}, 2)
        self.assertIn("system", out)
        self.assertIn("人格设定", out)

    def test_tool_message_type_supported(self):
        """用户点名要看到 tool 等消息类型（即便我们当前不用）。"""
        out = render_message({"role": "tool", "content": '{"ok":true}'}, 3)
        self.assertIn("tool", out)

    def test_unknown_role_still_rendered(self):
        """未知 role 也要渲染出来（人工排查时最怕"看不见的东西"）。"""
        out = render_message({"role": "weird", "content": "x"}, 4)
        self.assertIn("weird", out)

    def test_message_index_present(self):
        """带序号 —— 便于人工在对话里引用"第几条"。"""
        out = render_message({"role": "user", "content": "x"}, 42)
        self.assertIn("42", out)

    def test_internal_keys_surfaced_when_requested(self):
        """内部元数据（_mid/_uin）人工排查时需要看到。"""
        out = render_message({"role": "user", "content": "x", "_mid": "m9",
                              "_uin": "u9"}, 0)
        self.assertIn("m9", out)
        self.assertIn("u9", out)


class TestExportMarkdown(unittest.TestCase):
    """导出成 markdown：结构化文档，提升人工查看效率。"""

    def _session_file(self, td, group="g1", day="2026-09-16"):
        payload = {
            "version": 2, "group_id": group, "day": day,
            "system_prompt": "【人格 P1】",
            "sys_blocks": {"0": "［设定更新］以此为准：P2"},
            "messages": [
                {"role": "user", "content": "甲：第一句", "_mid": "m1", "_uin": "u1"},
                {"role": "assistant", "content": "机器人回复一",
                 "_reasoning": "我在想…"},
                {"role": "system", "__sys_block__": 0},
                {"role": "user", "content": "乙：第二句", "_mid": "m2", "_uin": "u2"},
            ],
        }
        p = Path(td) / f"session_{group}_{day}.json"
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return p

    def test_export_produces_readable_markdown(self):
        with tempfile.TemporaryDirectory() as td:
            src = self._session_file(td)
            out = Path(td) / "out.md"
            stats = export_markdown(src, out)
            md = out.read_text(encoding="utf-8")
            self.assertGreater(len(md), 100)
            # 4 条消息 + 1 条 system prompt = 5（system prompt 不入 messages，但会渲染）
            self.assertEqual(stats["messages"], 5)
            self.assertEqual(stats["reasoning_chars"], len("我在想…"))

    def test_export_shows_system_prompt_and_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            src = self._session_file(td)
            out = Path(td) / "out.md"
            export_markdown(src, out)
            md = out.read_text(encoding="utf-8")
            self.assertIn("【人格 P1】", md, "首条 system prompt 必须可见")
            self.assertIn("P2", md, "可变块（设定更新）必须可见")

    def test_export_shows_reasoning(self):
        with tempfile.TemporaryDirectory() as td:
            src = self._session_file(td)
            out = Path(td) / "out.md"
            export_markdown(src, out)
            md = out.read_text(encoding="utf-8")
            self.assertIn("我在想…", md)

    def test_export_header_has_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            src = self._session_file(td)
            out = Path(td) / "out.md"
            export_markdown(src, out)
            md = out.read_text(encoding="utf-8")
            self.assertIn("g1", md)
            self.assertIn("2026-09-16", md)

    def test_export_missing_file_returns_error(self):
        with tempfile.TemporaryDirectory() as td:
            stats = export_markdown(Path(td) / "nope.json", Path(td) / "o.md")
            self.assertIn("error", stats)

    def test_export_corrupt_file_returns_error(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.json"
            p.write_text("{坏", encoding="utf-8")
            stats = export_markdown(p, Path(td) / "o.md")
            self.assertIn("error", stats)

    def test_export_includes_context_preview(self):
        """可选：带上 KG 尾注等"LLM 能看到但不在 session 里"的内容。"""
        with tempfile.TemporaryDirectory() as td:
            src = self._session_file(td)
            out = Path(td) / "out.md"
            export_markdown(src, out,
                            extra={"kg_tail": "【关系】花鱼：爱发芳乃图",
                                   "rag": ["相似发言A"]})
            md = out.read_text(encoding="utf-8")
            self.assertIn("花鱼", md)
            self.assertIn("相似发言A", md)


if __name__ == "__main__":
    unittest.main()


class TestNameNeverLostOnPersist(unittest.TestCase):
    """🔴 回归：`name` 曾在落盘时被永久丢失（S15 引入，S16 修）。

    ## 事故

    `_public()` 剥掉 `name`（本意是"发 LLM 时别带重复标识"），而恢复路径写的是
    `_public(m) | {k: m[k] for k in ("_mid","_uin","_reasoning") if m.get(k)}`
    —— **没有 "name"**。于是 `save_all()` 落盘的条目永久失去 `name`。

    ## 为什么这很严重

    R5 之前写入的条目（实测生产里 **1313 条**）**内容里没有发言前缀**，
    `name` 是它们**唯一的发言人标识**。丢 name = 那些条目在 LLM 眼里**没有主**
    → 直接加剧"分不清谁说了什么"（Q1）。

    ## 修法

    存储与对外**分离**：
      - `_public()` 只剥内部元数据（`_mid`/`_uin`/`_reasoning`），**保留 name**
      - `_wire()`（发 LLM）在 `_public` 基础上再剥 name
    """

    def test_name_survives_save_reload(self):
        with tempfile.TemporaryDirectory() as td:
            sm = SessionManager(data_dir=td, max_messages=None)
            sm.append("g1", "user", "你好", name="花鱼",
                      message_id="m1", sender_uin="848671904")
            sm.save_all()
            sm2 = SessionManager(data_dir=td, max_messages=None)
            sm2.load_all()
            raw = sm2.raw_messages("g1")
            self.assertEqual(raw[0].get("name"), "花鱼",
                             "name 是旧格式条目的唯一发言人标识，绝不能丢")

    def test_name_not_sent_to_llm(self):
        """发 LLM 时不带 name（发言人已在前缀里，重复标识会加剧指向混乱）。"""
        with tempfile.TemporaryDirectory() as td:
            sm = SessionManager(data_dir=td, max_messages=None)
            sm.append("g1", "user", "你好", name="花鱼",
                      message_id="m1", sender_uin="848671904")
            sm.save_all()
            sm2 = SessionManager(data_dir=td, max_messages=None)
            sm2.load_all()
            ctx = sm2.get_contexts("g1")
            self.assertNotIn("name", ctx[0], "对外出口必须剥 name")
            self.assertIn("花鱼", ctx[0]["content"], "发言人应在前缀里")

    def test_internal_keys_still_stripped_from_wire(self):
        with tempfile.TemporaryDirectory() as td:
            sm = SessionManager(data_dir=td, max_messages=None)
            sm.append("g1", "assistant", "回复", reasoning="思考",
                      message_id="m1")
            sm.save_all()
            sm2 = SessionManager(data_dir=td, max_messages=None)
            sm2.load_all()
            ctx = sm2.get_contexts("g1")
            for m in ctx:
                self.assertNotIn("_reasoning", m)
                self.assertNotIn("_mid", m)
                self.assertNotIn("_uin", m)
