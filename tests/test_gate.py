"""GateService unit tests (A7 §4.1).

Covers: prompt building/cropping, strict JSON parse (incl. tolerances),
per-group cooldown reuse, conservative silent fallback on failure/timeout,
never-raises contract.
"""
import asyncio
import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from services.gate import GateService, GateDecision
except ImportError:
    from astrbot_plugin_persona_agent.services.gate import GateService, GateDecision


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _msgs(n=20, name="小红", text="今天好累"):
    return [{"role": "user", "name": name, "content": f"{text} {i}"} for i in range(n)]


class TestPromptBuild(unittest.TestCase):
    def test_crops_to_recent_n(self):
        svc = GateService(lambda p: _async("x"), recent_n=5)
        prompt = svc._build_prompt(_msgs(20), "小明", "哈哈", None)
        # only last 5 of the 20 messages appear
        self.assertIn("今天好累 15", prompt)
        self.assertNotIn("今天好累 0", prompt)

    def test_includes_speaker_text(self):
        svc = GateService(lambda p: _async("x"))
        prompt = svc._build_prompt(_msgs(2), "小明", "你猜", None)
        self.assertIn("当前说话人：小明", prompt)
        self.assertIn("本条消息：你猜", prompt)

    def test_rag_hits_embedded_and_capped(self):
        svc = GateService(lambda p: _async("x"), max_rag_hits=2)
        hits = [{"text": f"历史片段{i}", "score": 0.5 + i / 10} for i in range(5)]
        prompt = svc._build_prompt(_msgs(1), "小明", "hi", hits)
        self.assertIn("历史片段0", prompt)
        self.assertIn("历史片段1", prompt)
        self.assertNotIn("历史片段4", prompt)  # capped at 2
        self.assertIn("风格参考片段", prompt)

    def test_rag_hits_document_field(self):
        """RagService.query returns {'document', 'score', ...} — read that."""
        svc = GateService(lambda p: _async("x"), max_rag_hits=3)
        hits = [{"id": "x", "document": "文档字段片段", "metadata": {}, "score": 0.7}]
        prompt = svc._build_prompt(_msgs(1), "小明", "hi", hits)
        self.assertIn("文档字段片段", prompt)
        self.assertIn("[0.70]", prompt)


def _async(v):
    async def _f():
        return v
    return _f()


class TestParse(unittest.TestCase):
    def test_ok_true(self):
        d = GateService._parse('{"reply": true, "reason": "在问机器人"}')
        self.assertIsNotNone(d)
        self.assertTrue(d.reply)
        self.assertEqual(d.reason, "在问机器人")

    def test_ok_false(self):
        d = GateService._parse('{"reply": false, "reason": "纯寒暄"}')
        self.assertIsNotNone(d)
        self.assertFalse(d.reply)

    def test_string_tolerated(self):
        d = GateService._parse('{"reply": "true", "reason": ""}')
        self.assertTrue(d.reply)

    def test_int_tolerated(self):
        d = GateService._parse('{"reply": 1, "reason": "x"}')
        self.assertTrue(d.reply)

    def test_malformed_none(self):
        for bad in ["", "not json", "{}", '{"reply": "maybe"}', '{"reply": 2}', "[1,2]"]:
            self.assertIsNone(GateService._parse(bad), bad)


class TestDecide(unittest.TestCase):
    def test_reply_path(self):
        calls = []

        async def llm(p):
            calls.append(p)
            return '{"reply": true, "reason": "在问机器人"}'

        svc = GateService(llm)
        d = _run(svc.decide("g1", _msgs(3), "小明", "在吗", None))
        self.assertTrue(d.reply)
        self.assertFalse(d.fallback)
        self.assertFalse(d.cached)
        self.assertEqual(len(calls), 1)

    def test_silent_path(self):
        async def llm(p):
            return '{"reply": false, "reason": "闲聊"}'
        d = _run(GateService(llm).decide("g1", _msgs(3), "小明", "吃饭了", None))
        self.assertFalse(d.reply)
        self.assertFalse(d.fallback)

    def test_cooldown_reuses_cache(self):
        calls = []

        async def llm(p):
            calls.append(p)
            return '{"reply": true, "reason": "x"}'

        svc = GateService(llm, decide_cooldown_sec=60)
        d1 = _run(svc.decide("g1", _msgs(3), "小明", "m1", None))
        d2 = _run(svc.decide("g1", _msgs(4), "小明", "m2", None))
        self.assertTrue(d1.reply)
        self.assertTrue(d2.reply)
        self.assertTrue(d2.cached)
        self.assertEqual(len(calls), 1)  # only first hit the LLM

    def test_cooldown_group_isolated(self):
        calls = []

        async def llm(p):
            calls.append(p)
            return '{"reply": true, "reason": "x"}'

        svc = GateService(llm, decide_cooldown_sec=60)
        _run(svc.decide("g1", _msgs(2), "a", "m", None))
        _run(svc.decide("g2", _msgs(2), "b", "m", None))
        self.assertEqual(len(calls), 2)  # different groups don't share cache

    def test_failure_silent_fallback(self):
        async def llm(p):
            raise RuntimeError("boom")
        d = _run(GateService(llm).decide("g1", _msgs(2), "小明", "hi", None))
        self.assertFalse(d.reply)
        self.assertTrue(d.fallback)

    def test_timeout_silent_fallback(self):
        async def llm(p):
            await asyncio.sleep(5)
            return '{"reply": true, "reason": "late"}'
        svc = GateService(llm, timeout=0.05)
        d = _run(svc.decide("g1", _msgs(2), "小明", "hi", None))
        self.assertFalse(d.reply)
        self.assertTrue(d.fallback)

    def test_bad_json_silent_fallback(self):
        async def llm(p):
            return "sorry not json"
        d = _run(GateService(llm).decide("g1", _msgs(2), "小明", "hi", None))
        self.assertFalse(d.reply)
        self.assertTrue(d.fallback)

    def test_cache_clear(self):
        calls = []

        async def llm(p):
            calls.append(p)
            return '{"reply": false, "reason": "x"}'

        svc = GateService(llm, decide_cooldown_sec=60)
        _run(svc.decide("g1", _msgs(2), "a", "m", None))
        _run(svc.decide("g1", _msgs(2), "a", "m", None))
        svc.clear_cache("g1")
        _run(svc.decide("g1", _msgs(2), "a", "m", None))
        self.assertEqual(len(calls), 2)

    def test_never_raises_on_garbage(self):
        async def llm(p):
            raise ValueError("x")
        svc = GateService(llm)
        # multiple groups, multiple rounds — nothing raises
        for g in ("g1", "g2"):
            for _ in range(2):
                d = _run(svc.decide(g, [], "a", "m", None))
                self.assertIsInstance(d, GateDecision)


if __name__ == "__main__":
    unittest.main()
