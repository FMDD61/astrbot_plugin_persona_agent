# -*- coding: utf-8 -*-
import asyncio
import base64
import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.vision import VisionService, face_name, sniff_mime, resolve_image_bytes

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 16


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
            return {"choices": [{"message": {"content": "一只猫在吃草莓麻薯"}}]}

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
            return {"choices": [{"message": {"content": "ok"}}]}

        async def go():
            v = self._mk(post)
            await v.describe_bytes(GIF)
            return captured

        c = asyncio.run(go())
        self.assertEqual(c['url'], "https://x/v1/chat/completions")
        self.assertEqual(c['model'], "mimo-v2.5")
        self.assertTrue(c['img'].startswith("data:image/gif;base64,"))

    def test_timeout_fallback_none(self):
        async def post(url, payload):
            await asyncio.sleep(1.0)
            return {"choices": [{"message": {"content": "x"}}]}

        async def go():
            v = VisionService("https://x/v1", "k", "mimo-v2.5",
                              timeout=0.05, cache_ttl=30.0, http_post=post)
            return await v.describe_bytes(PNG)

        self.assertIsNone(asyncio.run(go()))

    def test_empty_desc_fallback_none(self):
        async def post(url, payload):
            return {"choices": [{"message": {"content": "  "}}]}

        async def go():
            v = self._mk(post)
            return await v.describe_bytes(PNG)

        self.assertIsNone(asyncio.run(go()))


    def test_persist_disabled_no_file(self):
        """不传 persist_path → 无持久文件、snapshot 标记 disabled。"""
        import tempfile
        async def post(url, payload):
            return {"choices": [{"message": {"content": "图"}}]}

        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                p = os.path.join(tmp, "c.json")
                v = VisionService("https://x/v1", "k", "m", http_post=post)
                await v.describe_bytes(PNG)
                self.assertFalse(os.path.exists(p))
                self.assertFalse(v.snapshot()["persist_enabled"])

        asyncio.run(go())


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
            return {"choices": [{"message": {"content": "\u4e00\u53ea\u732b"}}]}

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
            return {"choices": [{"message": {"content": "\u56fe"}}]}

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
            return {"choices": [{"message": {"content": "\u56fe"}}]}

        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                v = self._mk(os.path.join(tmp, "c.json"), post, max_entries=10)
                await v.describe_bytes(PNG)
                snap = v.snapshot()
                self.assertTrue(snap["persist_enabled"])
                self.assertEqual(snap["persist_size"], 1)
                self.assertEqual(snap["persist_max"], 10)
                self.assertEqual(snap["persist_evicted"], 0)

        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
