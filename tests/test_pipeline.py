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
        self.last_now_utc = None

    def query(self, ctx, k=8, top_n_final=3, now_utc=None):
        self.calls += 1
        self.last_query = ctx
        self.last_now_utc = now_utc
        return [dict(h) for h in self._hits[:top_n_final]]


class _FakeEmotion:
    def __init__(self, state=None):
        self._state = state or EmotionState.neutral()

    async def query(self, group_id, recent_msgs, kg_ctx=None):
        return self._state


class _FakeGate:
    def __init__(self, reply=True, reason="ok", conflict=False):
        self._reply = reply
        self._reason = reason
        self._conflict = conflict
        self.calls = []

    async def decide(self, group_id, recent_msgs, speaker, text, rag_hits=None, is_at=False):
        self.calls.append((group_id, text, is_at))
        from services.gate import GateDecision
        return GateDecision(reply=self._reply, conflict=self._conflict,
                            reason=self._reason, ts=__import__("time").time())


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
        examples_block=over.get("examples_block", lambda: ""),
        postprocess=lambda s: s.strip(),
        temperature_for=lambda trig: 0.8,
        turn_block=over.get("turn_block", None),
        session_append=over.get("session_append", None),
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

    def test_at_now_goes_through_gate(self):
        """A7\u2462: @ \u4e5f\u8fc7 gate\uff08\u51b2\u7a81\u65f6 @ \u4e5f\u4e0d\u53d1\u8a00\uff09\u3002"""
        gate = _FakeGate(reply=True)
        p = _pipeline(gate=gate)
        si = _run(p.run(PipelineInput("g1", "@bot 在吗", True, "1", "小红")))
        self.assertEqual(si.action, "reply")   # gate reply=true \u2192 \u653e\u884c
        self.assertEqual(len(gate.calls), 1)   # @ \u4e5f\u88ab\u8c03
        self.assertTrue(gate.calls[0][2])      # is_at=True \u4f20\u5165

    def test_at_rejected_by_gate_conflict(self):
        """\u51b2\u7a81\u4e2d @ \u4e5f\u4e0d\u56de\uff08\u5b89\u5168\u9600\uff09\u3002"""
        gate = _FakeGate(reply=True, conflict=True)
        p = _pipeline(gate=gate)
        si = _run(p.run(PipelineInput("g1", "@bot 在吗", True, "1", "小红")))
        self.assertEqual(si.action, "silent")
        self.assertIn("conflict", si.silent_reason)

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


