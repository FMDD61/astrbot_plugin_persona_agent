# -*- coding: utf-8 -*-
import asyncio
import base64
import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.vision import (  # noqa: E402
    VISION_GIF_SYSTEM_PROMPT, VISION_SYSTEM_PROMPT, VisionService, face_name,
    sniff_mime, resolve_image_bytes,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 16


def _real_gif(frames: int = 2) -> bytes:
    """真实的可解码多帧 GIF —— 新逻辑要求"多帧才整图直送"，假魔数不够。

    同时这也测到了 `image_prep` 的分流：假字节无法解码 → 回退原字节 + 按魔数标注。
    """
    try:
        from PIL import Image
    except ImportError:                       # 无 PIL 环境下退回假魔数
        return b"GIF89a" + b"\x00" * 32
    import io as _io
    frames_list = []
    for i in range(frames):
        im = Image.new("RGB", (8, 8), (200 - i * 40, 40 + i * 40, 90))
        frames_list.append(im)
    buf = _io.BytesIO()
    frames_list[0].save(buf, format="GIF", save_all=True,
                        append_images=frames_list[1:], duration=100, loop=0)
    return buf.getvalue()


GIF = _real_gif(2)


class TestFaceName(unittest.TestCase):
    def test_known(self):
        self.assertEqual(face_name(13), "呲牙")
        self.assertEqual(face_name(65), "爱心")

    def test_unknown(self):
        self.assertEqual(face_name(998877), "表情#998877")
        self.assertEqual(face_name("abc"), "表情#abc")


class TestSniffMime(unittest.TestCase):
    def test_types(self):
        self.assertEqual(sniff_mime(PNG), "image/png")
        self.assertEqual(sniff_mime(GIF), "image/gif")
        self.assertEqual(sniff_mime(JPG), "image/jpeg")


class TestResolveBytes(unittest.TestCase):
    def test_base64_fallback(self):
        class StubImage:
            file = "base64://" + base64.b64encode(PNG).decode()

            async def convert_to_file_path(self):
                raise RuntimeError("no local path")

        async def go():
            return await resolve_image_bytes(StubImage())

        data = asyncio.run(go())
        self.assertEqual(data, PNG)

    def test_none(self):
        class StubImage:
            file = ""

            async def convert_to_file_path(self):
                raise RuntimeError("no")

        async def go():
            return await resolve_image_bytes(StubImage())

        self.assertIsNone(asyncio.run(go()))


class TestVisionService(unittest.TestCase):
    def _mk(self, post=None):
        return VisionService("https://x/v1", "k", "mimo-v2.5",
                             timeout=15.0, cache_ttl=30.0, http_post=post)

    def test_describe_and_cache(self):
        calls = []

        async def post(url, payload):
            calls.append(payload)
            return {"choices": [{"message": {"content": '{"desc": "一只猫在吃草莓麻薯", "tags": ["猫"]}'}}]}

        async def go():
            v = self._mk(post)
            d1 = await v.describe_bytes(PNG)
            d2 = await v.describe_bytes(PNG)
            return d1, d2, len(calls)

        d1, d2, n = asyncio.run(go())
        self.assertEqual(d1, "一只猫在吃草莓麻薯")
        self.assertEqual(d2, "一只猫在吃草莓麻薯")
        self.assertEqual(n, 1)  # cache hit

    def test_payload_has_data_url_and_model(self):
        captured = {}

        async def post(url, payload):
            captured['url'] = url
            captured['model'] = payload['model']
            captured['img'] = payload['messages'][1]['content'][1]['image_url']['url']
            return {"choices": [{"message": {"content": '{"desc": "ok", "tags": ["猫"]}'}}]}

        async def go():
            v = self._mk(post)
            await v.describe_bytes(PNG)          # C27 起 GIF 不再是"原样直送"，用静图断言
            return captured

        c = asyncio.run(go())
        self.assertEqual(c['url'], "https://x/v1/chat/completions")
        self.assertEqual(c['model'], "mimo-v2.5")
        self.assertTrue(c['img'].startswith("data:image/png;base64,"))

    def test_timeout_fallback_none(self):
        async def post(url, payload):
            await asyncio.sleep(1.0)
            return {"choices": [{"message": {"content": '{"desc": "x", "tags": ["猫"]}'}}]}

        async def go():
            v = VisionService("https://x/v1", "k", "mimo-v2.5",
                              timeout=0.05, cache_ttl=30.0, http_post=post)
            return await v.describe_bytes(PNG)

        self.assertIsNone(asyncio.run(go()))

    def test_empty_desc_fallback_none(self):
        async def post(url, payload):
            return {"choices": [{"message": {"content": '{"desc": "  ", "tags": ["猫"]}'}}]}

        async def go():
            v = self._mk(post)
            return await v.describe_bytes(PNG)

        self.assertIsNone(asyncio.run(go()))


class TestVisionDiagnostics(unittest.TestCase):
    """S0：`无法识别` 是**一个结果、四种成因**，必须分得开。

    2026-09-13 实测部署后仍有 5/14 次失败，而旧代码四条路径全是
    `return None` → 调用方只看到「无法识别」，查不出是哪一层。
    """

    def test_timeout_reason_recorded(self):
        async def post(url, payload):
            await asyncio.sleep(1.0)
            return {"choices": [{"message": {"content": '{"desc": "x", "tags": ["猫"]}'}}]}

        async def go():
            v = VisionService("https://x/v1", "k", "m",
                              timeout=0.05, cache_ttl=30.0, http_post=post)
            d = {}
            out = await v.describe_bytes(PNG, diag=d)
            return v, d, out

        v, diag, out = asyncio.run(go())
        self.assertIsNone(out)
        self.assertIn("TimeoutError", v.last_error or "")
        self.assertEqual(v.stats["timeout"], 1)
        self.assertIn("TimeoutError", diag.get("vision_fail", ""))

    def test_empty_completion_reason_recorded(self):
        async def post(url, payload):
            return {"choices": [{"message": {"content": '{"desc": "", "tags": ["猫"]}'}}]}

        async def go():
            v = self._mk(post) if hasattr(self, "_mk") else VisionService(
                "https://x/v1", "k", "m", http_post=post)
            d = {}
            out = await v.describe_bytes(PNG, diag=d)
            return v, d, out

        v, diag, out = asyncio.run(go())
        self.assertIsNone(out)
        self.assertIn("empty completion", v.last_error or "")
        self.assertEqual(v.stats["empty"], 1)

    def test_api_error_reason_recorded(self):
        async def post(url, payload):
            raise RuntimeError("HTTP 400 unsupported_model")

        async def go():
            v = VisionService("https://x/v1", "k", "bad-model", http_post=post)
            d = {}
            out = await v.describe_bytes(PNG, diag=d)
            return v, d, out

        v, diag, out = asyncio.run(go())
        self.assertIsNone(out)
        self.assertIn("unsupported_model", v.last_error or "")
        self.assertEqual(v.stats["error"], 1)

    def test_empty_bytes_reason_recorded(self):
        async def go():
            v = VisionService("https://x/v1", "k", "m")
            d = {}
            out = await v.describe_bytes(b"", diag=d)
            return v, d, out

        v, diag, out = asyncio.run(go())
        self.assertIsNone(out)
        self.assertEqual(v.last_error, "empty bytes")

    def test_resolve_failure_is_distinct_from_model_failure(self):
        """取字节失败 vs 模型失败 —— 两条完全不同的路，成因必须可辨识。"""
        class NoFileImg:
            file = ""
            async def convert_to_file_path(self):
                raise RuntimeError("media resolver exploded")

        async def go():
            v = VisionService("https://x/v1", "k", "m")
            d = {}
            out = await v.describe_image(NoFileImg(), diag=d)
            return v, d, out

        v, diag, out = asyncio.run(go())
        self.assertIsNone(out)
        self.assertIn("convert_to_file_path", v.last_error or "")
        self.assertTrue(diag.get("resolve_fail"))
        self.assertNotIn("vision_fail", diag)   # 压根没走到模型那一步

    def test_success_records_ok_and_diag(self):
        async def post(url, payload):
            return {"choices": [{"message": {"content": '{"desc": "一张猫猫图", "tags": ["猫"]}'}}]}

        async def go():
            v = VisionService("https://x/v1", "k", "m", http_post=post)
            d = {}
            out = await v.describe_bytes(PNG, diag=d)
            return v, d, out

        v, diag, out = asyncio.run(go())
        self.assertEqual(out, "一张猫猫图")
        self.assertEqual(v.stats["ok"], 1)
        self.assertIsNone(v.last_error)
        self.assertEqual(diag.get("cache"), "miss")
        self.assertIn("hash", diag)

    def test_cache_hit_recorded(self):
        async def post(url, payload):
            return {"choices": [{"message": {"content": '{"desc": "图", "tags": ["猫"]}'}}]}

        async def go():
            v = VisionService("https://x/v1", "k", "m", http_post=post)
            await v.describe_bytes(PNG)
            d = {}
            out = await v.describe_bytes(PNG, diag=d)
            return v, d, out

        v, diag, out = asyncio.run(go())
        self.assertEqual(out, "图")
        self.assertEqual(v.stats["cache_hit"], 1)
        self.assertEqual(diag.get("cache"), "memory")


    def test_persist_disabled_no_file(self):
        """不传 persist_path → 无持久文件、snapshot 标记 disabled。"""
        import tempfile
        async def post(url, payload):
            return {"choices": [{"message": {"content": '{"desc": "图", "tags": ["猫"]}'}}]}

        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                p = os.path.join(tmp, "c.json")
                v = VisionService("https://x/v1", "k", "m", http_post=post)
                await v.describe_bytes(PNG)
                self.assertFalse(os.path.exists(p))
                self.assertFalse(v.snapshot()["persist_enabled"])

        asyncio.run(go())


# ---------------------------------------------------------------- C27：GIF 网格
try:                                          # 系统 python 无 PIL（生产/台式机有）
    import PIL  # noqa: F401
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

_requires_pil = unittest.skipUnless(_HAS_PIL, "本环境无 Pillow（生产/台式机有）")

_PALETTE_C27 = [(230, 30, 30), (30, 200, 30), (30, 30, 230), (230, 205, 20),
                (230, 30, 230), (30, 200, 200)]


def _anim_gif(frames=6, size=(400, 300)) -> bytes:
    """真实可解码的多帧 GIF（第 i 帧 = 第 i 个纯色）。"""
    from PIL import Image
    ims = [Image.new("RGB", size, _PALETTE_C27[i % len(_PALETTE_C27)])
           for i in range(frames)]
    import io as _io
    buf = _io.BytesIO()
    ims[0].save(buf, format="GIF", save_all=True, append_images=ims[1:],
                duration=80, loop=0)
    return buf.getvalue()


class TestGifGridPromptC27(unittest.TestCase):
    """C27：**GIF 网格版与静图版是两个 system prompt**（B-049）。

    只说明"描述这张图片"时，模型会把网格当**一张拼图**逐格描述 —— 动作语义又丢了。
    所以抽帧拼网格必须**成对**上线：网格 + "这是动图按顺序抽的连续帧"。
    """

    def _mk(self, post):
        return VisionService("https://x/v1", "k", "m", http_post=post)

    def _capture(self):
        cap = []

        async def post(url, payload):
            cap.append(payload)
            return {"choices": [{"message": {"content": '{"desc": "ok", "tags": ["猫"]}'}}]}
        return cap, post

    def test_prompts_are_distinct(self):
        self.assertNotEqual(VISION_GIF_SYSTEM_PROMPT, VISION_SYSTEM_PROMPT)

    def test_gif_prompt_says_frames_in_order(self):
        """网格版必须点明"动图 / 按顺序抽的帧"，否则模型当拼图描述。"""
        P = VISION_GIF_SYSTEM_PROMPT
        for kw in ("动图", "顺序", "帧", "网格"):
            self.assertIn(kw, P, f"GIF 网格提示词缺少 {kw!r}")
        # 输出契约与静图版一致（S6：约定 JSON schema 是可用率的主导因素）
        for kw in ('"desc"', '"tags"', "JSON"):
            self.assertIn(kw, P)

    def test_static_prompt_does_not_claim_to_be_a_grid(self):
        self.assertNotIn("网格", VISION_SYSTEM_PROMPT)
        self.assertNotIn("连续帧", VISION_SYSTEM_PROMPT)

    @_requires_pil
    def test_animated_gif_uses_grid_prompt_and_jpeg_payload(self):
        import io as _io
        cap, post = self._capture()
        gif = _anim_gif(6, (400, 300))

        async def go():
            v = self._mk(post)
            d = {}
            out = await v.describe_bytes(gif, diag=d)
            return v, d, out

        v, diag, out = asyncio.run(go())
        self.assertEqual(out, "ok")
        self.assertEqual(cap[0]["messages"][0]["content"], VISION_GIF_SYSTEM_PROMPT)
        url = cap[0]["messages"][1]["content"][1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/jpeg;base64,"), url[:32])
        # 载荷确实是一张 960×640 的网格（不是原 GIF、也不是首帧）
        self.assertNotEqual(base64.b64decode(url.split(",", 1)[1]), gif)
        from PIL import Image
        with Image.open(_io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as im:
            self.assertEqual(im.size, (960, 640))
        self.assertEqual(diag.get("prompt"), "gif_grid")
        self.assertEqual(diag.get("mime"), "image/jpeg")
        self.assertEqual(diag["grid"]["frames"], 6)
        self.assertEqual(v.stats["gif_grid"], 1)
        self.assertEqual(v.stats["gif_grid_fail"], 0)

    @_requires_pil
    def test_short_gif_uses_prompt_with_its_own_frame_count(self):
        """2 帧的 GIF → 提示词说"共 2 帧"（不是写死的 6）—— 12% 的真实贴纸是这种。"""
        cap, post = self._capture()

        async def go():
            v = self._mk(post)
            d = {}
            out = await v.describe_bytes(_anim_gif(2, (400, 300)), diag=d)
            return out, d

        out, diag = asyncio.run(go())
        self.assertEqual(out, "ok")
        sys_prompt = cap[0]["messages"][0]["content"]
        self.assertIn("共 2 帧", sys_prompt)
        self.assertNotEqual(sys_prompt, VISION_GIF_SYSTEM_PROMPT)
        self.assertEqual(diag["grid"]["rows"], 1)

    def test_static_image_uses_static_prompt(self):
        cap, post = self._capture()

        async def go():
            v = self._mk(post)
            d = {}
            out = await v.describe_bytes(PNG, diag=d)
            return v, d, out

        v, diag, out = asyncio.run(go())
        self.assertEqual(out, "ok")
        self.assertEqual(cap[0]["messages"][0]["content"], VISION_SYSTEM_PROMPT)
        self.assertEqual(diag.get("prompt"), "static")
        self.assertNotIn("grid", diag)
        self.assertEqual(v.stats["gif_grid"], 0)

    def test_grid_failure_uses_static_prompt_and_leaves_trace(self):
        """拼网格失败 → 回退静图路径，但**必须留下原因**（diag + stats）。

        假 GIF 魔数（解不开）走的就是这条路：既有的"回退原字节 + 按魔数标 mime"
        行为不变，新增的是可观测性。
        """
        fake_gif = b"GIF89a" + b"\x00" * 40
        cap, post = self._capture()

        async def go():
            v = self._mk(post)
            d = {}
            out = await v.describe_bytes(fake_gif, diag=d)
            return v, d, out

        v, diag, out = asyncio.run(go())
        self.assertEqual(out, "ok")
        self.assertEqual(cap[0]["messages"][0]["content"], VISION_SYSTEM_PROMPT)
        self.assertTrue(diag.get("gif_grid_fail"), "降级原因必须进 diag")
        self.assertEqual(diag.get("prompt"), "static")
        self.assertEqual(v.stats["gif_grid_fail"], 1)
        self.assertEqual(v.stats["gif_grid"], 0)

    @_requires_pil
    def test_prep_runs_off_the_event_loop_thread(self):
        """抽帧拼网格实测最坏 587ms（真实贴纸库 n=181）→ 必须在线程里跑。

        同步调会把整个 bot 的消息处理按住几百毫秒 —— 旧路径最多 75ms，
        这是 C27 带进来的新成本，所以要有这条钉子。
        """
        import threading
        from services import vision as V

        seen = []
        orig = V.prepare_bytes_for_vision

        def spy(data, **kw):
            seen.append(threading.current_thread().name)
            return orig(data, **kw)

        cap, post = self._capture()
        main_name = threading.current_thread().name
        V.prepare_bytes_for_vision = spy
        try:
            async def go():
                v = self._mk(post)
                await v.describe_bytes(_anim_gif(6, (400, 300)))

            asyncio.run(go())
        finally:
            V.prepare_bytes_for_vision = orig
        self.assertTrue(seen, "预处理没被调用？")
        self.assertNotEqual(seen[0], main_name,
                            "抽帧跑在主线程上 → 事件循环被阻塞")

    @_requires_pil
    def test_cache_key_is_original_bytes_and_prep_runs_after_lookup(self):
        """契约：缓存键 = **原始字节** sha256；抽帧拼网格发生在查缓存**之后**。"""
        import tempfile
        from services import vision as V

        cap, post = self._capture()
        gif = _anim_gif(6, (400, 300))
        calls = []
        orig = V.prepare_bytes_for_vision

        def spy(data, **kw):
            calls.append(len(data))
            return orig(data, **kw)

        V.prepare_bytes_for_vision = spy
        try:
            async def go():
                with tempfile.TemporaryDirectory() as tmp:
                    p = os.path.join(tmp, "image_desc_cache.json")
                    v1 = VisionService("https://x/v1", "k", "m", http_post=post,
                                       persist_path=p)
                    d1 = await v1.describe_bytes(gif)
                    v1.flush()
                    first_prep_calls = list(calls)
                    calls.clear()
                    v2 = VisionService("https://x/v1", "k", "m", http_post=post,
                                       persist_path=p)
                    diag = {}
                    d2 = await v2.describe_bytes(gif, diag=diag)
                    return d1, d2, first_prep_calls, list(calls), diag, len(cap)

            d1, d2, first, second, diag, n_calls = asyncio.run(go())
        finally:
            V.prepare_bytes_for_vision = orig

        self.assertEqual(d1, "ok")
        self.assertEqual(d2, "ok")
        self.assertEqual(len(first), 1, "首次必须预处理一次")
        self.assertEqual(first[0], len(gif), "预处理拿到的是**原始**字节")
        self.assertEqual(second, [], "命中持久缓存后不得再预处理")
        self.assertEqual(n_calls, 1, "第二次不得再调模型")
        self.assertEqual(diag.get("cache"), "persist")
        self.assertEqual(diag.get("hash"), hashlib.sha256(gif).hexdigest()[:16])
        self.assertEqual(diag.get("bytes"), len(gif))


class TestVisionPersistLRU(unittest.TestCase):
    """A7\u2462 \u6301\u4e45\u54c8\u5e0c\u7f13\u5b58\uff1a\u8de8\u5b9e\u4f8b\u590d\u7528 / LRU \u6dd8\u6c70 / flush / snapshot \u89c2\u6d4b\u3002"""

    def _mk(self, path, post, max_entries=2000):
        return VisionService("https://x/v1", "k", "mimo-v2.5",
                             timeout=15.0, cache_ttl=30.0, http_post=post,
                             persist_path=str(path), persist_max=max_entries)

    def test_persist_reuse_across_instances(self):
        """\u540c\u4e00 sha256 \u56fe\u7247\uff1a\u7b2c\u4e8c\u4e2a\u5b9e\u4f8b\uff08\u6a21\u62df\u91cd\u542f\uff09\u4e0d\u8c03\u6a21\u578b\u3002"""
        import tempfile
        calls = []

        async def post(url, payload):
            calls.append(payload)
            return {"choices": [{"message": {"content": '{"desc": "\u4e00\u53ea\u732b", "tags": ["猫"]}'}}]}

        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                p = os.path.join(tmp, "image_desc_cache.json")
                v1 = self._mk(p, post)
                d1 = await v1.describe_bytes(PNG)
                v1.flush()
                v2 = self._mk(p, post)   # \u65b0\u5b9e\u4f8b\u4ece\u6301\u4e45\u6587\u4ef6\u52a0\u8f7d
                d2 = await v2.describe_bytes(PNG)
                return d1, d2, len(calls)

        d1, d2, n = asyncio.run(go())
        self.assertEqual(d1, "\u4e00\u53ea\u732b")
        self.assertEqual(d2, "\u4e00\u53ea\u732b")
        self.assertEqual(n, 1)  # \u7b2c\u4e8c\u5b9e\u4f8b\u547d\u4e2d\u6301\u4e45\u7f13\u5b58\uff0c\u672a\u518d\u8c03\u6a21\u578b

    def test_lru_evicts_least_recently_used(self):
        """\u8d85\u4e0a\u9650\u6dd8\u6c70 last_ts \u6700\u65e7\uff1b\u547d\u4e2d\u5237\u65b0 last_ts \u4f7f\u5176\u4e0d\u88ab\u6dd8\u6c70\u3002"""
        import tempfile
        import time as _time
        calls = []

        async def post(url, payload):
            calls.append(payload)
            return {"choices": [{"message": {"content": '{"desc": "\u56fe", "tags": ["猫"]}'}}]}

        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                p = os.path.join(tmp, "image_desc_cache.json")
                v = self._mk(p, post, max_entries=3)
                imgs = [PNG + bytes([i]) for i in range(5)]
                for im in imgs:
                    await v.describe_bytes(im)
                # \u5bb9\u91cf 3\uff0c\u5df2\u6dd8\u6c70 2\u5f20\uff08\u6700\u65e7 img0/img1\uff09
                self.assertEqual(len(v._persist), 3)
                self.assertTrue(v._evicted >= 2)
                # \u547d\u4e2d img2 \u5237\u65b0 last_ts \u2192 \u540e\u7eed\u518d\u63d2\u4e0d\u6dd8\u6c70\u5b83
                v._persist[hashlib.sha256(imgs[2]).hexdigest()]["last_ts"] = _time.time() + 999
                await v.describe_bytes(PNG + bytes([99]))  # \u65b0\u56fe\u89e6\u53d1\u6dd8\u6c70
                self.assertIn(hashlib.sha256(imgs[2]).hexdigest(), v._persist)

        asyncio.run(go())

    def test_snapshot_observability(self):
        """snapshot \u66b4\u9732 size/max/evicted \u4f9b\u5224\u65ad\u4e0a\u9650\u662f\u5426\u591f\u3002"""
        import tempfile
        async def post(url, payload):
            return {"choices": [{"message": {"content": '{"desc": "\u56fe", "tags": ["猫"]}'}}]}

        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                v = self._mk(os.path.join(tmp, "c.json"), post, max_entries=10)
                await v.describe_bytes(PNG)
                snap = v.snapshot()
                self.assertTrue(snap["persist_enabled"])
                self.assertEqual(snap["persist_size"], 1)
                self.assertEqual(snap["persist_max"], 10)
                self.assertEqual(snap["persist_evicted"], 0)
                # C27：拼网格计数必须有出口（"只写不读的仪表盘 = 没有仪表盘"）
                self.assertEqual(snap["gif_grid"], 0)
                self.assertEqual(snap["gif_grid_fail"], 0)

        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()


class TestExtractCompletionText(unittest.TestCase):
    """🔴 2026-09-13 实测修复的解析漏洞（识图间歇失败的根因）。

    部分图片模型把最终答案写进 `reasoning` 字段而 `content` 留空 ——
    只读 content 会拿到空串，被渲染成「（配图：无法识别）」，
    **看起来像"识别不了"，实为解析漏了一个字段**（与 B-005 同形）。
    """

    def _e(self, resp):
        from services.vision import extract_completion_text
        return extract_completion_text(resp)

    def test_content_wins(self):
        self.assertEqual(self._e({"choices": [{"message": {"content": "一只猫",
                                                          "reasoning": "思考"}}]}), "一只猫")

    def test_falls_back_to_reasoning_final_answer(self):
        r = {"choices": [{"message": {
            "content": "",
            "reasoning": "1. 分析请求：…\n4. 草拟描述：…\n5. 最后输出：五个Q版角色站于草地。",
        }}]}
        self.assertEqual(self._e(r), "五个Q版角色站于草地。")

    def test_falls_back_to_reasoning_content_alias(self):
        r = {"choices": [{"message": {"content": "",
                                      "reasoning_content": "结论：一只狗在跑"}}]}
        self.assertEqual(self._e(r), "一只狗在跑")

    def test_last_line_fallback(self):
        r = {"choices": [{"message": {"content": "",
                                      "reasoning": "思考第一行\n最后一行内容"}}]}
        self.assertEqual(self._e(r), "最后一行内容")

    def test_whitespace_content_treated_as_empty(self):
        r = {"choices": [{"message": {"content": "   ", "reasoning": "最后输出：有内容"}}]}
        self.assertEqual(self._e(r), "有内容")

    def test_separator_not_swallowed_by_backtracking(self):
        """回归：捕获组最小长度若设得比实际内容长，正则**会回溯把分隔符吃进去**。

        实测：`[^\n]{4,}` 遇到「最后输出：有内容」（冒号后仅 3 字）时回溯吞掉
        全角冒号 → 返回 `'：有内容'`（带前导冒号）。降到 `{2,}` 后正确。
        """
        for reasoning, want in (
            ("最后输出：有内容", "有内容"),          # 冒号后 3 字
            ("最后输出：OK", "OK"),                # 冒号后 2 字
            ("结论: 一只狗", "一只狗"),             # 半角冒号
            ("最后输出 五个角色", "五个角色"),         # 空格分隔
        ):
            r = {"choices": [{"message": {"content": "", "reasoning": reasoning}}]}
            got = self._e(r)
            self.assertEqual(got, want, f"reasoning={reasoning!r}")
            self.assertFalse(got.startswith(("：", ":")))

    def test_all_empty_returns_empty(self):
        self.assertEqual(self._e({"choices": [{"message": {}}]}), "")
        self.assertEqual(self._e({"choices": []}), "")
        self.assertEqual(self._e({}), "")
