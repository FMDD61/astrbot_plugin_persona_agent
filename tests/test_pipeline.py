"""PersonaPipeline unit tests (A7 step 2a).

Covers: run() main-chain orchestration with fake services; SendIntent
action/text/quote; trace completeness (rag原文/hard_gate/generation);
gate rejection path; exception containment (never raises, silent fallback);
group isolation via passed-through group_id.
"""
import asyncio
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from services.pipeline import PersonaPipeline, PipelineInput, SendIntent
    from services.emotion import EmotionState
    from services.interjection import ACTION_REPLY, ACTION_SILENT, TRIGGER_AT
    from services.gate import GateService
except ImportError:
    from astrbot_plugin_persona_agent.services.pipeline import PersonaPipeline, PipelineInput, SendIntent
    from astrbot_plugin_persona_agent.services.emotion import EmotionState
    from astrbot_plugin_persona_agent.services.interjection import ACTION_REPLY, ACTION_SILENT, TRIGGER_AT
    from astrbot_plugin_persona_agent.services.gate import GateService


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeSession:
    def __init__(self):
        self.msgs = []
        self.contexts = [{"role": "user", "name": "小红", "content": "早"}]

    def recent(self, group_id, n=20):
        return self.msgs[-n:]

    def get_contexts(self, group_id):
        return list(self.contexts)


class _FakeRag:
    def __init__(self, hits):
        self._hits = hits
        self.last_query = None
        self.calls = 0

    def query(self, ctx, k=8, top_n_final=3):
        self.calls += 1
        self.last_query = ctx
        return [dict(h) for h in self._hits[:top_n_final]]


class _FakeEmotion:
    def __init__(self, state=None):
        self._state = state or EmotionState.neutral()

    async def query(self, group_id, recent_msgs, kg_ctx=None):
        return self._state


class _FakeGate:
    def __init__(self, reply=True, reason="ok"):
        self._reply = reply
        self._reason = reason
        self.calls = []

    async def decide(self, group_id, recent_msgs, speaker, text, rag_hits=None):
        self.calls.append((group_id, text))
        from services.gate import GateDecision
        return GateDecision(reply=self._reply, reason=self._reason, ts=__import__("time").time())


class _FakeInterjection:
    """Returns a scripted decision (bypass real InterjectionManager for unit)."""

    def __init__(self, action=ACTION_REPLY, trigger=None, reason="x"):
        from services.interjection import Decision
        # default trigger: follow is_at if not explicitly given
        self._trigger = trigger
        self._action = action
        self._reason = reason
        self.last_group = None

    def decide(self, **kw):
        self.last_group = kw.get("group_id")
        trig = self._trigger
        if trig is None:
            trig = TRIGGER_AT if kw.get("is_at_me") else "rag_hit"
        from services.interjection import Decision
        return Decision(action=self._action, trigger=trig, reason=self._reason)


def _pipeline(**over):
    """Build a pipeline with all fakes; override via kwargs."""
    p = PersonaPipeline(
        style=None,
        rag=over.get("rag", _FakeRag([{"document": "历史片段A", "score": 0.72}])),
        interjection=over.get("interjection", _FakeInterjection()),
        emotion=over.get("emotion", _FakeEmotion()),
        gate=over.get("gate", None),
        session_mgr=over.get("session", _FakeSession()),
        kg_provider=over.get("kg", None),
        buffer=over.get("buffer", None),
        generate=over.get("generate", lambda t, c, e, temp, su, umo: _async("好的~")),
        examples_block=lambda: "",
        postprocess=lambda s: s.strip(),
        temperature_for=lambda trig: 0.8,
        debounce_sec=0.0,
        rag_enabled=over.get("rag_enabled", True),
    )
    return p


def _async(v):
    async def _f():
        return v
    return _f()


