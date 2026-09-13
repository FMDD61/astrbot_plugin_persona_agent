# -*- coding: utf-8 -*-
"""KG 入库质量门测试（B-014，2026-09-13）。

背景：原 RAG 语料经历过第二轮清洗（7075 → 4704 对），KG 入库此前完全没有
等价的一道，实测 distinct topic 实体 3194 个、66.3% 只出现一次、
`配图`/`识别`/`无法` 占实体行 31.9%。这里锁住两道门 + 停用词过滤。
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services import kg_stopwords as kw
from services.memory_store import MemoryEvent, MemoryStore


class TestStopwords(unittest.TestCase):
    def test_system_structural_words_filtered(self):
        """我们自己的识图占位符切出的词 —— 必删。"""
        for w in ("配图", "识别", "无法", "图片", "表情", "组件", "引用"):
            self.assertTrue(kw.is_stopword(w), w)

    def test_generic_words_filtered(self):
        for w in ("什么", "怎么", "今日", "今天", "感觉", "群友", "哈哈哈", "好的"):
            self.assertTrue(kw.is_stopword(w), w)

    def test_single_char_and_punct_and_digits_filtered(self):
        for w in ("的", "了", "a", "", "  ", "123456", "，", "！！", "..."):
            self.assertTrue(kw.is_stopword(w), repr(w))

    def test_real_topics_survive(self):
        """群内真实梗必须保留 —— 宁可漏删不可误删。"""
        for w in ("老婆", "戒色", "签到", "晚安", "早睡", "成员丙", "成员己",
                  "成员庚", "成员辛", "报错", "日志", "名字", "Q版", "二次元"):
            self.assertFalse(kw.is_stopword(w), w)

    def test_filter_keywords_preserves_order_and_dedups(self):
        got = kw.filter_keywords(["老婆", "什么", "老婆", "配图", "签到"])
        self.assertEqual(got, ["老婆", "签到"])

    def test_image_description_marker_detected(self):
        self.assertTrue(kw.carries_image_description("（配图：无法识别）"))
        self.assertTrue(kw.carries_image_description("你好 （配图：一只猫）"))
        self.assertFalse(kw.carries_image_description("你好啊"))
        self.assertFalse(kw.carries_image_description(""))


def _stub_keywords(text: str) -> list[str]:
    """确定性取词桩：按空格切词，无空格则整串当一个词，再过停用词。

    不依赖 jieba —— 门槛逻辑必须可在任何环境验证（本机就没有 jieba）。
    测试文本用 `"成员庚 真不错"` 形式给出确定的多词；`"成员庚"` 单串也当一词。
    """
    t = str(text or "").strip()
    if not t:
        return []
    parts = t.split() if " " in t else [t]
    return kw.filter_keywords(p for p in parts if p)


class TestImageDescNotIngested(unittest.TestCase):
    """门槛①：含图片描述的消息不产出 topic 实体。"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.store = MemoryStore(
            self.td.name, topic_min_occurrences=1, keyword_fn=_stub_keywords)

    def tearDown(self):
        self.td.cleanup()

    def _topics(self):
        con = sqlite3.connect(os.path.join(self.td.name, "memory_store.db"))
        rows = con.execute("SELECT DISTINCT alias FROM entities WHERE type='topic'").fetchall()
        con.close()
        return {r[0] for r in rows}

    def test_image_description_produces_no_topics(self):
        self.store.ingest(MemoryEvent(
            speaker_alias="成员丙",
            text="（配图：图片为Q版二次元表情包，角色白发，头戴金色带粉色流苏的头饰）",
            group_id="g",
        ))
        self.assertEqual(self._topics(), set())
        self.assertEqual(self.store.stats()["image_desc_skipped"], 1)

    def test_failed_image_placeholder_produces_no_topics(self):
        self.store.ingest(MemoryEvent(
            speaker_alias="成员丙", text="（配图：无法识别）", group_id="g",
        ))
        # 正是当初产出 配图/识别/无法 三连的那条输入
        self.assertNotIn("配图", self._topics())
        self.assertNotIn("识别", self._topics())
        self.assertNotIn("无法", self._topics())

    def test_member_entities_still_recorded_for_image_messages(self):
        """门槛①只挡 topic —— 说话人与 @ 关系照常记录。"""
        self.store.ingest(MemoryEvent(
            speaker_alias="成员丙", text="（配图：一只猫） @成员卯 你看", group_id="g",
        ))
        con = sqlite3.connect(os.path.join(self.td.name, "memory_store.db"))
        members = {r[0] for r in con.execute(
            "SELECT DISTINCT alias FROM entities WHERE type='member'").fetchall()}
        con.close()
        self.assertIn("成员丙", members)
        self.assertIn("成员卯", members)

    def test_missing_jieba_does_not_crash(self):
        """环境缺 jieba（本机即如此）时取词返回空，但**不得抛异常**。"""
        store = MemoryStore(self.td.name, topic_min_occurrences=2)   # 不注入桩
        store.ingest(MemoryEvent(speaker_alias="甲", text="随便说点什么", group_id="g"))
        self.assertGreaterEqual(store.stats()["entities"], 1)   # speaker 实体仍在

    def test_real_jieba_path_filters_structural_words(self):
        """装了 jieba 的环境走真实路径：`（配图：无法识别）` 不得产出三连词。"""
        try:
            import jieba.analyse  # noqa: F401
        except ImportError:
            self.skipTest("jieba 不可用（本机未安装）")
        store = MemoryStore(self.td.name, topic_min_occurrences=1)
        store.ingest(MemoryEvent(speaker_alias="甲", text="（配图：无法识别）", group_id="g"))
        con = sqlite3.connect(os.path.join(self.td.name, "memory_store.db"))
        got = {r[0] for r in con.execute(
            "SELECT DISTINCT alias FROM entities WHERE type='topic'").fetchall()}
        con.close()
        self.assertEqual(got & {"配图", "识别", "无法"}, set())


