# -*- coding: utf-8 -*-
import asyncio
import base64
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


if __name__ == "__main__":
    unittest.main()
