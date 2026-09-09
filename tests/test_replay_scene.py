"""tools/replay_scene tests (A7④ 离线测试台).

extract: 两级选样（时间窗→小文件→--range→场景 JSON）的纯函数/CLI 行为；
run: 用注入 fake services 验证重放语义（每条走 pipeline、时钟推进、silent 不
生成、回复入 session、只读生产/写隔离 tmp、日志渲染）。
"""
import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.replay_scene import (  # noqa: E402
    _draft_line_text,
    _extract_range,
    _msg_in_window,
    _parse_draft_local,
    _parse_iso,
    extract_main,
    format_ts_utc8,
)


class _FakeIJ:
    """ijson.items 假件：从内存 dict 的 messages 数组迭代。"""

    @staticmethod
    def items(fh, path):
        data = json.load(fh)  # fh 是二进制流；json.load 支持 bytes
        return iter(data["messages"])


class TestWindowParsing(unittest.TestCase):
    def test_parse_iso(self):
        self.assertAlmostEqual(
            _parse_iso("2025-10-07T12:00:00.000Z"),
            _parse_iso("2025-10-07T12:00:00Z"),
        )
        self.assertIsNone(_parse_iso(""))

    def test_window_utc8_bounds(self):
        start = _parse_iso("2025-10-07T12:00:00Z")
        end = _parse_iso("2025-10-07T13:00:00Z")
        self.assertTrue(_msg_in_window(start + 1, start, end))
        self.assertTrue(_msg_in_window(start, start, end))    # 左闭（start 在内）
        self.assertFalse(_msg_in_window(end, start, end))     # 右开
        self.assertFalse(_msg_in_window(start - 60, start, end))

    def test_format_ts_utc8(self):
        # 12:00 UTC -> 20:00 +8
        self.assertEqual(format_ts_utc8(_parse_iso("2025-10-07T12:00:00Z")), "2025-10-07 20:00:00")

    def test_parse_draft_local(self):
        epoch = _parse_draft_local("2025-10-07 20:03:09")
        self.assertAlmostEqual(epoch, _parse_iso("2025-10-07T12:03:09Z"), places=3)


class TestDraftLine(unittest.TestCase):
    def test_multiline_collapse(self):
        self.assertEqual(_draft_line_text("a\nb"), "a ⏎ b")
        self.assertEqual(_draft_line_text("  a\nb\r\n  "), "a ⏎ b")


class TestExtractWindow(unittest.TestCase):
    def test_window_filters_and_formats(self):
        import tools.replay_scene as replay_scene
        tmp = tempfile.mkdtemp()
        merge = Path(tmp) / "merge.json"
        msgs = [
            {"timestamp": "2025-10-07T12:00:01.000Z", "messageType": 2,
             "receiver": {"uid": "123456789", "type": "group"},
             "sender": {"uin": "1", "name": "小红"},
             "content": {"text": "第一条"}},
            {"timestamp": "2025-10-07T12:30:00.000Z", "messageType": 2,
             "receiver": {"uid": "123456789", "type": "group"},
             "sender": {"uin": "2", "name": "小明"},
             "content": {"text": "窗口内"}},
            {"timestamp": "2025-10-07T14:00:00.000Z", "messageType": 2,
             "receiver": {"uid": "123456789", "type": "group"},
             "sender": {"uin": "3", "name": "某人"},
             "content": {"text": "窗口外"}},
            {"timestamp": "2025-10-07T12:01:00.000Z", "messageType": 2,
             "receiver": {"uid": "OTHER", "type": "group"},
             "sender": {"uin": "4", "name": "别群"},
             "content": {"text": "不是本群"}},
            {"timestamp": "2025-10-07T12:02:00.000Z", "messageType": 1,
             "receiver": {"uid": "123456789", "type": "group"},
             "sender": {"uin": "5", "name": "系统"},
             "content": {"text": "非文本消息"}},
            {"timestamp": "2025-10-07T12:03:00.000Z", "messageType": 2,
             "receiver": {"uid": "123456789", "type": "group"},
             "sender": {"uin": "6", "name": "风格源"},
             "content": {"text": "多行\n消息"}},
        ]
        merge.write_text(json.dumps({"messages": msgs}), encoding="utf-8")
        out = Path(tmp) / "draft.txt"

        real = sys.modules.get("ijson")
        sys.modules["ijson"] = _FakeIJ  # _extract_window 内 `import ijson` 拿到假件
        try:
            class A: pass
            args = A()
            args.merge = str(merge)
            args.group = "123456789"
            args.start = "2025-10-07 20:00"
            args.end = "2025-10-07 21:00"
            args.tz = 8
            args.out = str(out)
            rc = extract_main(args)
            self.assertEqual(rc, 0)
        finally:
            if real is None:
                sys.modules.pop("ijson", None)
            else:
                sys.modules["ijson"] = real

        lines = out.read_text("utf-8").splitlines()
        self.assertEqual(len(lines), 3)  # 窗口内文本 3 条（含多行）
        self.assertIn("[2025-10-07 20:00:01][小红]: 第一条", lines[0])
        self.assertIn("[2025-10-07 20:30:00][小明]: 窗口内", lines[1])
        self.assertIn("[2025-10-07 20:03:00][风格源]: 多行 ⏎ 消息", lines[2])


