"""Tests for services/summary.py (G13, stdlib only)."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from services.summary import (
        SummaryService, weekly_window, monthly_window, list_diaries,
        sample_days, build_prompt, append_summary, build_phi,
        split_digest_body, digest_fresh_today,
    )
    from services.style_profile import MEMORY_DIGEST_FILE  # noqa: E402
except ImportError:
    from astrbot_plugin_persona_agent.services.summary import (
        SummaryService, weekly_window, monthly_window, list_diaries,
        sample_days, build_prompt, append_summary, build_phi,
        split_digest_body, digest_fresh_today,
    )
    from astrbot_plugin_persona_agent.services.style_profile import (
        MEMORY_DIGEST_FILE,
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
        gid = "123456789"
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
                       {"role": "user", "name": "小明", "content": "今天好累哦"},
                       {"role": "assistant", "content": "口癖庚"},
                       {"role": "user", "name": "群友A", "content": "吃点好的"},
                       {"role": "user", "name": "", "content": "   "},
                   ]}
        (self.dir / f"session_{gid}_2026-08-20.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_list_diaries_range_and_group(self) -> None:
        recs = list_diaries(self.dir / "daily_diary.jsonl", "123456789",
                            date(2026, 8, 17), date(2026, 8, 23))
        days = [r["day"] for r in recs]
        self.assertEqual(days, ["2026-08-17", "2026-08-23"])

    def test_sample_days_only_user_messages(self) -> None:
        samples = sample_days(str(self.dir), "123456789",
                              date(2026, 8, 17), date(2026, 8, 23), max_per_day=6)
        self.assertEqual(len(samples), 2)
        self.assertIn("今天好累哦", samples[0])
        self.assertNotIn("口癖庚", "".join(samples))

    def test_sample_cap_per_day(self) -> None:
        samples = sample_days(str(self.dir), "123456789",
                              date(2026, 8, 17), date(2026, 8, 23), max_per_day=1)
        self.assertEqual(len(samples), 1)

    def test_collect_assembles(self) -> None:
        svc = SummaryService(str(self.dir))
        c = svc.collect("weekly", "123456789", today=date(2026, 8, 24))
        self.assertEqual(c["kind"], "weekly")
        self.assertEqual(c["label"], "2026-W34")
        self.assertEqual(c["n_diaries"], 2)
        self.assertEqual(c["n_samples"], 2)

    def test_prompt_contains_materials(self) -> None:
        svc = SummaryService(str(self.dir))
        c = svc.collect("weekly", "123456789", today=date(2026, 8, 24))
        p = build_prompt(c["kind"], c["group_id"], c["label"], c["diaries"], c["samples"])
        # C31：原料块只放原料（指令与格式在 build_phi 的 system 块里）
        self.assertNotIn("2026-W34", p, "周期标签已挪进 PHI")
        self.assertIn("周一 summary", p)
        self.assertIn("今天好累哦", p)
        phi = build_phi(c["kind"])
        self.assertIn("【这一周结束了】", phi)
        self.assertIn("---", phi, "格式围栏必须约定死（summary_v2 §2）")
        self.assertIn("digest:", phi)

    def test_append_summary_persists(self) -> None:
        svc = SummaryService(str(self.dir))
        append_summary(svc.output_path("weekly"), "weekly", "123456789",
                       "2026-W34", "本周摘要正文",
                       {"n_diaries": 2, "n_samples": 2})
        lines = svc.output_path("weekly").read_text("utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        loaded = json.loads(lines[0])
        self.assertEqual(loaded["period"], "2026-W34")
        # C31：不再写 summary —— 切开成 digest + body
        self.assertNotIn("summary", loaded)
        self.assertEqual(loaded["body"], "本周摘要正文")
        self.assertEqual(loaded["digest"], "")
        self.assertTrue(loaded.get("digest_missing"), "没按格式产出必须可统计")

    def test_empty_period_collect(self) -> None:
        svc = SummaryService(str(Path(tempfile.mkdtemp())))
        c = svc.collect("monthly", "123456789", today=date(2026, 8, 24))
        self.assertEqual(c["n_diaries"], 0)
        self.assertEqual(c["n_samples"], 0)


class DiarySourceTests(unittest.TestCase):
    """B-024（2026-09-17）：周/月报必须读**现行**日记路径。

    日记自 `e566116`（2026-09-08）起写在 `logs/<gid>/daily_diary.jsonl`，
    而 `collect()` 一直读数据根目录的同名文件（停在 2026-08-29）→
    生产周报 `n_diaries` 恒为 0，且 `_propose_relations` 的 `not diaries`
    守卫让**关系提升提案永久不产出**。
    """

    GID = "100000001"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, rel: str, recs: list[dict]) -> Path:
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
            encoding="utf-8")
        return p

    def _collect(self) -> dict:
        return SummaryService(str(self.dir)).collect(
            "weekly", self.GID, today=date(2026, 9, 14))  # 窗口 09-07~09-13

    def test_collect_reads_per_group_diary(self) -> None:
        """只在 `logs/<gid>/` 有日记时也必须收集到（现行路径）。"""
        self._write(f"logs/{self.GID}/daily_diary.jsonl", [
            {"day": "2026-09-11", "group_id": self.GID, "summary": "十一号", "n_messages": 46},
            {"day": "2026-09-13", "group_id": self.GID, "summary": "十三号", "n_messages": 1518},
        ])
        c = self._collect()
        self.assertEqual(c["n_diaries"], 2, "未读到 logs/<gid>/ 下的日记（B-024 未修）")
        self.assertEqual([d["day"] for d in c["diaries"]],
                         ["2026-09-11", "2026-09-13"])

    def test_collect_legacy_root_path_compatible(self) -> None:
        """旧根目录路径仍需兼容（历史部署只有它）。"""
        self._write("daily_diary.jsonl", [
            {"day": "2026-09-13", "group_id": self.GID, "summary": "旧路径", "n_messages": 10},
        ])
        c = self._collect()
        self.assertEqual(c["n_diaries"], 1)
        self.assertEqual(c["diaries"][0]["summary"], "旧路径")

    def test_collect_merges_both_paths_preferring_per_group(self) -> None:
        """两条路径同一天都有 → 只留一条，且**现行路径**优先。"""
        self._write("daily_diary.jsonl", [
            {"day": "2026-09-13", "group_id": self.GID, "summary": "旧的", "n_messages": 1},
        ])
        self._write(f"logs/{self.GID}/daily_diary.jsonl", [
            {"day": "2026-09-13", "group_id": self.GID, "summary": "现行的", "n_messages": 2},
        ])
        c = self._collect()
        self.assertEqual(c["n_diaries"], 1)
        self.assertEqual(c["diaries"][0]["summary"], "现行的")

    def test_collect_dedups_same_day_within_file(self) -> None:
        """同一文件内同一天多条（B-018 遗留重复）→ 取最新一条。"""
        self._write(f"logs/{self.GID}/daily_diary.jsonl", [
            {"day": "2026-09-13", "group_id": self.GID, "summary": "第一条", "n_messages": 1},
            {"day": "2026-09-13", "group_id": self.GID, "summary": "第二条", "n_messages": 2},
            {"day": "2026-09-13", "group_id": self.GID, "summary": "第三条", "n_messages": 3},
        ])
        c = self._collect()
        self.assertEqual(c["n_diaries"], 1)
        self.assertEqual(c["diaries"][0]["summary"], "第三条")

    def test_collect_ignores_other_group_and_out_of_window(self) -> None:
        self._write(f"logs/{self.GID}/daily_diary.jsonl", [
            {"day": "2026-09-13", "group_id": self.GID, "summary": "命中", "n_messages": 1},
            {"day": "2026-09-13", "group_id": "other", "summary": "别群", "n_messages": 1},
            {"day": "2026-08-01", "group_id": self.GID, "summary": "窗口外", "n_messages": 1},
        ])
        c = self._collect()
        self.assertEqual(c["n_diaries"], 1)
        self.assertEqual(c["diaries"][0]["summary"], "命中")


if __name__ == "__main__":
    unittest.main()


class TestC31DigestBody(unittest.TestCase):
    """C31：摘要改 digest + body 两字段（summary_v2 §2/§3/§7）。"""

    def test_split_with_fences(self):
        digest, body = split_digest_body(
            "---\ndate: 2026-09-17\ndigest: 成员戊拔智齿，大家轮流摸摸\n---\n"
            "今天群里从早上就很热闹……")
        self.assertEqual(digest, "成员戊拔智齿，大家轮流摸摸")
        self.assertTrue(body.startswith("今天群里"))

    def test_split_tolerates_missing_fences(self):
        """格式没遵守 → **正文照收**，只是没有 digest（不能整篇丢掉）。"""
        digest, body = split_digest_body("今天就这样过去了。")
        self.assertEqual(digest, "")
        self.assertEqual(body, "今天就这样过去了。")

    def test_split_tolerates_fences_without_digest_key(self):
        digest, body = split_digest_body("---\ndate: 2026-09-17\n---\n正文")
        self.assertEqual(digest, "")
        self.assertEqual(body, "正文")

    def test_split_strips_code_fence(self):
        """模型把整段包进代码围栏时先剥掉（围栏字符用 chr 拼，避免与源码样式冲突）。"""
        fence = chr(96) * 3
        digest, body = split_digest_body(
            fence + "\n---\ndigest: 一句话\n---\n正文\n" + fence)
        self.assertEqual(digest, "一句话")
        self.assertEqual(body, "正文")

    def test_append_keeps_digest_and_body(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "weekly_summary.jsonl"
            rec = append_summary(p, "weekly", "g1", "2026-W37",
                                 "---\nweek: W37\ndigest: 一句话\n---\n正文内容", {})
            self.assertEqual(rec["digest"], "一句话")
            self.assertEqual(rec["body"], "正文内容")
            self.assertFalse(rec.get("digest_missing"))

    def test_append_body_only_uses_digest_as_body(self):
        """只有 digest 没有正文 → 正文回落 digest，别让上一层拿到空原料。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "weekly_summary.jsonl"
            rec = append_summary(p, "weekly", "g1", "2026-W37",
                                 "---\ndigest: 只有一句话\n---\n", {})
            self.assertEqual(rec["body"], "只有一句话")
            self.assertTrue(rec.get("body_from_digest"))

    def test_list_summaries_reads_body_with_legacy_fallback(self):
        from services.summary import list_summaries
        from datetime import date as _d
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "monthly_summary.jsonl"
            with open(p, "w", encoding="utf-8") as f:
                f.write(json.dumps({"kind": "monthly", "group_id": "g1",
                                    "period": "2026-08", "body": "新格式正文"},
                                   ensure_ascii=False) + "\n")
                f.write(json.dumps({"kind": "monthly", "group_id": "g1",
                                    "period": "2026-07", "summary": "旧格式正文"},
                                   ensure_ascii=False) + "\n")
            got = list_summaries(p, "g1", _d(2026, 1, 1), _d(2026, 12, 31))
            self.assertEqual([r["body"] for r in got], ["旧格式正文", "新格式正文"])

    def test_each_kind_has_its_own_phi(self):
        for kind, marker in (("daily", "【今天结束了】"), ("weekly", "【这一周结束了】"),
                             ("monthly", "【这个月结束了】"), ("yearly", "【这一年结束了】")):
            self.assertIn(marker, build_phi(kind))
        self.assertEqual(build_phi("unknown"), "")