class TestRunBasics(unittest.TestCase):
    def test_reply_path(self):
        p = _pipeline()
        si = _run(p.run(PipelineInput("g1", "你好", False, "234567", "小明")))
        self.assertEqual(si.action, "reply")
        self.assertEqual(si.text, "好的~")
        self.assertIn("rag", si.trace)
        self.assertEqual(si.trace["rag"][0]["document"], "历史片段A")
        self.assertIn("hard_gate", si.trace)
        self.assertIn("final_text", si.trace)

    def test_at_passthrough_no_gate(self):
        gate = _FakeGate(reply=False)  # would reject if called
        p = _pipeline(gate=gate)
        si = _run(p.run(PipelineInput("g1", "@bot 在吗", True, "1", "小红")))
        self.assertEqual(si.action, "reply")  # @ always replies, gate not consulted
        self.assertEqual(len(gate.calls), 0)

    def test_gate_rejects_silent(self):
        gate = _FakeGate(reply=False, reason="纯寒暄")
        p = _pipeline(gate=gate)
        si = _run(p.run(PipelineInput("g1", "吃饭了", False, "1", "小红")))
        self.assertEqual(si.action, "silent")
        self.assertIn("纯寒暄", si.silent_reason)
        self.assertEqual(len(gate.calls), 1)
        self.assertIn("gate", si.trace)

    def test_group_id_passed_to_gate(self):
        gate = _FakeGate()
        p = _pipeline(gate=gate)
        _run(p.run(PipelineInput("123456789", "hi", False, "1", "a")))
        self.assertEqual(gate.calls[0][0], "123456789")

    def test_silent_when_interjection_silent(self):
        from services.interjection import ACTION_SILENT
        p = _pipeline(interjection=_FakeInterjection(action=ACTION_SILENT, reason="no trigger"))
        si = _run(p.run(PipelineInput("g1", "hi", False, "1", "a")))
        self.assertEqual(si.action, "silent")
        self.assertEqual(si.silent_reason, "no trigger")

    def test_never_raises_on_generate_failure(self):
        async def bad_gen(t, c, e, temp, su, umo):
            raise RuntimeError("llm down")
        p = _pipeline(generate=bad_gen)
        si = _run(p.run(PipelineInput("g1", "hi", False, "1", "a")))
        self.assertEqual(si.action, "silent")
        self.assertIn("error", si.trace)

    def test_empty_generation_silent(self):
        p = _pipeline(generate=lambda t, c, e, temp, su, umo: _async(""))
        si = _run(p.run(PipelineInput("g1", "hi", False, "1", "a")))
        self.assertEqual(si.action, "silent")
        self.assertEqual(si.silent_reason, "empty generation")


class TestTraceAndQuote(unittest.TestCase):
    def test_trace_has_full_stages(self):
        p = _pipeline()
        si = _run(p.run(PipelineInput("g1", "hi", False, "1", "a")))
        for key in ("input", "rag", "emotion", "hard_gate", "temperature", "final_text"):
            self.assertIn(key, si.trace, key)

    def test_quote_marker_parsed_when_buffer_present(self):
        from services.context_buffer import ContextBuffer
        import tempfile
        tmp = tempfile.mkdtemp()
        buf = ContextBuffer(tmp, max_messages=50)
        buf.add(ts=100.0, group_id="g1", sender_id="1", sender_name="小红",
                text="原消息", message_id="msg-42", message_type="group")

        p = _pipeline(buffer=buf)
        si = _run(p.run(PipelineInput("g1", "hi", False, "1", "a")))
        self.assertEqual(si.action, "reply")
        self.assertEqual(si.text, "好的~")  # no marker in generated text here
        self.assertIsNone(si.quote_id)

    def test_quote_marker_extracted(self):
        """When generation includes [r:-1], pipeline resolves quote_id."""
        from services.context_buffer import ContextBuffer
        import tempfile
        tmp = tempfile.mkdtemp()
        buf = ContextBuffer(tmp, max_messages=50)
        buf.add(ts=100.0, group_id="g1", sender_id="1", sender_name="小红",
                text="原消息", message_id="msg-42", message_type="group")
        gen = lambda t, c, e, temp, su, umo: _async("[r:-1] 对呀")
        p = _pipeline(buffer=buf, generate=gen)
        si = _run(p.run(PipelineInput("g1", "hi", False, "1", "a")))
        self.assertEqual(si.text, "对呀")
        self.assertEqual(si.quote_id, "msg-42")

    def test_quote_survives_real_postprocess(self):
        """Regression: real text_style.postprocess strips [r:-N] markers, so
        quote extraction must run BEFORE postprocess (2026-09-07 bug)."""
        from services.context_buffer import ContextBuffer
        from services import text_style
        import tempfile
        tmp = tempfile.mkdtemp()
        buf = ContextBuffer(tmp, max_messages=50)
        buf.add(ts=100.0, group_id="g1", sender_id="1", sender_name="小红",
                text="原消息", message_id="msg-42", message_type="group")
        gen = lambda t, c, e, temp, su, umo: _async("[r:-1] 对呀")
        p = _pipeline(buffer=buf, generate=gen, postprocess=text_style.postprocess)
        si = _run(p.run(PipelineInput("g1", "hi", False, "1", "a")))
        self.assertEqual(si.text, "对呀")
        self.assertEqual(si.quote_id, "msg-42")

    def test_emote_poke_fields_reserved(self):
        p = _pipeline()
        si = _run(p.run(PipelineInput("g1", "hi", False, "1", "a")))
        # fields exist (reserved for later), empty by default
        self.assertIsNone(si.emote)
        self.assertIsNone(si.poke)



