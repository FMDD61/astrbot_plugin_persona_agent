# -*- coding: utf-8 -*-
"""图像预处理 + 视觉 JSON 解析（S6 标定改造的回归防线）。

## 背景（2026-09-14，956 次实测标定）

**① 不降采样 = 白烧 7 倍时间，零额外信息。** 同一张图 A/B：prompt_tokens 完全相同
（412 vs 412，网关侧图像 token 数固定），时延 4.0s vs 29.6s（最坏 127.5s）。

**② 自由文本 prompt → 模型把预算花在"格式谈判"上。** 191/191 条空 content 的
响应 `finish_reason` 全是 `length`、`reasoning_tokens ≈ max_tokens`，正文一字未出；
描述可用率仅 31.2%。约定 JSON schema 后 reasoning 降到 p50≈150，可用率 **100%**。

**③ C27（2026-09-21）：GIF 一律抽帧拼网格。** 旧的体积闸门
（`GIF_INLINE_MAX_BYTES`）把 >1.5MB 的多帧 GIF 降级成**首帧 JPEG** → 动作语义丢失
（B-049 的根因；用户实测群内 GIF 普遍 >3MB，那条路几乎从不触发）。
现在 6 帧 / 每帧 320px / 3 列×2 行（960×640）/ JPEG q82，**体积闸门已废**。
"""
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.image_prep import (  # noqa: E402
    mime_of, prepare_bytes_for_vision, prepare_for_vision, sniff_mime,
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


#: 12 个互相分得开的纯色 —— 用来**反查网格里放的是第几帧**。
_PALETTE = [(230, 30, 30), (30, 200, 30), (30, 30, 230), (230, 205, 20),
            (230, 30, 230), (30, 200, 200), (125, 60, 20), (250, 140, 200),
            (80, 80, 80), (205, 250, 120), (10, 90, 160), (150, 10, 90)]


def _nearest_palette(px):
    """把（被 JPEG 轻微改过的）像素映射回最近的调色板下标。"""
    return min(range(len(_PALETTE)),
               key=lambda i: sum((px[k] - _PALETTE[i][k]) ** 2 for k in range(3)))


def _anim_gif_bytes(n_frames=12, size=(400, 300)) -> bytes:
    """真实可解码的多帧 GIF：**第 i 帧 = _PALETTE[i]**（>12 帧则循环取色）。"""
    from PIL import Image
    ims = [Image.new("RGB", size, _PALETTE[i % len(_PALETTE)])
           for i in range(n_frames)]
    buf = io.BytesIO()
    ims[0].save(buf, format="GIF", save_all=True, append_images=ims[1:],
                duration=80, loop=0)
    return buf.getvalue()


def _noise_gif_bytes(n_frames=16, size=(360, 360)) -> bytes:
    """**>1.5MB** 的多帧 GIF（随机调色板索引 → LZW 压不动）。

    专门用来钉住"体积闸门已废"：旧代码看 `len(data) <= GIF_INLINE_MAX_BYTES`，
    这个体积**必须**仍然走网格。
    """
    from PIL import Image
    frames = []
    for _ in range(n_frames):
        im = Image.frombytes("P", size, os.urandom(size[0] * size[1]))
        im.putpalette(os.urandom(256 * 3))
        frames.append(im)
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:],
                   duration=40, loop=0)
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
    def test_animation_detection_has_one_outlet(self):
        """**"是不是动图"只有一个出口** = `prepare_for_vision` 的 `meta`。

        `frame_count()` 已删（2026-09-23 批次三清理：全仓零生产引用，只有测试在调；
        离线工具自带一份同名实现）—— 想数帧就读 `meta["gif_grid"]["of"]`。
        这条用例把"三帧 GIF / 静图 / 打不开的文件"三种情形一次性钉在行为上，
        免得有人为了测试再添一份口径（两份实现必然漂移）。
        """
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            anim = Path(td) / "a.gif"
            anim.write_bytes(_img_bytes("GIF", frames=3))
            meta = {}
            prepare_for_vision(anim, meta=meta)
            self.assertEqual(meta["gif_grid"]["of"], 3, "多帧 GIF：帧数从 meta 读")
            self.assertEqual(meta["path"], "gif_grid")

            still = Path(td) / "b.png"
            still.write_bytes(_img_bytes())
            meta2 = {}
            prepare_for_vision(still, meta=meta2)
            self.assertNotIn("gif_grid", meta2)
            self.assertEqual(meta2["path"], "static", "静图不该进网格")

            meta3 = {}
            out, mime = prepare_for_vision(Path(td) / "missing.gif", meta=meta3)
            self.assertEqual((out, mime), (b"", "image/png"))
            self.assertIn("read_fail", meta3, "打不开要留痕（旧 frame_count 返回 0）")


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
            meta = {}
            out, mime = prepare_for_vision(p, meta=meta)
            # C27：路径版（离线入库工具走这条）也必须拼网格，否则离线/线上两套漂移
            self.assertEqual(mime, "image/jpeg")
            self.assertNotEqual(out, p.read_bytes())
            self.assertTrue(meta.get("gif_grid"))
            self.assertEqual(meta["path"], "gif_grid")