class TestDigestLayers(unittest.TestCase):
    """C4：§3「历史群聊摘要」的四层组装（`summary_v2.md` §4.2：**各层不重叠**）。

    为什么要不重叠：越久远越粗，才有**遗忘痕迹**；重叠会让同一件事在上下文里说两遍，
    而 §3 是**冻结头部**、一天付一次钱，重复是纯浪费。
    """

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.gid = "g1"
        d = Path(self.td.name) / "logs" / self.gid
        d.mkdir(parents=True)
        self.diary_path = d / "daily_diary.jsonl"

    def tearDown(self):
        self.td.cleanup()

    def _diary(self, rows):
        self.diary_path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8")

    def _layer(self, name, rows):
        p = Path(self.td.name) / name
        p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                     encoding="utf-8")

    def test_diaries_only(self):
        self._diary([{"day": f"2026-09-{d:02d}", "group_id": self.gid,
                      "digest": f"第{d}天的事"} for d in range(10, 21)])
        from services.summary import build_digest_layers
        res = build_digest_layers(self.td.name, self.gid)
        days = res["layers"]["recent_days"]
        self.assertEqual(len(days), 7, "上限 7 条")
        self.assertTrue(days[0].startswith("09-14 "), days[0])
        self.assertTrue(days[-1].startswith("09-20 "), days[-1])
        for k in ("recent_weeks", "recent_months", "older"):
            self.assertEqual(res["layers"][k], [], f"{k} 无内容时整段不出现")

    def test_weekly_pushes_diaries_out(self):
        """日记层只报**最近一篇周记结束日之后**的那些天。"""
        self._diary([{"day": "2026-09-10", "group_id": self.gid, "digest": "周内"},
                     {"day": "2026-09-14", "group_id": self.gid, "digest": "周后"}])
        # ISO 2026-W37 = 09-07 .. 09-13
        self._layer("weekly_summary.jsonl",
                    [{"kind": "weekly", "group_id": self.gid,
                      "period": "2026-W37", "digest": "那一周"}])
        from services.summary import build_digest_layers
        res = build_digest_layers(self.td.name, self.gid)
        days = res["layers"]["recent_days"]
        self.assertEqual([d.split()[0] for d in days], ["09-14"],
                         "已被周记覆盖的那天不该再出现（各层不重叠）")
        self.assertEqual(res["layers"]["recent_weeks"], ["2026-W37 那一周"])

    def test_monthly_pushes_weeks_out(self):
        self._layer("weekly_summary.jsonl", [
            {"kind": "weekly", "group_id": self.gid, "period": "2026-W30",
             "digest": "七月那周"},
            {"kind": "weekly", "group_id": self.gid, "period": "2026-W38",
             "digest": "九月那周"},
        ])
        self._layer("monthly_summary.jsonl", [
            {"kind": "monthly", "group_id": self.gid, "period": "2026-08",
             "digest": "八月"},
        ])
        from services.summary import build_digest_layers
        res = build_digest_layers(self.td.name, self.gid)
        self.assertEqual(res["layers"]["recent_weeks"], ["2026-W38 九月那周"],
                         "八月（含 W30）已由月记覆盖")
        self.assertEqual(res["layers"]["recent_months"], ["2026-08 八月"])

    def test_yearly_pushes_months_out(self):
        self._layer("monthly_summary.jsonl", [
            {"kind": "monthly", "group_id": self.gid, "period": "2025-06",
             "digest": "去年六月"},
            {"kind": "monthly", "group_id": self.gid, "period": "2026-01",
             "digest": "今年一月"},
        ])
        self._layer("yearly_summary.jsonl", [
            {"kind": "yearly", "group_id": self.gid, "period": "2025",
             "digest": "二〇二五"},
        ])
        from services.summary import build_digest_layers
        res = build_digest_layers(self.td.name, self.gid)
        self.assertEqual(res["layers"]["recent_months"], ["2026-01 今年一月"])
        self.assertEqual(res["layers"]["older"], ["2025 二〇二五"])

    def test_records_without_digest_are_skipped_and_counted(self):
        """「无 digest 视作空」（用户定案）：旧记录（只有 summary）不进 §3。

        但跳过的条数**必须可统计** —— 否则「长期记忆一直是空的」看不出原因。
        """
        self._diary([
            {"day": "2026-09-19", "group_id": self.gid, "summary": "旧格式正文"},
            {"day": "2026-09-20", "group_id": self.gid, "digest": "新格式"},
        ])
        from services.summary import build_digest_layers
        res = build_digest_layers(self.td.name, self.gid)
        self.assertEqual(res["layers"]["recent_days"], ["09-20 新格式"])
        self.assertEqual(res["stats"].get("skipped_no_digest"), 1)

    def test_empty_when_no_data(self):
        from services.summary import build_digest_layers
        res = build_digest_layers(self.td.name, self.gid)
        self.assertEqual(res["stats"]["total_lines"], 0)
        self.assertTrue(all(v == [] for v in res["layers"].values()))

    def test_caps_are_enforced(self):
        self._diary([{"day": f"2026-08-{d:02d}", "group_id": self.gid,
                      "digest": f"d{d}"} for d in range(1, 29)])
        from services.summary import build_digest_layers
        res = build_digest_layers(self.td.name, self.gid)
        self.assertEqual(len(res["layers"]["recent_days"]), 7)

    def test_write_memory_digest_is_atomic_and_readable(self):
        """落盘形态必须能被 StyleProfile.memory_layers() 直接读。"""
        self._diary([{"day": "2026-09-20", "group_id": self.gid, "digest": "今天"}])
        from services.summary import write_memory_digest
        write_memory_digest(self.td.name, self.gid)
        p = Path(self.td.name) / "memory_digest.json"
        self.assertTrue(p.exists())
        self.assertFalse(p.with_suffix(".json.tmp").exists(), "临时文件必须已 rename")
        from services.style_profile import StyleProfile
        sp = StyleProfile(self.td.name)
        self.assertEqual(sp.memory_layers().get("recent_days"), ["09-20 今天"])
        self.assertIn("09-20 今天", sp.system_prompt())


