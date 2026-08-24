"""Tests for services/poke.py (G11, stdlib only)."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from services.poke import PokeService
except ImportError:
    from astrbot_plugin_persona_agent.services.poke import PokeService


class PokeDecideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.svc = PokeService(
            str(self.dir),
            bot_qq="3589783410",
            cooldown_sec=300.0,
            hourly_cap=4,
            serious_keywords=["你他妈", "傻逼"],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _decide(self, poker="337934842", known=True, text="", now=None):
        return self.svc.decide(now_utc=now, poker=poker, group_id="881438753",
                               known_member=known, recent_text=text)

    def test_empty_poker_rejected(self) -> None:
        self.assertEqual(self._decide(poker="")[0], False)

    def test_unknown_member_never_responds(self) -> None:
        self.assertEqual(self._decide(known=False)[0], False)
        self.assertEqual(self._decide(known=False)[1], "unknown_member")

    def test_known_member_ok(self) -> None:
        ok, reason = self._decide()
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_cooldown_blocks_then_expires(self) -> None:
        t0 = 1_000_000.0
        ok, _ = self._decide(now=t0)
        self.assertTrue(ok)
        self.svc.record(now_utc=t0, poker="337934842", group_id="881438753", responded=True, reason="ok")
        ok2, reason2 = self._decide(now=t0 + 60)
        self.assertFalse(ok2)
        self.assertIn("cooldown", reason2)
        ok3, _ = self._decide(now=t0 + 301)
        self.assertTrue(ok3)

    def test_hourly_cap(self) -> None:
        t0 = 2_000_000.0
        for i in range(4):
            ok, _ = self._decide(now=t0 + i, poker=f"u{i}")
            self.assertTrue(ok)
            self.svc.record(now_utc=t0 + i, poker=f"u{i}", group_id="g",
                            responded=True, reason="ok")
        ok, reason = self._decide(now=t0 + 5, poker="u99")
        self.assertFalse(ok)
        self.assertEqual(reason, "hourly_cap")

    def test_hour_roll_resets_cap(self) -> None:
        # 23:50 -> 00:00 local (tz=8): year flip to keep it simple via roll hours
        t0 = 2_100_000.0
        self.svc._hour_key = self.svc._local_hour(t0) - 1  # force different hour
        self.assertEqual(self.svc._hour_count, 0)

    def test_serious_context_suppresses(self) -> None:
        ok, reason = self._decide(text="你他妈 别吵了")
        self.assertFalse(ok)
        self.assertEqual(reason, "serious_context")

    def test_benign_context_passes(self) -> None:
        ok, _ = self._decide(text="今晚打游戏吗")
        self.assertTrue(ok)

    def test_record_updates_state_only_when_responded(self) -> None:
        t0 = 3_000_000.0
        self.svc.record(now_utc=t0, poker="p1", group_id="g", responded=False, reason="x")
        self.assertNotIn("p1", self.svc._last_poke_ts)
        self.assertEqual(self.svc._hour_count, 0)
        self.svc.record(now_utc=t0, poker="p1", group_id="g", responded=True, reason="ok")
        self.assertIn("p1", self.svc._last_poke_ts)
        self.assertEqual(self.svc._hour_count, 1)


class PokeLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.svc = PokeService(str(self.dir), bot_qq="3589783410")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_log_appended_jsonl(self) -> None:
        self.svc.record(now_utc=4_000_000.0, poker="337934842", group_id="881438753",
                        responded=True, reason="ok")
        lines = (self.dir / "poke_log.jsonl").read_text("utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["poker"], "337934842")
        self.assertEqual(entry["responded"], True)
        self.assertEqual(entry["reason"], "ok")

    def test_keywords_hot_reload_from_file(self) -> None:
        kw_path = self.dir / "conflict_keywords.json"
        kw_path.write_text(json.dumps({"keywords": ["拉黑"]}), encoding="utf-8")
        svc2 = PokeService(str(self.dir), bot_qq="3589783410")
        self.assertIn("拉黑", svc2._kw)
        # append a new keyword; mtime must change -> reload on next check
        time.sleep(0.01)
        kw_path.write_text(json.dumps({"keywords": ["拉黑", "绝交"]}), encoding="utf-8")
        ok, reason = svc2.decide(now_utc=4_100_000.0, poker="p9", group_id="g",
                                 known_member=True, recent_text="再不回就绝交了")
        self.assertFalse(ok)
        self.assertEqual(reason, "serious_context")


if __name__ == "__main__":
    unittest.main()
