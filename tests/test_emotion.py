# -*- coding: utf-8 -*-
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.emotion import LLMEmotionProvider, EmotionState


class TestEmotionParse(unittest.TestCase):
    def test_valid_json(self):
        st = LLMEmotionProvider._parse(
            '{"willingness": 0.7, "mood": "有点小开心", "sticker": "猫猫摇尾巴"}')
        self.assertAlmostEqual(st.global_willingness, 0.7)
        self.assertEqual(st.current_mood, "有点小开心")
        self.assertEqual(st.sticker_prompt, "猫猫摇尾巴")

    def test_clamp(self):
        st = LLMEmotionProvider._parse('{"willingness": 9}')
        self.assertAlmostEqual(st.global_willingness, 1.5)
        st = LLMEmotionProvider._parse('{"willingness": -3}')
        self.assertAlmostEqual(st.global_willingness, 0.3)

    def test_invalid_fallback_neutral(self):
        st = LLMEmotionProvider._parse("抱歉我无法回答")
        self.assertEqual(st, EmotionState.neutral())
        st = LLMEmotionProvider._parse("")
        self.assertEqual(st, EmotionState.neutral())
        st = LLMEmotionProvider._parse("{broken")
        self.assertEqual(st, EmotionState.neutral())


class TestEmotionCacheAndTimeout(unittest.TestCase):
    def test_cache_returns_same_state(self):
        calls = []

        async def llm_fn(prompt):
            calls.append(prompt)
            return '{"willingness": 1.0, "mood": "m", "sticker": ""}'

        import asyncio
        prov = LLMEmotionProvider(llm_fn, timeout=3.0, cache_ttl=30.0)
        st1 = asyncio.run(prov.query("g1", [{"role": "user", "content": "hi"}], None))
        st2 = asyncio.run(prov.query("g1", [{"role": "user", "content": "hi"}], None))
        self.assertEqual(len(calls), 1)
        self.assertEqual(st1, st2)

    def test_per_group_cache(self):
        calls = []

        async def llm_fn(prompt):
            calls.append(prompt)
            return '{"willingness": 0.5, "mood": "m", "sticker": ""}'

        import asyncio
        prov = LLMEmotionProvider(llm_fn, timeout=3.0, cache_ttl=30.0)
        asyncio.run(prov.query("g1", [], None))
        asyncio.run(prov.query("g2", [], None))
        self.assertEqual(len(calls), 2)

    def test_timeout_falls_back_neutral(self):
        async def slow_fn(prompt):
            import asyncio
            await asyncio.sleep(1.0)
            return '{"willingness": 1.0, "mood": "x", "sticker": ""}'

        import asyncio
        prov = LLMEmotionProvider(slow_fn, timeout=0.05, cache_ttl=5.0)
        st = asyncio.run(prov.query("g", [], None))
        self.assertEqual(st, EmotionState.neutral())


if __name__ == "__main__":
    unittest.main()
