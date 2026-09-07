"""InterjectionManager tests (A7 group isolation + usage persistence).

Previously untested (gap found 2026-09-07 review). Locks:
  - per-group cooldown/budget isolation (no cross-group leak)
  - usage persistence to usages/<group_id>.json (atomic, reloadable)
  - AT bypass semantics / RAG threshold / silent reasons (regression)
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from services.interjection import InterjectionManager, ACTION_REPLY, ACTION_SILENT, ACTION_TOPIC, TRIGGER_AT, TRIGGER_RAG, TRIGGER_SILENT, TRIGGER_COLD
    from services.style_profile import StyleProfile
except ImportError:
    from astrbot_plugin_persona_agent.services.interjection import InterjectionManager, ACTION_REPLY, ACTION_SILENT, ACTION_TOPIC, TRIGGER_AT, TRIGGER_RAG, TRIGGER_SILENT, TRIGGER_COLD
    from astrbot_plugin_persona_agent.services.style_profile import StyleProfile


def _style(tmpdir: str) -> StyleProfile:
    """StyleProfile with a permissive hourly budget for every hour."""
    s = StyleProfile(tmpdir)
    # write my_hourly_distribution.json with high budget
    Path(tmpdir, "my_hourly_distribution.json").write_text(
        '{"hourly_budget": {"0": 5, "1": 5, "2": 5, "3": 5, "4": 5, "5": 5, "6": 5, "7": 5, "8": 5, "9": 5, "10": 5, "11": 5, "12": 5, "13": 5, "14": 5, "15": 5, "16": 5, "17": 5, "18": 5, "19": 5, "20": 5, "21": 5, "22": 5, "23": 5}, "hourly_share": {"0": 0.04}}',
        encoding="utf-8",
    )
    return s


class TestGroupIsolation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.style = _style(self._tmp)
        self.mgr = InterjectionManager(
            self.style,
            data_dir=self._tmp,
            active_interjection=1,
            reply_on_at=1,
            min_gap_sec=1000,  # huge: a reply in one group must NOT block another
            at_cooldown_sec=8,
        )

    def _rag_reply(self, gid, now=None, score=0.9):
        return self.mgr.decide(
            group_id=gid, now_utc=now or time.time(), is_at_me=False,
            sender_uin="1", last_group_msg_ts=(now or time.time()) - 1,
            top_rag_score=score, emotion_multiplier=1.0,
        )

    def test_cooldown_not_shared_across_groups(self):
        """A reply in group A must not put group B into min-gap cooldown."""
        now = time.time()
        dA = self._rag_reply("A", now)
        self.assertEqual(dA.action, ACTION_REPLY)
        self.mgr.register_reply(group_id="A", now_utc=now, trigger=TRIGGER_RAG)

        # group B, 1s later — must NOT be cooldown-blocked by A's reply
        dB = self._rag_reply("B", now + 1)
        self.assertEqual(dB.action, ACTION_REPLY, f"B blocked by A's cooldown: {dB.reason}")

    def test_cooldown_within_same_group(self):
        """Same group immediately after a reply IS blocked by min-gap."""
        now = time.time()
        dA = self._rag_reply("A", now)
        self.mgr.register_reply(group_id="A", now_utc=now, trigger=TRIGGER_RAG)
        dA2 = self._rag_reply("A", now + 0.5)
        self.assertEqual(dA2.action, ACTION_SILENT)
        self.assertIn("min_gap", dA2.reason)

    def test_hourly_usage_isolated_per_group(self):
        """Hourly budget consumption must not leak between groups."""
        # budget = 1 for the current hour; short min_gap so cooldown doesn't mask budget
        hour = self.mgr._local_hour(time.time())
        tmp = tempfile.mkdtemp()
        style = _style(tmp)
        Path(tmp, "my_hourly_distribution.json").write_text(
            '{"hourly_budget": {"%d": 1}, "hourly_share": {}}' % hour, encoding="utf-8"
        )
        mgr = InterjectionManager(style, data_dir=tmp, active_interjection=1, reply_on_at=1, min_gap_sec=0.1)
        now = time.time()
        # group A uses its 1 budget
        dA = mgr.decide(group_id="A", now_utc=now, is_at_me=False, sender_uin="1",
                        last_group_msg_ts=now, top_rag_score=0.9, emotion_multiplier=1.0)
        self.assertEqual(dA.action, ACTION_REPLY)
        mgr.register_reply(group_id="A", now_utc=now, trigger=TRIGGER_RAG)
        # A exhausted (past min_gap)
        dA2 = mgr.decide(group_id="A", now_utc=now + 1, is_at_me=False, sender_uin="1",
                         last_group_msg_ts=now + 1, top_rag_score=0.9, emotion_multiplier=1.0)
        self.assertEqual(dA2.action, ACTION_SILENT)
        self.assertIn("budget", dA2.reason)
        # B independent budget still has room
        dB = mgr.decide(group_id="B", now_utc=now + 2, is_at_me=False, sender_uin="1",
                        last_group_msg_ts=now + 2, top_rag_score=0.9, emotion_multiplier=1.0)
        self.assertEqual(dB.action, ACTION_REPLY, f"B budget leaked from A: {dB.reason}")

    def test_at_cooldown_isolated_per_group(self):
        """AT cooldown per user must be per-group (user can @ in B while A cooling)."""
        now = time.time()
        self.mgr.register_reply(group_id="A", now_utc=now, trigger=TRIGGER_AT, sender_uin="234567")
        dA = self.mgr.decide(group_id="A", now_utc=now + 1, is_at_me=True, sender_uin="234567", last_group_msg_ts=now)
        self.assertEqual(dA.action, ACTION_SILENT)  # A still cooling for that user
        dB = self.mgr.decide(group_id="B", now_utc=now + 1, is_at_me=True, sender_uin="234567", last_group_msg_ts=now)
        self.assertEqual(dB.action, ACTION_REPLY)  # B not affected


class TestPersistence(unittest.TestCase):
    def test_register_reply_persists_usage_file(self):
        tmp = tempfile.mkdtemp()
        mgr = InterjectionManager(_style(tmp), data_dir=tmp, active_interjection=1, reply_on_at=1)
        now = time.time()
        mgr.register_reply(group_id="123456789", now_utc=now, trigger=TRIGGER_RAG)
        usage_file = Path(tmp, "usages", "123456789.json")
        self.assertTrue(usage_file.exists())
        data = __import__("json").loads(usage_file.read_text("utf-8"))
        self.assertAlmostEqual(data["last_reply_ts"], now, places=1)
        self.assertEqual(data["hourly_used"], 1.0)

    def test_reload_from_file_on_new_instance(self):
        """A fresh manager (simulating restart) restores persisted usage."""
        tmp = tempfile.mkdtemp()
        mgr1 = InterjectionManager(_style(tmp), data_dir=tmp, active_interjection=1, reply_on_at=1, min_gap_sec=1000)
        now = time.time()
        mgr1.register_reply(group_id="A", now_utc=now, trigger=TRIGGER_RAG)

        mgr2 = InterjectionManager(_style(tmp), data_dir=tmp, active_interjection=1, reply_on_at=1, min_gap_sec=1000)
        # same group right after: should be min-gap blocked because restored last_reply_ts
        d = mgr2.decide(group_id="A", now_utc=now + 0.5, is_at_me=False, sender_uin="1",
                        last_group_msg_ts=now, top_rag_score=0.9, emotion_multiplier=1.0)
        self.assertEqual(d.action, ACTION_SILENT)
        self.assertIn("min_gap", d.reason)

    def test_missing_file_starts_fresh(self):
        tmp = tempfile.mkdtemp()
        mgr = InterjectionManager(_style(tmp), data_dir=tmp, active_interjection=1)
        now = time.time()
        d = mgr.decide(group_id="nonexistent", now_utc=now, is_at_me=False, sender_uin="1",
                       last_group_msg_ts=now - 1, top_rag_score=0.9, emotion_multiplier=1.0)
        self.assertEqual(d.action, ACTION_REPLY)


class TestRegressionSemantics(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.mgr = InterjectionManager(
            _style(self._tmp), data_dir=self._tmp,
            active_interjection=0, reply_on_at=1,  # master gate off
        )
        self.now = time.time()

    def test_active_interjection_zero_blocks_rag_but_not_at(self):
        d = self.mgr.decide(group_id="A", now_utc=self.now, is_at_me=False, sender_uin="1",
                            last_group_msg_ts=self.now, top_rag_score=0.9)
        self.assertEqual(d.action, ACTION_SILENT)
        self.assertIn("active_interjection=0", d.reason)
        # AT still works when active_interjection=0 (reply_on_at governs)
        dAt = self.mgr.decide(group_id="A", now_utc=self.now, is_at_me=True, sender_uin="234567",
                              last_group_msg_ts=self.now)
        self.assertEqual(dAt.action, ACTION_REPLY)

    def test_reply_on_at_zero_blocks_at(self):
        mgr = InterjectionManager(_style(self._tmp), data_dir=self._tmp,
                                  active_interjection=1, reply_on_at=0)
        d = mgr.decide(group_id="A", now_utc=self.now, is_at_me=True, sender_uin="1",
                       last_group_msg_ts=self.now)
        self.assertEqual(d.action, ACTION_SILENT)
        self.assertIn("reply_on_at=0", d.reason)

    def test_rag_threshold_and_silence_cap(self):
        mgr = InterjectionManager(_style(self._tmp), data_dir=self._tmp,
                                  active_interjection=1, rag_score_threshold=0.6,
                                  silence_cap_sec=120)
        now = self.now
        # below threshold
        d = mgr.decide(group_id="A", now_utc=now, is_at_me=False, sender_uin="1",
                       last_group_msg_ts=now, top_rag_score=0.5)
        self.assertEqual(d.action, ACTION_SILENT)
        # above threshold but group silent too long
        d2 = mgr.decide(group_id="A", now_utc=now, is_at_me=False, sender_uin="1",
                        last_group_msg_ts=now - 200, top_rag_score=0.9)
        self.assertEqual(d2.action, ACTION_SILENT)
        # good
        d3 = mgr.decide(group_id="A", now_utc=now, is_at_me=False, sender_uin="1",
                        last_group_msg_ts=now - 1, top_rag_score=0.9)
        self.assertEqual(d3.action, ACTION_REPLY)

    def test_cold_topic_when_enabled(self):
        mgr = InterjectionManager(_style(self._tmp), data_dir=self._tmp,
                                  active_interjection=1, topic_bank_enabled=1,
                                  cold_start_threshold_sec=300)
        now = self.now
        d = mgr.decide(group_id="A", now_utc=now, is_at_me=False, sender_uin="1",
                       last_group_msg_ts=now - 400, top_rag_score=0.0)
        self.assertEqual(d.action, ACTION_TOPIC)
        self.assertEqual(d.trigger, TRIGGER_COLD)

    def test_snapshot_per_group(self):
        now = self.now
        self.mgr.register_reply(group_id="A", now_utc=now, trigger=TRIGGER_RAG)
        snapA = self.mgr.snapshot("A")
        self.assertEqual(snapA["group_id"], "A")
        self.assertEqual(snapA["hourly_used"], 1.0)
        global_snap = self.mgr.snapshot()
        self.assertIn("A", global_snap["groups"])


if __name__ == "__main__":
    unittest.main()