class TestQuoteSnapshotIsolation(unittest.TestCase):
    """B-001 回归：``[r:-N]`` 必须对**生成前冻结**的编号基求值。

    线上事故（2026-09-13 11:45，群 881438753）：LLM 看到的是「@我的那条」，
    但生成窗口内又进了 1 条，解析时对实时 buffer 求值 → 引用了后来那条。
    本组测试用「生成期间往 session 塞新消息」精确复现该窗口。
    """

    def _pipeline_with_session(self, gen, session_mgr):
        return _pipeline(generate=gen, session=session_mgr)

    def test_numbering_basis_is_frozen_at_generation_time(self):
        from services.session_manager import SessionManager

        sm = SessionManager(data_dir=None, max_messages=None)
        sm.append("g1", "user", "被 @ 的那条", name="焦糖",
                  message_id="MID-AT", sender_uin="337934842")

        async def gen_with_new_message(t, c, e, temp, su, umo):
            # 生成窗口内，群里又来了消息（正是线上事故的时间窗）
            sm.append("g1", "user", "生成期间新到的", name="花心鱼",
                      message_id="MID-LATE", sender_uin="848671904")
            return "[r:-1] 才没有啦"

        p = self._pipeline_with_session(gen_with_new_message, sm)
        si = _run(p.run(PipelineInput("g1", "hi", False, "1", "a")))

        # 旧实现对实时 buffer 求值 → 会引用 MID-LATE（错）
        self.assertEqual(si.quote_id, "MID-AT")
        self.assertEqual(si.text, "才没有啦")
        self.assertEqual(si.trace["quote_n"], 1)
        self.assertTrue(si.trace["quote_resolved"])
        self.assertEqual(si.trace["quote_target_uin"], "337934842")
        self.assertEqual(si.trace["quote_target_alias"], "焦糖")
        self.assertEqual(si.trace["quote_basis"], 1)  # 冻结基长度 = 1

    def test_bot_reply_occupies_a_slot_but_is_not_quotable(self):
        from services.session_manager import SessionManager

        sm = SessionManager(data_dir=None, max_messages=None)
        sm.append("g1", "user", "一", name="甲", message_id="m1", sender_uin="u1")
        sm.append("g1", "assistant", "机器人上一句")          # 占位、无 id
        sm.append("g1", "user", "二", name="乙", message_id="m2", sender_uin="u2")

        gen = lambda t, c, e, temp, su, umo: _async("[r:-2] 嗯")
        p = self._pipeline_with_session(gen, sm)
        si = _run(p.run(PipelineInput("g1", "hi", False, "1", "a")))
        # [r:-2] 指向机器人的回复 → 不可引用：剥标记、发纯文本、trace 留痕
        self.assertIsNone(si.quote_id)
        self.assertEqual(si.text, "嗯")
        self.assertTrue(si.trace.get("quote_target_missing"))
        self.assertEqual(si.trace["quote_n"], 2)

    def test_empty_entries_do_not_shift_numbering(self):
        """B-002 × B-001 交叉：空 content 被丢弃后编号基必须同步丢弃。"""
        from services.session_manager import SessionManager

        sm = SessionManager(data_dir=None, max_messages=None)
        sm.append("g1", "user", "旧", name="甲", message_id="m-old", sender_uin="u1")
        sm.append("g1", "user", "", name="脏", message_id="m-dirty", sender_uin="u9")
        sm.append("g1", "user", "新", name="乙", message_id="m-new", sender_uin="u2")

        gen = lambda t, c, e, temp, su, umo: _async("[r:-2] 早的")
        p = self._pipeline_with_session(gen, sm)
        si = _run(p.run(PipelineInput("g1", "hi", False, "1", "a")))
        self.assertEqual(si.quote_id, "m-old")
        self.assertNotEqual(si.quote_id, "m-dirty")

    def test_trace_records_unresolvable_quote(self):
        from services.session_manager import SessionManager

        sm = SessionManager(data_dir=None, max_messages=None)
        sm.append("g1", "user", "只有一条", name="甲", message_id="m1", sender_uin="u1")
        gen = lambda t, c, e, temp, su, umo: _async("[r:-9] 越界")
        p = self._pipeline_with_session(gen, sm)
        si = _run(p.run(PipelineInput("g1", "hi", False, "1", "a")))
        self.assertIsNone(si.quote_id)
        self.assertFalse(si.trace["quote_resolved"])
        self.assertEqual(si.text, "越界")



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



    def test_topic_goes_through_gate_conflict(self):
        """A7\u2462: TOPIC\uff08\u51b7\u573a\u4e3b\u52a8\u8bdd\u9898\uff09\u4e5f\u8fc7 gate conflict\u2014\u2014\u51b2\u7a81\u540e\u7684\u51b7\u573a\u4e0d\u4e3b\u52a8\u53d1\u8a00\u3002"""
        from services.interjection import ACTION_TOPIC, Decision
        ij = _FakeInterjection()
        ij._action = ACTION_TOPIC
        gate = _FakeGate(reply=True, conflict=True)
        p = _pipeline(interjection=ij, gate=gate)
        si = _run(p.run(PipelineInput("g1", "hi", False, "1", "a")))
        self.assertEqual(si.action, "silent")
        self.assertIn("conflict", si.silent_reason)

    def test_topic_passes_gate_when_no_conflict(self):
        """TOPIC \u65e0\u51b2\u7a81\u65f6\u6b63\u5e38\u53d1\u9001\u3002"""
        from services.interjection import ACTION_TOPIC
        ij = _FakeInterjection()
        ij._action = ACTION_TOPIC
        gate = _FakeGate(reply=True, conflict=False)
        p = _pipeline(interjection=ij, gate=gate)
        si = _run(p.run(PipelineInput("g1", "hi", False, "1", "a")))
        self.assertEqual(si.action, "topic")

if __name__ == "__main__":
    unittest.main()


