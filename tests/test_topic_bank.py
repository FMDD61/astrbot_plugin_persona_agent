"""Tests for services/topic_bank.py (G12, stdlib only)."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from services.topic_bank import TopicBank, Topic
except ImportError:
    from astrbot_plugin_persona_agent.services.topic_bank import TopicBank, Topic


def _topics_json():
    return [
        {"id": "t1", "content": "瓦最近怎么样", "category": "游戏见闻",
         "context_hints": ["瓦"], "priority": 3, "min_silence_seconds": 180, "enabled": True},
        {"id": "t2", "content": "晚上吃什么", "category": "日常",
         "context_hints": ["吃"], "priority": 5, "min_silence_seconds": 600, "enabled": True},
        {"id": "t3", "content": "历史坏例话题", "category": "坏例",
         "context_hints": [], "priority": 5, "min_silence_seconds": 0, "enabled": False},
    ]


class TopicBankTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "topic_bank.json").write_text(
            json.dumps(_topics_json(), ensure_ascii=False), encoding="utf-8")
        self.bank = TopicBank(str(self.dir))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_loads_topics(self) -> None:
        self.assertEqual(len(self.bank._topics), 3)
        self.assertFalse(self.bank._topics[2].enabled)

    def test_no_candidate_below_min_silence(self) -> None:
        self.assertIsNone(self.bank.pick(now=1000.0, silence_sec=60, live_text=""))
        top = self.bank.pick(now=1000.0, silence_sec=200, live_text="")
        self.assertIsNotNone(top)
        self.assertEqual(top.id, "t1")

    def test_context_hint_boost(self) -> None:
        # t1 has matching hint (瓦), t2 requires 600s silence -> excluded
        top = self.bank.pick(now=1000.0, silence_sec=700, live_text="今天打瓦了吗")
        self.assertEqual(top.id, "t1")

    def test_disabled_topic_never_picked(self) -> None:
        top = self.bank.pick(now=1000.0, silence_sec=3600, live_text="")
        self.assertNotEqual(top.id, "t3")

    def test_sent_moves_topic_to_archive(self) -> None:
        # 200s: t2 (min 600s) excluded, so t1 alone is eligible
        top = self.bank.pick(now=1000.0, silence_sec=200, live_text="")
        self.assertEqual(top.id, "t1")
        self.bank.mark_sent(top, now=1000.0, reason="cold_start")
        again = self.bank.pick(now=2000.0, silence_sec=200, live_text="")
        self.assertIsNone(again)
        records = json.loads((self.dir / "topic_sent.json").read_text("utf-8"))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["id"], top.id)

    def test_sent_state_survives_restart(self) -> None:
        top = self.bank.pick(now=1000.0, silence_sec=200, live_text="")
        self.assertEqual(top.id, "t1")
        self.bank.mark_sent(top, now=1000.0)
        bank2 = TopicBank(str(self.dir))
        self.assertEqual(bank2.pick(now=2000.0, silence_sec=200, live_text=""), None)

    def test_freshness_favors_never_sent(self) -> None:
        # t2 needs 600s; both eligible at 700s and no hint match: t2 priority 5 wins anyway
        top = self.bank.pick(now=1000.0, silence_sec=700, live_text="")
        self.assertEqual(top.id, "t2")

    def test_absent_file_returns_none(self) -> None:
        empty = TopicBank(str(Path(tempfile.mkdtemp())))
        self.assertIsNone(empty.pick(now=1.0, silence_sec=99999, live_text="x"))

    def test_hot_reload_adds_topic(self) -> None:
        # exhaust the pool: t2 (highest priority) then t1
        t2 = self.bank.pick(now=1000.0, silence_sec=9999, live_text="")
        self.assertEqual(t2.id, "t2")
        self.bank.mark_sent(t2, now=1000.0)
        t1 = self.bank.pick(now=2000.0, silence_sec=9999, live_text="")
        self.assertEqual(t1.id, "t1")
        self.bank.mark_sent(t1, now=2000.0)
        self.assertIsNone(self.bank.pick(now=3000.0, silence_sec=9999, live_text=""))
        time.sleep(0.01)
        data = json.loads((self.dir / "topic_bank.json").read_text("utf-8"))
        data.append({"id": "t4", "content": "新话题", "category": "日常",
                     "context_hints": [], "priority": 1, "min_silence_seconds": 0})
        (self.dir / "topic_bank.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        top = self.bank.pick(now=4000.0, silence_sec=9999, live_text="")
        self.assertIsNotNone(top)
        self.assertEqual(top.id, "t4")


if __name__ == "__main__":
    unittest.main()