class TestExtractRange(unittest.TestCase):
    def _mk_draft(self, n=6):
        tmp = tempfile.mkdtemp()
        d = Path(tmp) / "draft.txt"
        lines = [f"[2025-10-07 20:{i:02d}:00][用户{i}]: 消息{i}" for i in range(1, n + 1)]
        d.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return tmp, d

    def _args(self, tmp, d, rng, out_name="scene.json"):
        class A: pass
        a = A()
        a.from_draft = str(d)
        a.range = rng
        a.out = str(Path(tmp) / out_name)
        a.group = "123456789"
        a.scene_id = ""
        return a

    def test_range_json(self):
        tmp, d = self._mk_draft()
        rc = _extract_range(self._args(tmp, d, "2..4"))
        self.assertEqual(rc, 0)
        data = json.loads((Path(tmp) / "scene.json").read_text("utf-8"))
        self.assertEqual(len(data["messages"]), 3)
        self.assertEqual(data["messages"][0]["name"], "用户2")
        self.assertEqual(data["messages"][0]["text"], "消息2")
        self.assertIsInstance(data["messages"][0]["ts"], float)
        self.assertEqual(data["meta"]["range"], "2..4")
        self.assertEqual(data["meta"]["n_msgs"], 3)

    def test_range_bad_syntax(self):
        tmp, d = self._mk_draft()
        self.assertNotEqual(_extract_range(self._args(tmp, d, "abc")), 0)

    def test_range_overrun(self):
        tmp, d = self._mk_draft(n=3)
        self.assertEqual(_extract_range(self._args(tmp, d, "2..99")), 0)
        data = json.loads((Path(tmp) / "scene.json").read_text("utf-8"))
        self.assertEqual(len(data["messages"]), 2)


