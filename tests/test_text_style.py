# -*- coding: utf-8 -*-
import unittest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.text_style import (
    clean_message_text, extract_quote, postprocess, collapse_newlines,
    cap_koupi,
)


class TestCleanMessageText(unittest.TestCase):
    def test_strips_quote_and_at_markers(self):
        self.assertEqual(
            clean_message_text("[引用消息(FMDD61: 你好)] [At:123456] 早上好"),
            "早上好")

    def test_keeps_plain(self):
        self.assertEqual(clean_message_text("早上好"), "早上好")


class TestExtractQuote(unittest.TestCase):
    def test_leading_marker(self):
        self.assertEqual(extract_quote("[r:-1]\n明天见"), ("明天见", 1))
        self.assertEqual(extract_quote("[r:-2] 回复内容"), ("回复内容", 2))

    def test_no_marker(self):
        self.assertEqual(extract_quote("普通回复"), ("普通回复", None))

    def test_artifact_not_quote(self):
        self.assertEqual(extract_quote("[回复dog] 成员戌都睡啦"), ("[回复dog] 成员戌都睡啦", None))


class TestPostprocess(unittest.TestCase):
    def test_newline_collapse(self):
        self.assertEqual(
            postprocess("呜哇小明你这个人！\n角色快来管管她呀"),
            "呜哇小明你这个人！角色快来管管她呀")
        self.assertEqual(
            postprocess("嗯…眼熟\n\n你好，报个名吧？"),
            "嗯…眼熟，你好，报个名吧？")

    def test_reply_marker_stripped(self):
        self.assertEqual(postprocess("[r:-1] 你好"), "你好")
        self.assertEqual(postprocess("[回复dog] 成员戌都睡啦"), "成员戌都睡啦")

    def test_ai_phrases_removed(self):
        self.assertNotIn("AI", postprocess("作为一个AI，我可以帮你"))

    def test_koupi_capped_at_2(self):
        """口癖封顶走**外部名单**（真实名单在仓库外 data_out/koupi.json）。"""
        from services import text_style
        text = "测试口癖A测试口癖A测试口癖A，测试口癖B测试口癖B"
        out = text_style.cap_koupi(text, phrases=("测试口癖A", "测试口癖B"))
        self.assertLessEqual(out.count("测试口癖A") + out.count("测试口癖B"), 2)

    def test_emoji_and_at_survive_postprocess_c15(self):
        """🔴 C15 / B-048（2026-09-21）：postprocess **不再**删 emoji 与 @。

        这两步原先是 S0 的预防性收口，代价是提示词 §7【句末的表情】与
        §6「@ 他一句」两处教学**自上线起不可能生效**（实测 bot 输出 465 条：
        含 emoji 0 / 含 @ 0；风格源 2337 条：emoji 112 / @ 50）。
        用户 2026-09-21：「emoji 和 @ 都打开，我们留给 RP 更大的发挥空间」。
        """
        self.assertIn("🤤", postprocess("好饿🤤🤤🤤"))
        self.assertIn("@成员乙", postprocess("@成员乙 肘，咱俩骗钱去"))
        # 但「防泄漏」那几项必须照旧剥掉（标记泄漏到群里就是乱码）
        self.assertNotIn("[r]", postprocess("[r] 对呀"))
        self.assertNotIn("[emote:", postprocess("好耶 [emote:开心比耶]"))
        self.assertNotIn("[poke:", postprocess("戳 [poke:成员乙]"))

    def test_length_and_lines_capped(self):
        out = postprocess("，" * 300 + "。" * 300)
        self.assertLessEqual(len(out), 400)
        long_lines = "\n".join(["line%d" % i for i in range(20)])
        self.assertLessEqual(len(postprocess(long_lines).split("，")), 8 + 1)

    def test_empty(self):
        self.assertEqual(postprocess(""), "")
        self.assertEqual(postprocess("   "), "")


class TestCollapseNewlines(unittest.TestCase):
    def test_join_with_comma(self):
        self.assertEqual(collapse_newlines("甲\n乙"), "甲，乙")
        self.assertEqual(collapse_newlines("甲。\n乙"), "甲。乙")


class TestCapKoupi(unittest.TestCase):
    def test_cap(self):
        # 用**假口癖**验证机制（真实名单在仓库外，代码/夹具里不留真实口癖）
        out = cap_koupi("测试口癖A1测试口癖A2测试口癖A3", phrases=("测试口癖A",))
        self.assertEqual(out.count("测试口癖A"), 2)
        self.assertIn("1", out)
        self.assertIn("3", out)


class TestStripStepsRemovedC15(unittest.TestCase):
    """🔴 C15 / B-048 的**墓志铭**：`strip_emoji()` / `strip_at_mentions()` 已删。

    它们是 S0 的预防性收口，由 `postprocess` 调用 → 提示词 §7【句末的表情】与
    §6「@ 他一句」两处教学**自上线起不可能生效**（实测 bot 输出 465 条：含 emoji 0 /
    含 @ 0；风格源 2337 条：emoji 112 / @ 50）。C15 放开后这两步只剩测试在用，
    2026-09-23 批次三清理**删掉函数**。

    所以本类不再测"函数怎么剥 emoji"（函数没了），而是钉住**它们不该回来**：
    名字不存在 + `postprocess` 的调用图里没有它们。行为侧的断言在
    `TestPostprocess.test_emoji_and_at_survive_postprocess_c15`。
    """

    def test_functions_no_longer_exist(self):
        from services import text_style
        self.assertFalse(hasattr(text_style, "strip_emoji"),
                         "strip_emoji 回来了 —— C15/B-048 的账会重开")
        self.assertFalse(hasattr(text_style, "strip_at_mentions"),
                         "strip_at_mentions 回来了 —— §6「@ 他一句」又会失效")

    def test_postprocess_does_not_call_them(self):
        """AST 看**调用图**，不看注释/docstring（那里故意留着历史说明）。"""
        import ast
        import inspect
        import textwrap
        from services import text_style
        tree = ast.parse(textwrap.dedent(inspect.getsource(text_style.postprocess)))
        called = {n.func.id for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertNotIn("strip_emoji", called)
        self.assertNotIn("strip_at_mentions", called)


if __name__ == "__main__":
    unittest.main()