class TestDigestHardening(unittest.TestCase):
    """独立核验第 6 轮的非阻塞项（N-1 / N-3 / N-5）。"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        d = Path(self.td.name) / "logs" / "g1"
        d.mkdir(parents=True)
        (d / "daily_diary.jsonl").write_text(
            json.dumps({"day": "2026-09-20", "group_id": "g1", "digest": "今天"},
                       ensure_ascii=False) + "\n", encoding="utf-8")

    def tearDown(self):
        self.td.cleanup()

    def test_unchanged_layers_do_not_rewrite_file(self):
        """N-1：内容没变就不落盘 —— 否则白造一次「［设定更新］」块。"""
        from services.summary import write_memory_digest
        p = Path(self.td.name) / "memory_digest.json"
        write_memory_digest(self.td.name, "g1")
        first = p.read_text(encoding="utf-8")
        res = write_memory_digest(self.td.name, "g1")
        self.assertTrue(res.get("unchanged"), "第二次必须识别为未变化")
        self.assertEqual(p.read_text(encoding="utf-8"), first, "文件不该被重写")
        # 内容真变了就要写
        d = Path(self.td.name) / "logs" / "g1" / "daily_diary.jsonl"
        with open(d, "a", encoding="utf-8") as f:
            f.write(json.dumps({"day": "2026-09-21", "group_id": "g1",
                                "digest": "新的一天"}, ensure_ascii=False) + "\n")
        res2 = write_memory_digest(self.td.name, "g1")
        self.assertFalse(res2.get("unchanged"))
        self.assertIn("新的一天", p.read_text(encoding="utf-8"))

    def test_bad_period_is_skipped_and_counted(self):
        """N-3：period/day 非法时**不进 §3**（此前会原样漏成一行垃圾）。"""
        from services.summary import build_digest_layers
        (Path(self.td.name) / "weekly_summary.jsonl").write_text(
            json.dumps({"kind": "weekly", "group_id": "g1",
                        "period": "坏-W w坏", "digest": "x"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        with open(Path(self.td.name) / "logs" / "g1" / "daily_diary.jsonl", "a",
                  encoding="utf-8") as f:
            f.write(json.dumps({"day": "不是日期", "group_id": "g1", "digest": "y"},
                               ensure_ascii=False) + "\n")
        res = build_digest_layers(self.td.name, "g1")
        self.assertEqual(res["layers"]["recent_weeks"], [])
        self.assertNotIn("不是日期", "\n".join(res["layers"]["recent_days"]))
        self.assertGreaterEqual(res["stats"].get("skipped_bad_period", 0), 2)

    def test_staleness_is_observable(self):
        """N-5：读侧要能报出「这版 §3 是什么时候组装的」。"""
        from services.summary import write_memory_digest
        from services.style_profile import StyleProfile
        write_memory_digest(self.td.name, "g1")
        sp = StyleProfile(self.td.name)
        sp.memory_layers()
        self.assertTrue(sp.last_memory_generated_at, "generated_at 必须被读出来")
        self.assertEqual(sp.last_memory_layers_n, 1)


class TestCrossYear(unittest.TestCase):
    """跨年核查（用户 2026-09-20 点名要核）。

    两处容易错的地方：
      ① **ISO 周的周边界跨年** —— 2026-W01 其实是 2025-12-29 .. 2026-01-04（周一在去年）；
      ② **年/月/日三层的边界**在 1 月初同时生效，容易漏层或重层。
    """

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.gid = "g1"
        d = Path(self.td.name) / "logs" / self.gid
        d.mkdir(parents=True)
        self.diary_path = d / "daily_diary.jsonl"

    def tearDown(self):
        self.td.cleanup()

    def _w(self, name, rows):
        (Path(self.td.name) / name).write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8")

    def test_iso_week_1_spans_the_year_boundary(self):
        """2026-W01 = 2025-12-29 .. 2026-01-04 → 12-30 归它、01-05 不归它。"""
        from services.summary import build_digest_layers
        self._w("weekly_summary.jsonl", [
            {"kind": "weekly", "group_id": self.gid, "period": "2026-W01",
             "digest": "跨年那周"}])
        self.diary_path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [
            {"day": "2025-12-30", "group_id": self.gid, "digest": "周内"},
            {"day": "2026-01-05", "group_id": self.gid, "digest": "周后"},
        ]) + "\n", encoding="utf-8")
        res = build_digest_layers(self.td.name, self.gid)
        self.assertEqual([x.split()[0] for x in res["layers"]["recent_days"]], ["01-05"],
                         "12-30 在 W01 覆盖期内 → 该退场；01-05 在期外 → 留下")
        self.assertEqual(res["layers"]["recent_weeks"], ["2026-W01 跨年那周"])

    def test_year_boundary_keeps_each_layer_once(self):
        """1 月初：年/月/周/日四层的边界同时生效 —— **不重不漏**。"""
        from services.summary import build_digest_layers
        self._w("yearly_summary.jsonl", [
            {"kind": "yearly", "group_id": self.gid, "period": "2025",
             "digest": "二〇二五"}])
        self._w("monthly_summary.jsonl", [
            {"kind": "monthly", "group_id": self.gid, "period": "2025-12",
             "digest": "去年十二月"},
            {"kind": "monthly", "group_id": self.gid, "period": "2026-01",
             "digest": "今年一月"}])
        self._w("weekly_summary.jsonl", [
            {"kind": "weekly", "group_id": self.gid, "period": "2025-W52",
             "digest": "去年最后一周"},
            {"kind": "weekly", "group_id": self.gid, "period": "2026-W02",
             "digest": "今年第二周"}])
        self.diary_path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [
            {"day": "2026-01-05", "group_id": self.gid, "digest": "周内"},
            {"day": "2026-01-12", "group_id": self.gid, "digest": "周后"},
        ]) + "\n", encoding="utf-8")
        res = build_digest_layers(self.td.name, self.gid)
        L2 = res["layers"]
        # 年记只留 2025；月记只留 2026-01（2025-12 被年记覆盖）
        self.assertEqual(L2["older"], ["2025 二〇二五"])
        self.assertEqual(L2["recent_months"], ["2026-01 今年一月"])
        # 周记层**为空**：W52 结束于 2025-12-28（被 2025 年记覆盖）；
        # W02（01-05..01-11）落在已存在的 2026-01 月记覆盖月**之内** —— 两层都不该出现。
        # 这不是漏洞：这段时间由**月记那一层**代表（下一条断言它在）。
        self.assertEqual(L2["recent_weeks"], [])
        self.assertIn("2026-01 今年一月", L2["recent_months"])
        # 日记只留 01-12（01-05 在 W02=01-05..01-11 之内）
        self.assertEqual([x.split()[0] for x in L2["recent_days"]], ["01-12"])
        # 每层最多一次、没有重复行
        all_lines = [x for v in L2.values() for x in v]
        self.assertEqual(len(all_lines), len(set(all_lines)))

    def test_window_functions_at_new_year(self):
        """周/月/年窗口在 1 月 1 日的行为（年记读**上一个完整自然年**）。"""
        from services.summary import monthly_window, weekly_window, yearly_window
        from datetime import date as _d
        self.assertEqual(monthly_window(_d(2026, 1, 1))[2], "2025-12")
        self.assertEqual(yearly_window(_d(2026, 1, 1))[2], "2025")
        s, e, label = weekly_window(_d(2026, 1, 1))
        self.assertEqual(e, _d(2025, 12, 31))
        self.assertEqual(s, _d(2025, 12, 25))
        # ⚠️ ISO 周边界的坑：2025-12-31 落在 **2026 年的第 1 个 ISO 周**
        # （周一起于 2025-12-29）→ 标签是 2026-W01，不是 2025-W52。
        # 人读 §3 会觉得别扭，但这是 ISO 标准，且与 `_week_end("2026-W01")=2026-01-04` 自洽。
        self.assertEqual(label, "2026-W01")


class TestDigestFreshTodayB056(unittest.TestCase):
    """🔴 B-056（2026-09-22 线上）：每条消息都重读摘要并刷日志（当天 7269 条）。

    修法 = 按 `generated_at` 的**本地日期**判「今天是否已组装」；异常一律按陈旧处理。
    这条判据必须可单测（不能只活在 main.py 里 —— 测试从不导入它）。
    """

    def _write(self, td: str, iso: str) -> None:
        (Path(td) / MEMORY_DIGEST_FILE).write_text(
            json.dumps({"generated_at": iso, "layers": {}}), encoding="utf-8")

    def test_fresh_when_generated_now(self):
        with tempfile.TemporaryDirectory() as td:
            now = datetime.now(timezone.utc)
            self._write(td, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
            self.assertTrue(digest_fresh_today(td))

    def test_stale_when_generated_yesterday(self):
        with tempfile.TemporaryDirectory() as td:
            # 用「本地昨天」构造，避免 UTC 与本地跨日造成假阴性
            y = datetime.now().astimezone() - timedelta(days=1)
            self._write(td, y.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            self.assertFalse(digest_fresh_today(td))

    def test_missing_file_is_stale(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(digest_fresh_today(td))

    def test_corrupt_or_shapeless_is_stale(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / MEMORY_DIGEST_FILE).write_text("{坏 JSON", encoding="utf-8")
            self.assertFalse(digest_fresh_today(td))
            (Path(td) / MEMORY_DIGEST_FILE).write_text("{}", encoding="utf-8")
            self.assertFalse(digest_fresh_today(td))
            (Path(td) / MEMORY_DIGEST_FILE).write_text(
                json.dumps({"generated_at": "不是时间"}), encoding="utf-8")
            self.assertFalse(digest_fresh_today(td))

    def test_constant_is_imported_not_shadowed(self):
        """回归钉：常量住在 style_profile —— 本模块少一个 import 就会 NameError，
        而它会被 `except` 吞成「永远不新鲜」= **每条消息都重建**（正是 B-056 本身）。
        """
        from services import summary as S
        self.assertTrue(hasattr(S, "MEMORY_DIGEST_FILE"), "必须在本模块可见")
        self.assertEqual(S.MEMORY_DIGEST_FILE, "memory_digest.json")
        # 反向自证：把常量改名 → 函数必须**仍然基于同一份文件名**（防两处漂移）
        self.assertIn(S.MEMORY_DIGEST_FILE,
                      (Path(__file__).resolve().parents[1] / "services" / "style_profile.py")
                      .read_text(encoding="utf-8"))

    def test_explicit_cst_not_process_local(self):
        """🔴 P2（独立审查）：判据必须用**显式 +8**，不能跟进程本地时区走。

        探针实测 TZ=UTC 时旧写法 ~18h/天恒不新鲜 → 退化成每条消息重读四层摘要，
        而日志已降 DEBUG = 完全静默（B-056 换马甲）。这里用 now= 钉住这条。
        """
        cst = timezone(timedelta(hours=8))
        with tempfile.TemporaryDirectory() as td:
            local = datetime(2026, 9, 22, 1, 0, tzinfo=cst)      # CST 凌晨 1 点
            self._write(td, local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            self.assertTrue(digest_fresh_today(td, now=local), "同一 CST 日应算新鲜")
            self.assertFalse(
                digest_fresh_today(td, now=local + timedelta(days=1)), "次日应算陈旧")

    def test_cross_midnight_boundary(self):
        """跨日边界：CST 23:59 组装 → 次日 00:01 必须判陈旧（否则 §3 陈旧一整天）。"""
        cst = timezone(timedelta(hours=8))
        with tempfile.TemporaryDirectory() as td:
            t1 = datetime(2026, 9, 22, 23, 59, tzinfo=cst)
            self._write(td, t1.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            self.assertTrue(digest_fresh_today(td, now=t1))
            self.assertFalse(digest_fresh_today(
                td, now=t1 + timedelta(minutes=2)), "过零点就该重建")