class TestGifGridC27(unittest.TestCase):
    """C27：**全部** GIF 抽帧拼网格（6 帧 / 每帧 320px / 3 列×2 行 / JPEG q82）。

    钉住三件事：① 帧数与帧号（均匀抽样，含首尾）；② 网格几何（960×640、每格 320、
    超出只缩不放）；③ **体积闸门已废** —— 1.5MB 以上的动图也必须走网格。
    """

    def test_layout_constants_match_spec(self):
        from services import image_prep as P
        self.assertEqual(P.GIF_GRID_FRAMES, 6)
        self.assertEqual(P.GIF_GRID_FRAME_EDGE, 320)
        self.assertEqual(P.GIF_GRID_COLS, 3)
        self.assertEqual(P.GIF_GRID_ROWS, 2)
        self.assertEqual(P.GIF_GRID_JPEG_QUALITY, 82)
        self.assertEqual(P.GIF_GRID_COLS * P.GIF_GRID_ROWS, P.GIF_GRID_FRAMES)

    def test_uniform_frame_indices_include_both_ends(self):
        from services.image_prep import gif_frame_indices
        self.assertEqual(gif_frame_indices(109, 6), [0, 22, 43, 65, 86, 108])
        self.assertEqual(gif_frame_indices(12, 6), [0, 2, 4, 7, 9, 11])
        self.assertEqual(gif_frame_indices(6, 6), [0, 1, 2, 3, 4, 5])
        self.assertEqual(gif_frame_indices(2, 6), [0, 1])          # 帧不够 → 全取
        self.assertEqual(gif_frame_indices(1, 6), [0])
        self.assertEqual(gif_frame_indices(0, 6), [])
        # 严格递增（不许抽样抽样出重复帧号 —— 那会让同一帧占两格）
        for n in (3, 7, 17, 109, 400):
            idx = gif_frame_indices(n, 6)
            self.assertEqual(idx, sorted(set(idx)))
            self.assertTrue(all(0 <= i < n for i in idx))

    @requires_pil
    def test_grid_is_2x3_of_320_cells(self):
        from PIL import Image
        gif = _anim_gif_bytes(12, (400, 300))
        out, mime = prepare_bytes_for_vision(gif)
        self.assertEqual(mime, "image/jpeg")
        with Image.open(io.BytesIO(out)) as im:
            self.assertEqual(im.format, "JPEG")
            self.assertEqual(im.size, (960, 640))         # 3 列 × 2 行 × 320

    @requires_pil
    def test_grid_holds_six_sampled_frames_in_order(self):
        """每格放的必须是**抽样帧本身**（用纯色反查帧号）。"""
        from PIL import Image
        gif = _anim_gif_bytes(12, (400, 300))
        out, _ = prepare_bytes_for_vision(gif)
        want = [0, 2, 4, 7, 9, 11]                        # gif_frame_indices(12, 6)
        with Image.open(io.BytesIO(out)) as im:
            rgb = im.convert("RGB")
            for slot, frame_no in enumerate(want):
                cx = (slot % 3) * 320 + 160
                cy = (slot // 3) * 320 + 160
                got = _nearest_palette(rgb.getpixel((cx, cy)))
                self.assertEqual(
                    got, frame_no % len(_PALETTE),
                    f"第 {slot} 格（{cx},{cy}）应是第 {frame_no} 帧的颜色，实得 "
                    f"{rgb.getpixel((cx, cy))}")

    @requires_pil
    def test_each_frame_long_edge_capped_at_320_and_centered(self):
        """400×300 帧 → 320×240，竖直居中（上下留白），**不放大**。"""
        from PIL import Image
        gif = _anim_gif_bytes(6, (400, 300))
        out, _ = prepare_bytes_for_vision(gif)
        with Image.open(io.BytesIO(out)) as im:
            rgb = im.convert("RGB")
            self.assertEqual(rgb.getpixel((160, 20)), (255, 255, 255), "上留白")
            self.assertEqual(rgb.getpixel((160, 300)), (255, 255, 255), "下留白")
            self.assertNotEqual(rgb.getpixel((160, 160)), (255, 255, 255), "帧本体")
            # 帧占满整格宽（400 的长边被压到 320）
            self.assertNotEqual(rgb.getpixel((5, 160)), (255, 255, 255))

    @requires_pil
    def test_small_frame_is_not_upscaled(self):
        """小帧居中留白，**不放大**（放大不加信息、只加字节）。"""
        from PIL import Image
        gif = _anim_gif_bytes(6, (100, 80))
        out, _ = prepare_bytes_for_vision(gif)
        with Image.open(io.BytesIO(out)) as im:
            rgb = im.convert("RGB")
            self.assertEqual(rgb.getpixel((20, 20)), (255, 255, 255), "左上留白")
            self.assertEqual(_nearest_palette(rgb.getpixel((160, 160))), 0,
                             "首帧应居中（且没有被放大）")

    @requires_pil
    def test_transparent_gif_composited_on_white(self):
        """透明像素合成到**白底**：JPEG 无 alpha，直接 convert 会变黑（假信息）。"""
        from PIL import Image, ImageDraw
        frames = []
        for color in ((255, 0, 0, 255), (0, 0, 255, 255)):
            im = Image.new("RGBA", (320, 320), (0, 0, 0, 0))
            ImageDraw.Draw(im).rectangle([110, 110, 209, 209], fill=color)
            frames.append(im)
        buf = io.BytesIO()
        frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:],
                       duration=80, loop=0)
        out, mime = prepare_bytes_for_vision(buf.getvalue())
        self.assertEqual(mime, "image/jpeg")
        with Image.open(io.BytesIO(out)) as im:
            rgb = im.convert("RGB")
            self.assertEqual(rgb.getpixel((5, 5)), (255, 255, 255), "透明区应为白底")
            r, g, b = rgb.getpixel((160, 160))
            self.assertGreater(r, 180)
            self.assertLess(g, 90)

    @requires_pil
    def test_fewer_frames_than_six_trims_blank_row(self):
        """帧数 <6 的 GIF（实测占真实贴纸的 12%）→ 画布按**实际用到的行数**收窄。"""
        from PIL import Image
        meta = {}
        out, mime = prepare_bytes_for_vision(_anim_gif_bytes(2, (400, 300)), meta=meta)
        self.assertEqual(mime, "image/jpeg")
        with Image.open(io.BytesIO(out)) as im:
            self.assertEqual(im.size, (960, 320), "2 帧只占 1 行，别留整行空白")
        self.assertEqual(meta["gif_grid"]["frames"], 2)
        self.assertEqual(meta["gif_grid"]["rows"], 1)
        self.assertEqual(meta["gif_grid"]["of"], 2)

    @requires_pil
    def test_volume_gate_is_gone(self):
        """🔴 反向断言：**>1.5MB 的多帧 GIF 也必须拼网格**（B-049 的根因）。

        旧行为：`len(data) <= GIF_INLINE_MAX_BYTES` 才整图直送，否则**首帧 JPEG**
        → 动作语义丢失。这里用 LZW 压不动的随机帧把体积顶到 1.5MB 以上。
        """
        gif = _noise_gif_bytes()
        self.assertGreater(len(gif), 1_500_000, "样例本身必须超过旧闸门")
        meta = {}
        out, mime = prepare_bytes_for_vision(gif, meta=meta)
        self.assertEqual(mime, "image/jpeg")
        self.assertNotEqual(out, gif, "不得整图直送")
        self.assertTrue(meta.get("gif_grid"), "必须走网格")
        self.assertFalse(meta.get("gif_grid_fail"))
        from PIL import Image
        with Image.open(io.BytesIO(out)) as im:
            self.assertEqual(im.size, (960, 640))

    @requires_pil
    def test_volume_gate_constant_is_deleted(self):
        """🔴 **墓志铭**：体积闸门常量已删，行为里没有任何体积分支。

        旧行为（B-049 的根因）：`len(data) <= GIF_INLINE_MAX_BYTES` 才整图直送，
        否则降级成**首帧 JPEG** → 动作语义丢失。C27 废掉分流后这个常量只剩墓碑
        （注释还声称"保留给 `tools/build_sticker_index.py` 的 import"，而那个 import
        早已删掉、全仓零引用）→ 2026-09-23 批次三清理连名字一起删。

        反向断言两件事：① 名字不许回来（回来了 = 又有人想按体积分流）；
        ② 行为不看体积（`test_volume_gate_is_gone` 用 >1.5MB 的样例钉住那一半）。
        """
        from services import image_prep as P
        self.assertFalse(hasattr(P, "GIF_INLINE_MAX_BYTES"),
                         "体积闸门常量回来了 —— C27/B-049 的账会重开")
        gif = _anim_gif_bytes(8, (400, 300))
        meta = {}
        out, mime = prepare_bytes_for_vision(gif, meta=meta)
        self.assertEqual(mime, "image/jpeg")
        self.assertNotEqual(out, gif, "不得整图直送")
        self.assertTrue(meta.get("gif_grid"), "多帧 GIF 一律走网格")

    @requires_pil
    def test_non_gif_unaffected(self):
        """非 GIF 的静图路径原样（PNG 仍走 768px 降采样 JPEG）。"""
        from PIL import Image
        png = _img_bytes(size=(2000, 1000))
        meta = {}
        out, mime = prepare_bytes_for_vision(png, meta=meta)
        self.assertEqual(mime, "image/jpeg")
        self.assertNotIn("gif_grid", meta)
        self.assertNotIn("gif_grid_fail", meta)
        self.assertEqual(meta.get("path"), "static")
        with Image.open(io.BytesIO(out)) as im:
            self.assertEqual(max(im.size), 768)


