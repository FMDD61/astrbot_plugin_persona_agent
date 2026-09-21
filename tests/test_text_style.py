# -*- coding: utf-8 -*-
import unittest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.text_style import (
    clean_message_text, extract_quote, postprocess, collapse_newlines,
    cap_koupi, strip_emoji,
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
            postprocess("嗯…眼熟\n\n口癖己，报个名吧？"),
            "嗯…眼熟，口癖己，报个名吧？")

    def test_reply_marker_stripped(self):
        self.assertEqual(postprocess("[r:-1] 你好"), "你好")
        self.assertEqual(postprocess("[回复dog] 成员戌都睡啦"), "成员戌都睡啦")

    def test_ai_phrases_removed(self):
        self.assertNotIn("AI", postprocess("作为一个AI，我可以帮你"))

    def test_koupi_capped_at_2(self):
        text = "口癖己口癖己口癖己，口癖丁口癖丁口癖丁，口癖庚"
        out = postprocess(text)
        self.assertLessEqual(out.count("口癖己"), 2)

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
        out = cap_koupi("口癖己a口癖己b口癖己c")
        self.assertEqual(out.count("口癖己"), 2)
        self.assertIn("a", out)
        self.assertIn("c", out)


class TestStripEmoji(unittest.TestCase):
    def test_strip(self):
        self.assertEqual(strip_emoji("a🤤b😭c"), "abc")


if __name__ == "__main__":
    unittest.main()