class TestTurnBlockS2(unittest.TestCase):
    """S2 输入打包重划：「现在要回应的」块。

    依据（实测）：决策窗口 96 条的文本 **59% 已在 session 里**，按 spec §4.2
    再注入一遍是重复付费。故**不加输入**，改为把"当前这一轮"显式抬出来，
    并把图片/表情描述从正文里拆出来（直投式，而不是拼在用户话里）。
    """

    def _capture(self):
        seen = {}

        def tb(turn_lines, ctx):
            seen["lines"] = list(turn_lines)
            seen["ctx"] = dict(ctx)
            return "【现在要回应的】\n" + "\n".join(turn_lines)

        return tb, seen

    def test_turn_block_receives_body_without_image_markup(self):
        tb, seen = self._capture()
        gen = lambda t, c, e, temp, su, umo: _async("好")
        p = _pipeline(turn_block=tb, generate=gen)
        # 注意：PipelineInput.text 是 main 清洗+识图后的文本
        _run(p.run(PipelineInput("g1", "daishuki！ （配图：一只橘猫）", True, "337934842", "焦糖")))
        self.assertIn("lines", seen)
        joined = "\n".join(seen["lines"])
        self.assertIn("daishuki！", joined)          # 正文保留
        self.assertNotIn("（配图：", joined)          # 原始占位格式已拆
        self.assertIn("［图片］一只橘猫", joined)      # 直投式标注
        self.assertNotIn("一只橘猫）", joined)

    def test_image_failure_is_labeled_not_dropped(self):
        tb, seen = self._capture()
        p = _pipeline(turn_block=tb)
        _run(p.run(PipelineInput("g1", "（配图：无法识别）", False, "1", "甲")))
        joined = "\n".join(seen["lines"])
        self.assertIn("看不清内容", joined)   # 失败要明说，RP 不该脑补

    def test_face_annotation_split_out(self):
        tb, seen = self._capture()
        p = _pipeline(turn_block=tb)
        _run(p.run(PipelineInput("g1", "哈哈（表情：呲牙）", False, "1", "甲")))
        joined = "\n".join(seen["lines"])
        self.assertIn("哈哈", joined)
        self.assertIn("［表情］呲牙", joined)

    def test_media_only_message_marked(self):
        tb, seen = self._capture()
        p = _pipeline(turn_block=tb)
        _run(p.run(PipelineInput("g1", "（配图：一只狗）", False, "1", "甲")))
        joined = "\n".join(seen["lines"])
        self.assertIn("只发了媒体", joined)

    def test_ctx_carries_speaker_and_is_at(self):
        tb, seen = self._capture()
        p = _pipeline(turn_block=tb)
        _run(p.run(PipelineInput("g1", "在吗", True, "999", "小明")))
        self.assertEqual(seen["ctx"]["sender_uin"], "999")
        self.assertEqual(seen["ctx"]["sender_alias"], "小明")
        self.assertTrue(seen["ctx"]["is_at"])

    def test_trace_records_turn_block_shape(self):
        tb, _ = self._capture()
        p = _pipeline(turn_block=tb)
        si = _run(p.run(PipelineInput("g1", "嗨（配图：猫）（表情：呲牙）", False, "1", "甲")))
        tb_trace = si.trace.get("turn_block") or {}
        self.assertEqual(tb_trace.get("images"), 1)
        self.assertEqual(tb_trace.get("faces"), 1)
        self.assertEqual(tb_trace.get("body_chars"), len("嗨"))

    def test_block_inserted_before_kg_tail(self):
        """缓存序不变：稳定在上、易变在下，KG 尾注仍是最后一条。"""
        tb, _ = self._capture()

        class _Kg:
            async def query(self, ctx, external_dense_hits=None):
                class R: content = "KG尾注内容"
                return R()

        p = _pipeline(turn_block=tb, kg=_Kg())
        captured = {}

        def gen(t, c, e, temp, su, umo):
            captured["ctx"] = list(c)
            return _async("好")

        p = _pipeline(turn_block=tb, kg=_Kg(), generate=gen)
        _run(p.run(PipelineInput("g1", "你好", False, "1", "甲")))
        ctx = captured["ctx"]
        self.assertEqual(ctx[-1]["content"], "KG尾注内容")          # KG 仍最后
        self.assertIn("【现在要回应的】", ctx[-2]["content"])        # turn block 在其前

    def test_no_turn_block_keeps_legacy_context(self):
        """未接线回调时行为不变（离线测试台/旧路径）。"""
        captured = {}

        def gen(t, c, e, temp, su, umo):
            captured["ctx"] = list(c)
            return _async("好")

        p = _pipeline(generate=gen)
        si = _run(p.run(PipelineInput("g1", "你好", False, "1", "甲")))
        self.assertEqual(si.action, "reply")
        self.assertFalse(any("【现在要回应的】" in str(m.get("content")) for m in captured["ctx"]))

    def test_examples_block_sits_before_session(self):
        """恒定示例块必须在 session **之前**（否则永远落在缓存失效区）。

        2026-09-13 修正：它原先拼在 session 之后，而 session 每轮增长
        （实测一天 1482 条）→ 排在增长段之后的恒定内容等于每轮白付 token。
        """
        from services.session_manager import SessionManager
        sm = SessionManager(data_dir=None, max_messages=None)
        sm.append("g1", "user", "历史一", name="甲")
        sm.append("g1", "assistant", "历史二")

        def sa(gid, t, n, mid, uin):
            sm.append(gid, "user", t, name=n or None, message_id=mid or "", sender_uin=uin or "")

        captured = {}

        def gen(t, c, e, temp, su, umo):
            captured["ctx"] = [dict(m) for m in c]
            return _async("好")

        p = _pipeline(session=sm, session_append=sa, generate=gen,
                      examples_block=lambda: "【示例块】",
                      turn_block=lambda lines, ctx: "【现在要回应的】\n" + "\n".join(lines))
        _run(p.run(PipelineInput("g1", "当前", False, "u1", "甲")))
        contents = [str(m.get("content")) for m in captured["ctx"]]
        self.assertEqual(contents[0], "【示例块】", "示例块必须是第一条（缓存前缀起点）")
        i_ex = contents.index("【示例块】")
        i_hist = contents.index("历史一")
        i_turn = next(i for i, c in enumerate(contents) if "【现在要回应的】" in c)
        self.assertLess(i_ex, i_hist, "示例块要在 session 之前")
        self.assertLess(i_hist, i_turn, "当前轮要在 session 之后（易变量集中尾部）")

    def test_turn_block_exception_does_not_break_reply(self):
        """回调抛异常 → 记 trace 但照常生成（不因打包失败而哑掉）。"""
        def bad_tb(lines, ctx):
            raise RuntimeError("boom")

        p = _pipeline(turn_block=bad_tb)
        si = _run(p.run(PipelineInput("g1", "你好", False, "1", "甲")))
        self.assertEqual(si.action, "reply")
        self.assertIn("boom", si.trace.get("turn_block_error", ""))