class TestGifGridDegradeC27(unittest.TestCase):
    """降级**必须留痕**：拼不出来时，理由要进 meta（线上再落到 diag/stats/log）。"""

    def test_undecodable_gif_records_grid_fail(self):
        fake_gif = b"GIF89a" + b"\x00" * 40          # 魔数对但解不开
        meta = {}
        out, mime = prepare_bytes_for_vision(fake_gif, meta=meta)
        self.assertEqual(out, fake_gif)              # 回退原字节（既有行为）
        self.assertEqual(mime, "image/gif")
        self.assertTrue(meta.get("gif_grid_fail"), "解码失败必须留下原因")
        self.assertEqual(meta.get("path"), "gif_firstframe_fallback")

    def test_no_pil_records_grid_fail(self):
        import sys
        saved = sys.modules.get("PIL")
        sys.modules["PIL"] = None                    # 模拟无 Pillow 的生产环境
        try:
            gif = b"GIF89a" + b"\x00" * 40
            meta = {}
            prepare_bytes_for_vision(gif, meta=meta)
        finally:
            if saved is None:
                sys.modules.pop("PIL", None)
            else:
                sys.modules["PIL"] = saved
        self.assertIn("PIL", meta.get("gif_grid_fail", ""))

    @requires_pil
    def test_single_frame_gif_is_not_a_degradation(self):
        """单帧 GIF 不是"降级"—— 它本来就该走静图路径，不许报 fail。"""
        gif = _img_bytes("GIF", size=(1200, 900), frames=1)
        meta = {}
        out, mime = prepare_bytes_for_vision(gif, meta=meta)
        self.assertEqual(mime, "image/jpeg")
        self.assertNotIn("gif_grid", meta)
        self.assertNotIn("gif_grid_fail", meta, "单帧不是失败")
        self.assertEqual(meta.get("path"), "static")

    @requires_pil
    def test_grid_failure_falls_back_to_first_frame_jpeg(self):
        """能解码但 seek 失败的那类坏 GIF → 回退首帧 JPEG（且留痕）。"""
        from services import image_prep as P
        gif = _anim_gif_bytes(6, (400, 300))
        orig = P.gif_frame_indices
        P.gif_frame_indices = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        meta = {}
        try:
            out, mime = prepare_bytes_for_vision(gif, meta=meta)
        finally:
            P.gif_frame_indices = orig
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(meta.get("path"), "gif_firstframe_fallback")
        self.assertIn("boom", meta.get("gif_grid_fail", ""))
        from PIL import Image
        with Image.open(io.BytesIO(out)) as im:
            self.assertLessEqual(max(im.size), 768)   # 首帧降采样，不是网格


