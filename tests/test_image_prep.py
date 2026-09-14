# -*- coding: utf-8 -*-
"""图像预处理 + 视觉 JSON 解析（S6 标定改造的回归防线）。

## 背景（2026-09-14，956 次实测标定）

**① 不降采样 = 白烧 7 倍时间，零额外信息。** 同一张图 A/B：prompt_tokens 完全相同
（412 vs 412，网关侧图像 token 数固定），时延 4.0s vs 29.6s（最坏 127.5s）。

**② 自由文本 prompt → 模型把预算花在"格式谈判"上。** 191/191 条空 content 的
响应 `finish_reason` 全是 `length`、`reasoning_tokens ≈ max_tokens`，正文一字未出；
描述可用率仅 31.2%。约定 JSON schema 后 reasoning 降到 p50≈150，可用率 **100%**。
"""
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.image_prep import (  # noqa: E402
    frame_count, mime_of, prepare_bytes_for_vision, prepare_for_vision, sniff_mime,
)


try:                                          # 系统 python 无 PIL（生产/台式机有）
    import PIL  # noqa: F401
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

requires_pil = unittest.skipUnless(HAS_PIL, "本环境无 Pillow（生产/台式机有）")


def _img_bytes(fmt="PNG", size=(600, 400), frames=1) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    if frames > 1:
        ims = [Image.new("RGB", size, (200 - i * 20, 40 + i * 20, 90))
               for i in range(frames)]
        ims[0].save(buf, format="GIF", save_all=True, append_images=ims[1:],
                    duration=80, loop=0)
    else:
        Image.new("RGB", size, (120, 180, 240)).save(buf, format=fmt)
    return buf.getvalue()