class TestDeferredSessionAppend(unittest.TestCase):
    """S2：本条**推迟**入会话，避免「当前消息说两遍」。

    不变式：
      - LLM 看到的 context 里**不含**当前这条（它在「现在要回应的」块里）
      - 落盘发生在硬闸之后、任何 early return 之前
      - 静默 / 睡眠 / 异常路径都不丢消息
    """

    def _mk(self, session):
        appends = []

        def sa(group_id, text, name, message_id, sender_uin):
            appends.append((group_id, text, name, message_id, sender_uin))
            session.append(group_id, "user", text, name=name,
                           message_id=message_id, sender_uin=sender_uin)

        return sa, appends

    def _pipeline_with(self, appends_cb, session, **over):
        """注意：不变式测试必须传真实 SessionManager（_FakeSession 的
        get_contexts 是静态列表，append 不会反映到 context 里）。"""
        captured = {}

        def gen(t, c, e, temp, su, umo):
            captured["ctx"] = list(c)
            return _async("好")

        p = _pipeline(session=session, session_append=appends_cb, generate=gen, **over)
        return p, captured

    def test_current_message_not_in_context_but_is_in_turn_block(self):
        # 必须用**真实 SessionManager**：_FakeSession 的 contexts 是静态的，
        # 测不出"落盘前后 context 的差异"这个不变式。
        from services.session_manager import SessionManager
        sm = SessionManager(data_dir=None, max_messages=None)
        sm.append("g1", "user", "之前的消息", name="甲", message_id="m0")
        cb, appends = self._mk(sm)
        tb_calls = []

        def tb(lines, ctx):
            tb_calls.append(list(lines))
            return "【现在要回应的】\n" + "\n".join(lines)

        p, captured = self._pipeline_with(cb, sm, turn_block=tb)
        p.defer_session_append("g1", "当前这条", name="乙", message_id="m1", sender_uin="u2")
        si = _run(p.run(PipelineInput("g1", "当前这条", False, "u2", "乙")))

        self.assertEqual(si.action, "reply")
        # ① 落盘了，且元数据完整
        self.assertEqual(len(appends), 1)
        self.assertEqual(appends[0][1], "当前这条")
        self.assertEqual(appends[0][3], "m1")
        # ② session 段必须**在落盘前**构建 —— 即 context 里不出现"当前这条"
        #    作为独立条目（会话历史条目），它只应出现在「现在要回应的」块里。
        #    注意不能只看 user 角色：turn block 是 system 消息、内容里也含本条。
        self.assertIn("之前的消息", [m.get("content") for m in captured["ctx"]])
        hist_entries = [m.get("content") for m in captured["ctx"]
                        if m.get("content") == "当前这条"]
        self.assertEqual(hist_entries, [], "当前这条不应作为会话条目出现在 context 里")
        # ③ 当前这条出现在 turn block
        self.assertTrue(tb_calls)
        self.assertIn("当前这条", "\n".join(tb_calls[0]))

    def test_silent_path_still_records(self):
        """v3 设计意图：静默但照常记录。"""
        from services.session_manager import SessionManager

        class _Silent:
            def decide(self, **kw):
                from services.interjection import Decision, ACTION_SILENT, TRIGGER_SILENT
                return Decision(action=ACTION_SILENT, trigger=TRIGGER_SILENT,
                                reason="test silent")

        sm = SessionManager(data_dir=None, max_messages=None)
        cb, appends = self._mk(sm)
        p, _ = self._pipeline_with(cb, sm, interjection=_Silent())
        p.defer_session_append("g1", "静默也要记", name="甲", message_id="m1", sender_uin="u1")
        si = _run(p.run(PipelineInput("g1", "静默也要记", False, "u1", "甲")))
        self.assertEqual(si.action, "silent")
        self.assertEqual(len(appends), 1)
        self.assertIn("静默也要记", [m["content"] for m in sm.get_contexts("g1")])
        self.assertTrue(si.trace.get("session_appended"))

    def test_exception_path_flushes(self):
        """生成回调抛异常 → 消息不得丢失。"""
        from services.session_manager import SessionManager

        def boom(t, c, e, temp, su, umo):
            raise RuntimeError("gen boom")

        sm = SessionManager(data_dir=None, max_messages=None)
        cb, appends = self._mk(sm)
        p = _pipeline(session=sm, session_append=cb, generate=boom)
        p.defer_session_append("g1", "异常也要记", name="甲", message_id="m9", sender_uin="u1")
        si = _run(p.run(PipelineInput("g1", "异常也要记", False, "u1", "甲")))
        self.assertEqual(si.action, "silent")
        self.assertEqual(len(appends), 1, "异常路径必须落盘")

    def test_no_defer_no_append(self):
        """没挂起任何东西时不应凭空写入。"""
        from services.session_manager import SessionManager
        sm = SessionManager(data_dir=None, max_messages=None)
        cb, appends = self._mk(sm)
        p, _ = self._pipeline_with(cb, sm)
        _run(p.run(PipelineInput("g1", "嗨", False, "u1", "甲")))
        self.assertEqual(appends, [])

    def test_flush_is_idempotent(self):
        from services.session_manager import SessionManager
        sm = SessionManager(data_dir=None, max_messages=None)
        cb, appends = self._mk(sm)
        p, _ = self._pipeline_with(cb, sm)
        p.defer_session_append("g1", "一次", name="甲", message_id="m1", sender_uin="u1")
        self.assertTrue(p.flush_session_append("g1"))
        self.assertFalse(p.flush_session_append("g1"))
        self.assertEqual(len(appends), 1)

    def test_next_message_appends_previous(self):
        """同群两条消息：第一条挂起 → 第二条的 run 会把第一条落盘。"""
        from services.session_manager import SessionManager
        sm = SessionManager(data_dir=None, max_messages=None)
        cb, appends = self._mk(sm)
        p, _ = self._pipeline_with(cb, sm)
        p.defer_session_append("g1", "第一条", name="甲", message_id="m1", sender_uin="u1")
        _run(p.run(PipelineInput("g1", "第一条", False, "u1", "甲")))
        p.defer_session_append("g1", "第二条", name="乙", message_id="m2", sender_uin="u2")
        _run(p.run(PipelineInput("g1", "第二条", False, "u2", "乙")))
        self.assertEqual([a[1] for a in appends], ["第一条", "第二条"])
