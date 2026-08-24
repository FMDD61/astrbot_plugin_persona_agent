"""Tests for services/summary.py (G13, stdlib only)."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from services.summary import (
        SummaryService, weekly_window, monthly_window, list_diaries,
        sample_days, build_prompt, append_summary,
    )
except ImportError:
    from astrbot_plugin_persona_agent.services.summary import (
        SummaryService, weekly_window, monthly_window, list_diaries,
        sample_days, build_prompt, append_summary,
    )


class WindowTests(unittest.TestCase):
    def test_weekly_window(self) -> None:
        start, end, label = weekly_window(date(2026, 8, 24))  # Monday
        self.assertEqual(end, date(2026, 8, 23))  # Sunday
        self.assertEqual(start, date(2026, 8, 17))
        self.assertEqual(label, "2026-W34")

    def test_monthly_window(self) -> None:
        start, end, label = monthly_window(date(2026, 8, 24))
        self.assertEqual(start, date(2026, 7, 1))
        self.assertEqual(end, date(2026, 7, 31))
        self.assertEqual(label, "2026-07")

    def test_monthly_window_january(self) -> None:
        start, end, label = monthly_window(date(2026, 1, 15))
        self.assertEqual((start, end), (date(2025, 12, 1), date(2025, 12, 31)))
        self.assertEqual(label, "2025-12")


class CollectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        gid = "881438753"
        # two diaries inside window, one outside, one other group
        diary = [
            {"day": "2026-08-17", "group_id": gid, "summary": "周一 summary", "n_messages": 3},
            {"day": "2026-08-23", "group_id": gid, "summary": "周日 summary", "n_messages": 5},
            {"day": "2026-08-10", "group_id": gid, "summary": "old", "n_messages": 1},
            {"day": "2026-08-20", "group_id": "other", "summary": "other", "n_messages": 1},
            "corrupt-line-not-json",
        ]
        (self.dir / "daily_diary.jsonl").write_text(
            "\n".join(json.dumps(x, ensure_ascii=False) if isinstance(x, dict) else x for x in diary)
            + "\n", encoding="utf-8")
        # session file with user + assistant messages
        payload = {"version": 2, "group_id": gid, "day": "2026-08-20",
                   "messages": [
                       {"role": "user", "name": "焦糖", "content": "今天好累哦"},
                       {"role": "assistant", "content": "搓搓"},
                       {"role": "user", "name": "群友A", "content": "吃点好的"},
                       {"role": "user", "name": "", "content": "   "},
                   ]}
        (self.dir / f"session_{gid}_2026-08-20.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_list_diaries_range_and_group(self) -> None:
        recs = list_diaries(self.dir / "daily_diary.jsonl", "881438753",
                            date(2026, 8, 17), date(2026, 8, 23))
        days = [r["day"] for r in recs]
        self.assertEqual(days, ["2026-08-17", "2026-08-23"])

    def test_sample_days_only_user_messages(self) -> None:
        samples = sample_days(str(self.dir), "881438753",
                              date(2026, 8, 17), date(2026, 8, 23), max_per_day=6)
        self.assertEqual(len(samples), 2)
        self.assertIn("今天好累哦", samples[0])
        self.assertNotIn("搓搓", "".join(samples))

    def test_sample_cap_per_day(self) -> None:
        samples = sample_days(str(self.dir), "881438753",
                              date(2026, 8, 17), date(2026, 8, 23), max_per_day=1)
        self.assertEqual(len(samples), 1)

    def test_collect_assembles(self) -> None:
        svc = SummaryService(str(self.dir))
        c = svc.collect("weekly", "881438753", today=date(2026, 8, 24))
        self.assertEqual(c["kind"], "weekly")
        self.assertEqual(c["label"], "2026-W34")
        self.assertEqual(c["n_diaries"], 2)
        self.assertEqual(c["n_samples"], 2)

    def test_prompt_contains_materials(self) -> None:
        svc = SummaryService(str(self.dir))
        c = svc.collect("weekly", "881438753", today=date(2026, 8, 24))
        p = build_prompt(c["kind"], c["group_id"], c["label"], c["diaries"], c["samples"])
        self.assertIn("2026-W34", p)
        self.assertIn("周一 summary", p)
        self.assertIn("今天好累哦", p)

    def test_append_summary_persists(self) -> None:
        svc = SummaryService(str(self.dir))
        append_summary(svc.output_path("weekly"), "weekly", "881438753",
                       "2026-W34", "本周摘要正文",
                       {"n_diaries": 2, "n_samples": 2})
        lines = svc.output_path("weekly").read_text("utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        loaded = json.loads(lines[0])
        self.assertEqual(loaded["period"], "2026-W34")
        self.assertEqual(loaded["summary"], "本周摘要正文")

    def test_empty_period_collect(self) -> None:
        svc = SummaryService(str(Path(tempfile.mkdtemp())))
        c = svc.collect("monthly", "881438753", today=date(2026, 8, 24))
        self.assertEqual(c["n_diaries"], 0)
        self.assertEqual(c["n_samples"], 0)


if __name__ == "__main__":
    unittest.main()