class TestSniffAndMeta(unittest.TestCase):
    def test_sniff_magic(self):
        self.assertEqual(sniff_mime(b"GIF89a...."), "image/gif")
        self.assertEqual(sniff_mime(b"\xff\xd8\xff\xe0xxx"), "image/jpeg")
        self.assertEqual(sniff_mime(b"\x89PNG\r\n\x1a\nxx"), "image/png")

    def test_mime_of_ext(self):
        self.assertEqual(mime_of("a.GIF"), "image/gif")
        self.assertEqual(mime_of("a.jpeg"), "image/jpeg")
        self.assertEqual(mime_of("a.unknown"), "image/png")

    @requires_pil
    def test_frame_count(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.gif"
            p.write_bytes(_img_bytes("GIF", frames=3))
            self.assertEqual(frame_count(p), 3)
            p2 = Path(td) / "b.png"
            p2.write_bytes(_img_bytes())
            self.assertEqual(frame_count(p2), 1)
            self.assertEqual(frame_count(Path(td) / "missing.gif"), 0)


class TestDownscale(unittest.TestCase):
    """核心：长边超限必须被压下来（这是 7 倍时延的来源）。"""

    @requires_pil
    def test_large_image_is_shrunk_a_lot(self):
        from PIL import Image
        big = _img_bytes(size=(3000, 2000))
        out, mime = prepare_bytes_for_vision(big)
        self.assertEqual(mime, "image/jpeg")
        self.assertLess(len(out), len(big) / 5, "3000px 图应被显著压缩")
        with Image.open(io.BytesIO(out)) as im:
            self.assertEqual(max(im.size), 768)

    @requires_pil
    def test_small_image_not_upscaled(self):
        from PIL import Image
        small = _img_bytes(size=(200, 150))
        out, _ = prepare_bytes_for_vision(small)
        with Image.open(io.BytesIO(out)) as im:
            self.assertEqual(im.size, (200, 150))

    @requires_pil
    def test_multiframe_gif_sent_inline(self):
        """多帧 GIF ≤ 闸门 → **整图直送**（保留动作语义）。"""
        gif = _img_bytes("GIF", size=(64, 64), frames=4)
        out, mime = prepare_bytes_for_vision(gif)
        self.assertEqual(mime, "image/gif")
        self.assertEqual(out, gif)

    @requires_pil
    def test_single_frame_gif_downscaled(self):
        """单帧 GIF 走降采样路径（不是整图直送）。

        断言**行为**而非体积：GIF 的调色板压缩对纯色小图比 JPEG 更省，
        所以"转 JPEG 后一定更小"是伪命题（实测 1942B GIF → 2878B JPEG）。
        真正要保证的是：mime 变成 jpeg、且尺寸被限制到闸门内。
        """
        gif = _img_bytes("GIF", size=(1200, 900), frames=1)
        out, mime = prepare_bytes_for_vision(gif)
        self.assertEqual(mime, "image/jpeg")
        from PIL import Image
        with Image.open(io.BytesIO(out)) as im:
            self.assertEqual(max(im.size), 768)
            self.assertEqual(im.format, "JPEG")

    def test_no_pil_path_labels_by_magic(self):
        """无 PIL 时 `to_jpeg` 回退原字节 → mime 必须按魔数标注（不谎称 jpeg）。"""
        jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 20
        out, mime = prepare_bytes_for_vision(jpg)
        self.assertEqual(mime, "image/jpeg")
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
        out2, mime2 = prepare_bytes_for_vision(png)
        self.assertEqual(mime2, "image/png")

    def test_undecodable_bytes_fall_back_with_correct_mime(self):
        """回归：转换失败回退原字节时，**mime 必须按真实内容标注**。

        曾经的写法是盲目返回 "image/jpeg"，PIL 缺失/解码失败时标签就与内容不符。
        """
        fake_gif = b"GIF89a" + b"\x00" * 40          # 魔数对但解不开
        out, mime = prepare_bytes_for_vision(fake_gif)
        self.assertEqual(out, fake_gif)
        self.assertEqual(mime, "image/gif")           # 按魔数，而不是 jpeg

    def test_empty_bytes(self):
        self.assertEqual(prepare_bytes_for_vision(b""), (b"", "image/png"))

    @requires_pil
    def test_prepare_for_vision_from_path(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.gif"
            p.write_bytes(_img_bytes("GIF", size=(64, 64), frames=3))
            out, mime = prepare_for_vision(p)
            self.assertEqual(mime, "image/gif")
            self.assertEqual(out, p.read_bytes())
            # 大动图超闸门 → 回退首帧降采样
            from services import image_prep
            orig = image_prep.GIF_INLINE_MAX_BYTES
            image_prep.GIF_INLINE_MAX_BYTES = 10
            try:
                out2, mime2 = prepare_for_vision(p)
                self.assertEqual(mime2, "image/jpeg")
                self.assertLess(len(out2), len(p.read_bytes()))
            finally:
                image_prep.GIF_INLINE_MAX_BYTES = orig


class TestParseVisionJson(unittest.TestCase):
    """结构化解析：实测在 442 条真实响应上 441/442 = 99.8% 成功。"""

    def _p(self, content=None, reasoning=None):
        from services.vision import parse_vision_json
        msg = {}
        if content is not None:
            msg["content"] = content
        if reasoning is not None:
            msg["reasoning"] = reasoning
        return parse_vision_json({"choices": [{"message": msg}]})

    def test_plain_json(self):
        d, t = self._p('{"desc": "一只猫在睡觉", "tags": ["可爱", "睡觉"]}')
        self.assertEqual(d, "一只猫在睡觉")
        self.assertEqual(t, ["可爱", "睡觉"])

    def test_markdown_fence_stripped(self):
        d, t = self._p('```json\n{"desc": "狗在跑", "tags": ["奔跑"]}\n```')
        self.assertEqual(d, "狗在跑")
        self.assertEqual(t, ["奔跑"])

    def test_json_with_surrounding_chatter(self):
        d, _ = self._p('好的，结果如下：{"desc": "鸟在飞", "tags": ["飞翔"]} 以上。')
        self.assertEqual(d, "鸟在飞")

    def test_broken_json_falls_back_to_desc_regex(self):
        d, t = self._p('{"desc": "残缺的JSON", "tags": [')
        self.assertEqual(d, "残缺的JSON")
        self.assertEqual(t, [])

    def test_falls_back_to_reasoning(self):
        d, t = self._p("", '{"desc": "从思考里捞出来的", "tags": ["测试"]}')
        self.assertEqual(d, "从思考里捞出来的")
        self.assertEqual(t, ["测试"])

    def test_tags_capped_at_four(self):
        _, t = self._p('{"desc": "x", "tags": ["a","b","c","d","e","f"]}')
        self.assertEqual(len(t), 4)

    def test_leaked_prompt_rejected(self):
        d, _ = self._p('{"desc": "用户说不要脑补，不超过80字", "tags": ["x"]}')
        self.assertEqual(d, "")

    def test_all_empty(self):
        self.assertEqual(self._p("", ""), ("", []))
        self.assertEqual(self._p(), ("", []))

    def test_system_prompt_has_json_schema(self):
        """约定固定格式是唯一主导因素 —— 提示词必须写明 JSON 结构。"""
        from services.vision import VISION_SYSTEM_PROMPT
        self.assertIn('"desc"', VISION_SYSTEM_PROMPT)
        self.assertIn('"tags"', VISION_SYSTEM_PROMPT)
        self.assertIn("JSON", VISION_SYSTEM_PROMPT)
        self.assertIn("动图", VISION_SYSTEM_PROMPT)   # 动图要看动作


if __name__ == "__main__":
    unittest.main()