class TestGifPromptFrameCountC27(unittest.TestCase):
    """提示词必须报**这次实际**的帧数（12% 的真实 GIF 不足 6 帧，会留白）。"""

    def test_default_geometry_prompt_computed_on_demand(self):
        """默认几何（6 帧 / 3 列 × 2 行）那一版**现算**，没有预求值常量。

        ⚠️ 曾经有 `VISION_GIF_SYSTEM_PROMPT = gif_grid_system_prompt(6)`，生产零引用
        （线上/离线都按本轮实际帧数现算），2026-09-23 已删 —— 别再加第二份口径：
        短 GIF（实测占真实贴纸 12%）拿写死"共 6 帧"的提示词会去解释空白格。
        """
        from services import vision as V
        from services.vision import gif_grid_system_prompt as G
        D = G(6, 3, 2)
        self.assertIn("共 6 帧", D)
        self.assertIn("3 列 × 2 行", D)
        self.assertNotIn("空白", D, "满格时不该提空白格")
        self.assertFalse(hasattr(V, "VISION_GIF_SYSTEM_PROMPT"),
                         "预求值常量回来了 —— 它就等于 gif_grid_system_prompt(6,3,2)")

    def test_short_gif_prompt_reports_real_count_and_blanks(self):
        from services.vision import gif_grid_system_prompt as G
        p = G(2, 3, 1)
        self.assertIn("共 2 帧", p)
        self.assertNotIn("共 6 帧", p)
        self.assertIn("3 列 × 1 行", p)
        self.assertIn("空白", p)
        # 输出契约不能丢（S6 的唯一主导因素）
        for kw in ('"desc"', '"tags"', "JSON", "动图", "顺序"):
            self.assertIn(kw, p)


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