class TestReplayRun(unittest.TestCase):
    """run 语义（fake services 手工注入 PersonaPipeline + ReplayRuntime.replay）。"""

    def _mk_scene(self, n=5, t0="2025-10-07T12:00:00Z"):
        tmp = tempfile.mkdtemp()
        p = Path(tmp) / "scene.json"
        # 全部同一发言人（uin 1001/用户1，member_relations 能解析别名）
        msgs = [{
            "ts": _parse_iso(t0) + i * 5,
            "uin": "1001",
            "name": "用户1",
            "text": f"消息{i}",
        } for i in range(1, n + 1)]
        p.write_text(json.dumps({"scene_id": "s1", "group_id": "123456789",
                                 "messages": msgs}, ensure_ascii=False), encoding="utf-8")
        return tmp, p

    def _rt(self, scene_p, fake_rag, fake_gen, gate=None, emotion=None,
            interjection=None, plugin_conf=None):
        """构造最小 ReplayRuntime（全 fake services），返回 (rt, tmpdir)。"""
        from tools.replay_scene import ReplayRuntime
        from services.pipeline import PersonaPipeline
        from services.session_manager import SessionManager
        from services.context_buffer import ContextBuffer
        from services.emotion import DefaultEmotionProvider

        tmp = tempfile.mkdtemp()
        dd = Path(tmp) / "data"
        dd.mkdir()
        (dd / "member_relations.json").write_text(json.dumps({
            "members": [
                {"uin": "1001", "alias": "用户1", "closeness": "close", "other_names": []},
            ]}, ensure_ascii=False), encoding="utf-8")
        (dd / "system_prompt_fragments.json").write_text(json.dumps(
            {"identity": "你是夕化炭。", "tone": "短句。", "vocabulary": "",
             "schedule": "", "relations": "", "group_context": "", "personality": "",
             "rules": []}), encoding="utf-8")
        (dd / "my_hourly_distribution.json").write_text(json.dumps(
            {"hourly_budget": {"20": 100.0}, "hourly_share": {"20": 0.2}}),
            encoding="utf-8")
        conf = {
            "reply_on_at": 1, "active_interjection": 1,
            "rag": {"enabled": 1, "score_threshold": 0.5, "k_retrieve": 8,
                    "top_n_final": 3, "max_example_chars": 400},
            "interjection": {"min_gap_sec": 0, "at_cooldown_sec": 0,
                             "cold_start_threshold_sec": 600, "silence_cap_sec": 120,
                             "local_tz_offset_hours": 8},
            "context_buffer": {"max_messages": 200, "max_age_sec": 3600},
            "topic_bank": {"enabled": 0},
            "emotion": {"enabled": 0, "temperature": 0.2, "reasoning_effort": "off"},
            "gate": {"enabled": 0, "temperature": 0.2, "reasoning_effort": "off",
                     "timeout_sec": 3, "decide_cooldown_sec": 8, "recent_n": 15,
                     "max_rag_hits": 3},
            "examples": {"enabled": 0},
            "llm": {"temperature": {"at_reply": 0.8, "active_interjection": 1.0,
                                    "cold_start": 1.1},
                    "reasoning_effort": "off", "max_tokens": 256},
            "bot_qq": "123456",
        }
        if plugin_conf:
            conf.update(plugin_conf)
        (Path(tmp) / "plugin_config.json").write_text(json.dumps(conf), encoding="utf-8")
        (Path(tmp) / "cmd_config.json").write_text(json.dumps({
            "provider_sources": [
                {"id": "p1", "enable": True, "type": "openai_chat_completion",
                 "api_base": "https://api.example.com/v1", "key": ["sk-test-123"],
                 "model": "model-a"}]}), encoding="utf-8")

        class A: pass
        args = A()
        args.scene = str(scene_p)
        args.data_dir = str(dd)
        args.cmd_config = str(Path(tmp) / "cmd_config.json")
        args.plugin_config = str(Path(tmp) / "plugin_config.json")
        args.model = ""
        args.rag_off = False
        args.no_gate = False
        args.gate_enabled = False
        args.tmp = str(Path(tmp) / "replay_tmp")
        args.out = str(Path(tmp) / "out.md")

        rt = ReplayRuntime(args)
        rt.style = types.SimpleNamespace(
            system_prompt=lambda local_hour=None: "你是测试。",
            preferred_alias=lambda uin: ("用户1" if uin == "1001" else ""),
            resolve_uin_from_name=lambda name: ("1001" if name == "用户1" else ""),
            hourly_budget=lambda hour: 100.0,
        )
        rt.rag = fake_rag
        rt.session_mgr = SessionManager(data_dir=str(Path(tmp) / "sess"), max_messages=None)
        rt.buffer = ContextBuffer(str(Path(tmp) / "buf"), persist_jsonl=False)
        rt.emotion = emotion or DefaultEmotionProvider()
        rt.gate = gate
        rt.memory_store = types.SimpleNamespace(ingest=lambda ev: None)
        rt.interjection = interjection or _FakeInterjection(0.9)
        rt.pipeline = PersonaPipeline(
            style=rt.style, rag=fake_rag, interjection=rt.interjection,
            emotion=rt.emotion, gate=rt.gate, session_mgr=rt.session_mgr,
            kg_provider=None, buffer=rt.buffer, generate=fake_gen,
            examples_block=lambda: "", postprocess=lambda s: s.strip(),
            temperature_for=lambda t: 0.8, rag_enabled=True,
            now_utc_fn=rt.clock, debounce_sec=0.0,
        )
        return rt, tmp

    def test_reply_each_message(self):
        calls = {"n": 0}
        async def gen(user_text, contexts, emotion, temperature, sender_uin, umo):
            calls["n"] += 1
            self.assertEqual(sender_uin, "1001")
            return f"回复{sender_uin}"

        class FakeRag:
            def query(self, ctx, k=8, top_n_final=3, now_utc=None):
                return [{"id": "x", "document": "历史例子", "metadata": {},
                         "score": 0.9}][:top_n_final]
            def warmup(self):
                return True

        _, scene_p = self._mk_scene(n=5)
        rt, tmp = self._rt(scene_p, FakeRag(), gen)
        import asyncio
        rc = asyncio.run(rt.replay())
        self.assertEqual(rc, 0)
        self.assertEqual(calls["n"], 5)
        out = Path(tmp) / "out.md"
        txt = out.read_text("utf-8")
        self.assertIn("# replay: scene=s1", txt)
        self.assertIn("【回复】回复1001", txt)
        # session 续了 assistant
        self.assertGreaterEqual(rt.session_mgr.size("123456789"), 5)

    def test_gate_silent_no_generation(self):
        """gate 拒绝（reply=False）→ silent，不调生成器。"""
        from services.gate import GateDecision
        calls = {"n": 0}

        async def gen(*a, **k):
            calls["n"] += 1
            return "x"

        class FakeGate:
            async def decide(self, group_id, recent_msgs, speaker, text,
                             rag_hits=None, is_at=False):
                return GateDecision(reply=False, conflict=False,
                                    reason="gate 不接", ts=1.0)

        _, scene_p = self._mk_scene(n=3)
        rt, tmp = self._rt(scene_p, _RagLow(), gen, gate=FakeGate())
        import asyncio
        rc = asyncio.run(rt.replay())
        self.assertEqual(rc, 0)
        self.assertEqual(calls["n"], 0)   # 生成器没被调
        txt = (Path(tmp) / "out.md").read_text("utf-8")
        self.assertIn("silent", txt)
        self.assertNotIn("【回复】", txt)

    def test_interjection_silent_no_generation(self):
        """硬闸 silent（RAG 低分）→ 不生成。"""
        calls = {"n": 0}

        async def gen(*a, **k):
            calls["n"] += 1
            return "x"

        _, scene_p = self._mk_scene(n=2)
        rt, tmp = self._rt(scene_p, _RagLow(), gen,
                           interjection=_FakeInterjection(0.0, silent=True))
        import asyncio
        rc = asyncio.run(rt.replay())
        self.assertEqual(rc, 0)
        self.assertEqual(calls["n"], 0)
        txt = (Path(tmp) / "out.md").read_text("utf-8")
        self.assertIn("【决策】silent", txt)

    def test_output_log_marks_msgs_and_replies(self):
        async def gen(*a, **k):
            return "好的~"

        _, scene_p = self._mk_scene(n=2)
        rt, tmp = self._rt(scene_p, _RagHigh(), gen)
        import asyncio
        asyncio.run(rt.replay())
        txt = (Path(tmp) / "out.md").read_text("utf-8")
        self.assertIn("━━━ [#1]", txt)
        self.assertIn("【决策】reply", txt)
        self.assertIn("【RAG top】", txt)
        self.assertIn("【回复】好的~", txt)

    def test_replay_writes_only_to_tmp(self):
        """数据目录除预置风格外无新增；tmp 目录获得 session/usages。"""
        async def gen(*a, **k):
            return "x"

        _, scene_p = self._mk_scene(n=2)
        rt, tmp = self._rt(scene_p, _RagHigh(), gen)
        dd = Path(tmp) / "data"
        before = set(p.name for p in dd.iterdir())
        import asyncio
        asyncio.run(rt.replay())
        after = set(p.name for p in dd.iterdir())
        self.assertEqual(before, after)  # 生产 data_dir 零写入


class _RagHigh:
    def query(self, ctx, k=8, top_n_final=3, now_utc=None):
        return [{"id": "x", "document": "历史例子", "metadata": {},
                 "score": 0.99}][:top_n_final]

    def warmup(self):
        return True


class _RagLow:
    def query(self, ctx, k=8, top_n_final=3, now_utc=None):
        return [{"id": "x", "document": "无关", "metadata": {},
                 "score": 0.1}][:top_n_final]

    def warmup(self):
        return True


class _FakeInterjection:
    def __init__(self, score, silent=False):
        self._score = score
        self._silent = silent

    def decide(self, **kw):
        from services.interjection import Decision, ACTION_REPLY, ACTION_SILENT, TRIGGER_RAG
        if self._silent:
            return Decision(action=ACTION_SILENT, trigger=TRIGGER_RAG,
                            reason="no trigger (fake)")
        return Decision(action=ACTION_REPLY, trigger=TRIGGER_RAG,
                        reason="fake rag hit", score=self._score)

    def register_reply(self, **kw):
        pass


if __name__ == "__main__":
    unittest.main()