class TestTopicOccurrenceThreshold(unittest.TestCase):
    """门槛②：只出现一次的 topic 不建 talks_about 边。"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.td.cleanup()

    def _store(self, min_occ):
        return MemoryStore(
            self.td.name, topic_min_occurrences=min_occ, keyword_fn=_stub_keywords)

    def _edges(self):
        con = sqlite3.connect(os.path.join(self.td.name, "memory_store.db"))
        rows = con.execute(
            "SELECT to_alias, COUNT(*) FROM edges WHERE type='talks_about' GROUP BY to_alias"
        ).fetchall()
        con.close()
        return dict(rows)

    def test_first_occurrence_low_value_topic_gets_no_edge(self):
        s = self._store(2)
        s.ingest(MemoryEvent(speaker_alias="甲", text="成员庚 真不错", group_id="g"))
        self.assertEqual(self._edges(), {})
        self.assertGreaterEqual(s.stats()["topics_deferred"], 0)

    def test_second_occurrence_creates_edge(self):
        s = self._store(2)
        for _ in range(2):
            s.ingest(MemoryEvent(speaker_alias="甲", text="成员庚 真不错", group_id="g"))
        edges = self._edges()
        self.assertIn("成员庚", edges)
        self.assertEqual(edges["成员庚"], 1)   # 只在第二次建了一条

    def test_third_occurrence_keeps_adding(self):
        s = self._store(2)
        for _ in range(3):
            s.ingest(MemoryEvent(speaker_alias="甲", text="成员庚 真不错", group_id="g"))
        self.assertEqual(self._edges()["成员庚"], 2)   # 第2、3次各一条

    def test_threshold_one_disables_gate(self):
        """min_occurrences=1 → 退回旧行为（每条都建边），用作回滚开关。"""
        s = self._store(1)
        s.ingest(MemoryEvent(speaker_alias="甲", text="成员庚 真不错", group_id="g"))
        self.assertIn("成员庚", self._edges())

    def test_threshold_zero_clamped_to_one(self):
        s = self._store(0)
        s.ingest(MemoryEvent(speaker_alias="甲", text="成员庚 真不错", group_id="g"))
        self.assertIn("成员庚", self._edges())

    def test_stats_report_gates(self):
        s = self._store(2)
        s.ingest(MemoryEvent(speaker_alias="甲", text="（配图：无法识别）", group_id="g"))
        s.ingest(MemoryEvent(speaker_alias="甲", text="成员庚 真不错", group_id="g"))
        st = s.stats()
        self.assertEqual(st["image_desc_skipped"], 1)
        self.assertEqual(st["topic_min_occurrences"], 2)
        self.assertGreaterEqual(st["topics_deferred"], 1)
        self.assertIn("distinct_topics", st)

    def test_mentions_edges_unaffected_by_topic_gate(self):
        """@ 关系不受门槛②影响。"""
        s = self._store(2)
        s.ingest(MemoryEvent(speaker_alias="甲", text="@乙 在吗", group_id="g"))
        con = sqlite3.connect(os.path.join(self.td.name, "memory_store.db"))
        n = con.execute("SELECT COUNT(*) FROM edges WHERE type='mentions'").fetchone()[0]
        con.close()
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()


class TestCleanKgTool(unittest.TestCase):
    """历史清洗工具（tools/clean_kg.py）—— 必须只删垃圾、不误伤真实信号。"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.td.name, "memory_store.db")
        MemoryStore(self.td.name, topic_min_occurrences=1)   # 建 schema
        con = sqlite3.connect(self.db)
        for alias, n in (("配图", 5), ("识别", 4), ("无法", 3), ("什么", 3), ("只出现一次", 1)):
            for i in range(n):
                con.execute("INSERT INTO entities(alias,type,text,ts) VALUES (?,?,?,?)",
                            (alias, "topic", alias, 1.0))
                con.execute("INSERT INTO edges(from_alias,to_alias,type,ts,properties_json) "
                            "VALUES (?,?,?,?,?)", ("甲", alias, "talks_about", 1.0, "{}"))
        for i in range(5):   # 真实话题
            con.execute("INSERT INTO entities(alias,type,text,ts) VALUES (?,?,?,?)",
                        ("老婆", "topic", "老婆", 2.0))
            con.execute("INSERT INTO edges(from_alias,to_alias,type,ts,properties_json) "
                        "VALUES (?,?,?,?,?)", ("甲", "老婆", "talks_about", 2.0, "{}"))
        con.execute("INSERT INTO edges(from_alias,to_alias,type,ts,properties_json) "
                    "VALUES (?,?,?,?,?)", ("甲", "乙", "mentions", 3.0, "{}"))
        con.execute("INSERT INTO entities(alias,type,text,ts) VALUES (?,?,?,?)",
                    ("乙", "member", "乙", 3.0))
        con.commit()
        con.close()

    def tearDown(self):
        self.td.cleanup()

    def _run(self, *extra):
        import subprocess
        return subprocess.run(
            [sys.executable, "-m", "tools.clean_kg", "--db", self.db, *extra],
            capture_output=True, text=True,
            cwd=os.path.join(os.path.dirname(__file__), ".."),
        )

    def _edges(self):
        con = sqlite3.connect(self.db)
        rows = dict(con.execute(
            "SELECT to_alias, COUNT(*) FROM edges WHERE type='talks_about' GROUP BY to_alias"
        ).fetchall())
        mentions = con.execute("SELECT COUNT(*) FROM edges WHERE type='mentions'").fetchone()[0]
        con.close()
        return rows, mentions

    def test_dry_run_changes_nothing(self):
        before, _ = self._edges()
        r = self._run()
        self.assertEqual(r.returncode, 0)
        self.assertIn("dry-run", r.stdout)
        after, _ = self._edges()
        self.assertEqual(before, after)

    def test_apply_removes_only_junk(self):
        r = self._run("--apply")
        self.assertEqual(r.returncode, 0)
        edges, mentions = self._edges()
        self.assertEqual(edges, {"老婆": 5})          # 真实话题保留
        self.assertEqual(mentions, 1)                 # @ 关系不受影响

    def test_apply_creates_backup(self):
        self._run("--apply")
        import glob
        self.assertTrue(glob.glob(self.db + ".bak.*"), "必须自动备份")

    def test_threshold_is_configurable(self):
        """阈值生效：抬高到 10 → 连"老婆"(5 次) 也落入一次性候选。"""
        import json as _json
        r = self._run("--keep-threshold", "10")
        self.assertEqual(r.returncode, 0)
        self.assertIn("出现 < 10 次", r.stdout)
        # ③ 只打计数（有意不刷屏）→ 用 json 模式取精确值
        d10 = _json.loads(self._run("--keep-threshold", "10", "--json").stdout)
        # 库里：老婆5 / 配图5 / 识别4 / 无法3 / 什么3 / 只出现一次1 = 6 个
        self.assertEqual(d10["rare_topics_count"], 6)
        # 阈值 2 时只应有 1 个（"只出现一次"）
        d2 = _json.loads(self._run("--json").stdout)
        self.assertEqual(d2["rare_topics_count"], 1)

    def test_missing_db_returns_error(self):
        import subprocess
        r = subprocess.run(
            [sys.executable, "-m", "tools.clean_kg", "--db", "/nonexistent/x.db"],
            capture_output=True, text=True,
            cwd=os.path.join(os.path.dirname(__file__), ".."),
        )
        self.assertEqual(r.returncode, 2)
