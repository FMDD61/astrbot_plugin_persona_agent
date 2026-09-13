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

    def test_invalid_raises_so_degradation_is_visible(self):
        # S0 语义变更：解析失败**不再伪装成中性值**。
        # 旧行为（except → neutral）把「格式不合」与「模型真的输出中性」
        # 混为一谈 —— 这正是情绪引擎静默失效十几天没被发现的原因之一。
        for bad in ("抱歉我无法回答", "", "{broken", "null"):
            with self.assertRaises(Exception):
                LLMEmotionProvider._parse(bad)

    def test_fenced_json_tolerated(self):
        # 容错：模型把 JSON 包在围栏或解释文字里时应仍能解析
        st = LLMEmotionProvider._parse(
            '```json\n{"willingness": 0.6, "mood": "平静", "sticker": ""}\n```')
        self.assertAlmostEqual(st.global_willingness, 0.6)
        self.assertEqual(st.current_mood, "平静")
        st = LLMEmotionProvider._parse(
            '好的，结果如下：{"willingness": 0.4, "mood": "困", "sticker": "zzz"}')
        self.assertAlmostEqual(st.global_willingness, 0.4)
        self.assertEqual(st.current_mood, "困")


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
        # S0：降级必须可见 —— 超时原因 + 计数
        self.assertIn("timeout", prov.last_error or "")
        self.assertEqual(prov.stats["timeout"], 1)

    def test_degradation_reasons_are_distinguishable(self):
        """S0 核心诉求：超时 / 网络异常 / 解析失败 三种降级必须分得开。

        它们的对外表现（中性值 + 不发言）完全一样，只有 last_error/stats
        能区分 —— 2026-09-13 的排查正是卡在「分不开」上。
        """
        import asyncio

        async def slow_fn(prompt):
            await asyncio.sleep(1.0)
            return "{}"

        async def boom_fn(prompt):
            raise RuntimeError("gateway 500")

        async def garbage_fn(prompt):
            return "我觉得今天挺开心的"

        p1 = LLMEmotionProvider(slow_fn, timeout=0.05)
        asyncio.run(p1.query("g", [], None))
        self.assertEqual(p1.stats["timeout"], 1)

        p2 = LLMEmotionProvider(boom_fn, timeout=3.0)
        asyncio.run(p2.query("g", [], None))
        self.assertEqual(p2.stats["error"], 1)
        self.assertIn("gateway 500", p2.last_error or "")

        p3 = LLMEmotionProvider(garbage_fn, timeout=3.0)
        asyncio.run(p3.query("g", [], None))
        self.assertEqual(p3.stats["parse_fail"], 1)

    def test_ok_clears_last_error(self):
        import asyncio
        calls = {"n": 0}

        async def flaky(prompt):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("first fails")
            return '{"willingness": 0.9, "mood": "好", "sticker": ""}'

        prov = LLMEmotionProvider(flaky, timeout=3.0, cache_ttl=0.0)
        asyncio.run(prov.query("g", [], None))
        self.assertIsNotNone(prov.last_error)
        st = asyncio.run(prov.query("g", [], None))
        self.assertIsNone(prov.last_error, "成功一次后不应残留上一轮的降级原因")
        self.assertAlmostEqual(st.global_willingness, 0.9)


if __name__ == "__main__":
    unittest.main()