class TestRagEnabled(unittest.TestCase):
    """A7③: rag.enabled=0 \u4e0d\u67e5 RAG + trace \u8bb0 disabled; \u5355\u6b21\u68c0\u7d22\u590d\u7528 (pipeline+KG \u4e00\u8f6e\u53ea 1 \u6b21)."""

    def test_rag_disabled_no_query(self):
        rag = _FakeRag([{"document": "\u5386\u53f2", "score": 0.9}])
        p = _pipeline(rag=rag, rag_enabled=False)
        si = _run(p.run(PipelineInput("g1", "hi", False, "1", "a")))
        self.assertEqual(rag.calls, 0)              # \u4e0d\u67e5\u5411\u91cf\u5e93
        self.assertTrue(si.trace.get("rag_disabled"))
        self.assertEqual(si.trace.get("rag"), [])
        # \u51b3\u7b56\u65e0 RAG \u5206\u6570\uff08\u4ecd\u53ef\u56de\u590d\uff1a@\u6216\u901a\u8fc7 fake interjection\uff09
        self.assertEqual(si.action, "reply")

    def test_single_query_reused_by_kg(self):
        """\u4e00\u8f6e pipeline+KG \u53ea\u8c03\u4e00\u6b21 rag.query\uff08KG \u6536\u5230 external hits\uff09\u3002"""
        rag = _FakeRag([
            {"document": "h1", "score": 0.9},
            {"document": "h2", "score": 0.8},
            {"document": "h3", "score": 0.7},
        ])
        received = {}

        class FakeKG:
            async def query(self, ctx, external_dense_hits=None):
                received["ext"] = external_dense_hits
                return None

        p = _pipeline(rag=rag, kg=FakeKG())
        si = _run(p.run(PipelineInput("g1", "hi", False, "1", "a")))
        self.assertEqual(rag.calls, 1)              # \u53ea\u67e5\u4e00\u6b21
        # \u5168\u91cf\u4f20\u7ed9 KG\uff08\u4e0d\u662f\u622a\u65ad\u7684 top3\uff09
        self.assertEqual(len(received.get("ext") or []), 3)
        # trace \u8bb0\u5168\u91cf
        self.assertEqual(len(si.trace.get("rag") or []), 3)

    def test_trace_full_hits(self):
        """trace \u8bb0\u5168\u91cf\u547d\u4e2d\uff08LLM \u770b\u5230\u4ec0\u4e48 trace \u5c31\u8bb0\u4ec0\u4e48\uff09\u3002"""
        rag = _FakeRag([{"document": f"h{i}", "score": 0.9 - i / 10} for i in range(8)])
        p = _pipeline(rag=rag)
        si = _run(p.run(PipelineInput("g1", "hi", False, "1", "a")))
        self.assertEqual(len(si.trace.get("rag") or []), 8)


if __name__ == "__main__":
    unittest.main()
