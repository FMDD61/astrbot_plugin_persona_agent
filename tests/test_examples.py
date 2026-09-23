# -*- coding: utf-8 -*-
"""示例块加载（2026-09-23 脱敏后的口径）。

🔴 示例语料源自**真实群聊语句** → 整批移出仓库（仓库是 public）。
代码里**没有内置语料**（`examples_default.ENTRIES` 恒空），所以：

* 数据目录有可用文件 → `source="file"`（部署形态；scp 迁移过来）；
* 没有 / 坏了 / 空 → **空示例块** `source="none"`（**可达且必须可见**，不是"回落到默认句"）。

用户 2026-09-20 的口径不变：**任何情况下不回退到旧示例句**。
"""
import json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services import examples_default
from services.examples import (
    CURRENT_EXAMPLES_FILE, EXAMPLES_FILES, LEGACY_EXAMPLES_FILE, ExamplesState,
    MAX_ENTRIES, candidate_paths, cleanup_legacy_file, load_examples_block,
    parse_payload,
)

#: 夹具语料（**纯造**，不含任何真实群聊语句）
EX = [
    {"topic": "A", "messages": [{"role": "群友", "content": "早上好"}, {"role": "角色", "content": "早上好~"}]},
    {"topic": "B", "messages": [{"role": "群友", "content": "x"}, {"role": "角色", "content": "y"}]},
]


class TestExamplesLoader(unittest.TestCase):
    def _write(self, td, data, ns_shift=1_000_000_000):
        p = os.path.join(td, "example_dialogs.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        st = os.stat(p)
        os.utime(p, ns=(st.st_atime_ns + ns_shift, st.st_mtime_ns + ns_shift))
        return p

    def test_load_from_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, EX)
            block, st = load_examples_block(os.path.join(td, "example_dialogs.json"))
            self.assertIn("早上好~", block)
            self.assertIn("[A]", block)
            self.assertEqual(st.source, "file")
            self.assertEqual(st.entries, 2)
            self.assertEqual(st.path, p, "state 必须点名实际生效的文件")
            self.assertEqual(st.error, "")
            # 旧的两条"规则A/规则B"已删（规则B 已证伪、规则A 是错误示例）
            self.assertNotIn("规则A", block)
            self.assertNotIn("规则B", block)

    def test_no_mtime_cache_rewrite_is_seen(self):
        """N-1：不做 mtime 短路 —— 改写后立刻可见（同刻度也不漏）。"""
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, EX)
            block, st = load_examples_block(p)
            self.assertIn("[B]", block)
            self._write(td, EX[:1])
            block2, _st2 = load_examples_block(p, prev=st)
            self.assertNotIn("[B]", block2)

    def test_missing_file_yields_empty_block_and_none_source(self):
        """🔴 框架态：代码里没有语料 → 缺文件就是**空块**，且必须显式标 none。"""
        with tempfile.TemporaryDirectory() as td:
            block, st = load_examples_block(os.path.join(td, "nope.json"))
            self.assertEqual(block, "")
            self.assertEqual(st.source, "none")
            self.assertEqual(st.entries, 0)
            self.assertEqual(st.path, "")
            # 删掉已生效的文件后同样变空（不是"变回默认句"）
            p = self._write(td, EX)
            b1, s1 = load_examples_block(p)
            self.assertEqual(s1.source, "file")
            os.unlink(p)
            b2, s2 = load_examples_block(p, prev=s1)
            self.assertEqual(s2.source, "none")
            self.assertEqual(b2, "")

    def test_corrupt_file_is_none_with_reason(self):
        """坏文件与"没文件"都落到 none，但 **error 能把两者区分开**（N6 同族纪律）。"""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "example_dialogs.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write("{broken")
            block, st = load_examples_block(p)
            self.assertEqual(st.source, "none")
            self.assertEqual(block, "")
            self.assertIn("example_dialogs.json", st.error, "坏文件必须留下原因")
            # 对照：压根没有文件时 error 为空
            with tempfile.TemporaryDirectory() as td2:
                _b, st2 = load_examples_block(os.path.join(td2, "nope.json"))
                self.assertEqual(st2.error, "")

    def test_examples_json_alias_is_accepted(self):
        """`data_out/` 的交付名 `examples.json` 与历史名等价。"""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "examples.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(EX, f, ensure_ascii=False)
            block, st = load_examples_block(os.path.join(td, "example_dialogs.json"))
            self.assertEqual(st.source, "file")
            self.assertEqual(st.path, p)
            self.assertIn("[A]", block)
            self.assertIn("examples.json", EXAMPLES_FILES)

    def test_header_entries_payload_uses_file_header(self):
        """`data_out/examples.json` 自带 HEADER → 用它，而不是框架头。"""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "examples.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"header": "自定义头部：", "entries": EX}, f, ensure_ascii=False)
            block, st = load_examples_block(p)
            self.assertEqual(st.source, "file")
            self.assertTrue(block.startswith("自定义头部："))
            self.assertNotIn(examples_default.HEADER, block)
            # 裸数组仍走框架头
            self.assertEqual(parse_payload(EX), ("", EX))
            self.assertEqual(parse_payload({"entries": EX})[1], EX)

    def test_candidate_paths_covers_both_names(self):
        got = [p.name for p in candidate_paths("/x/example_dialogs.json")]
        self.assertEqual(got, ["example_dialogs.json", "examples.json"])
        got = [p.name for p in candidate_paths("/x/examples.json")]
        self.assertEqual(got, ["examples.json", "example_dialogs.json"])

    def test_single_message_entries_are_unusable(self):
        """只有单条消息的条目不算示例 → 该文件没有可用条目 → none（不是半截块）。"""
        with tempfile.TemporaryDirectory() as td:
            bad = [{"topic": "C", "messages": [{"role": "群友", "content": "only-one"}]}]
            p = self._write(td, bad)
            block, st = load_examples_block(p)
            self.assertEqual(st.source, "none")
            self.assertEqual(block, "")
            self.assertIn("没有可用条目", st.error)

    def test_cap_and_ordering(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, EX * 10)  # 20 entries
            block, st = load_examples_block(p, max_entries=12)
            self.assertLessEqual(block.count("[A]") + block.count("[B]"), 12)
            self.assertEqual(st.entries, 12)


class TestBundledIsEmptyByDesign(unittest.TestCase):
    """🔴 脱敏的核心不变量：**代码里没有语料**。"""

    def test_bundled_entries_are_empty(self):
        self.assertEqual(examples_default.ENTRIES, [],
                         "仓库不得内置任何示例语料（真实语料在仓库外 data_out/）")
        self.assertEqual(json.dumps(examples_default.ENTRIES, ensure_ascii=False), "[]")

    def test_header_is_framework_text(self):
        self.assertEqual(examples_default.HEADER, "示例对话（就是这种语感，照着说）：")

    def test_no_old_example_sentences_anywhere(self):
        """用户要求：**不论如何，不会回退到旧的示例句**（旧句现在连代码都不在）。"""
        # 旧句现在连"数据"都不在：ENTRIES 恒空、HEADER 是纯框架文本。
        # （源码注释里会出现"旧句叫什么名字"的说明 —— 那是文档，不是语料。）
        blob = json.dumps(examples_default.ENTRIES, ensure_ascii=False) + examples_default.HEADER
        # ⚠️ 2026-09-23 口癖解耦：旧句里那个**口癖词**已不在代码/夹具里（真实口癖
        # 属数据，移到仓库外 data_out/koupi.json）；这里只剩示例句名可查。
        for old in ("规则A", "规则B"):
            self.assertNotIn(old, blob, f"旧示例残留：{old}")

    def test_max_entries_default_is_20(self):
        self.assertEqual(MAX_ENTRIES, 20)
        block, st = load_examples_block("/nonexistent/path.json")
        self.assertEqual(st.entries, 0)
        self.assertEqual(block, "")

    def test_state_defaults_are_none(self):
        st = ExamplesState()
        self.assertEqual((st.source, st.entries, st.path, st.error), ("none", 0, "", ""))


class TestLegacyExamplesCleanup(unittest.TestCase):
    """轮转时的示例文件换代（用户 2026-09-23：「仅保留新 examples.json，旧文件进行清理」）。

    判定在 `services.examples.cleanup_legacy_file`（主链路 main._rotate_followup 调它），
    这里钉住**删不删**这条规则本身。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _put(self, name, entries):
        p = os.path.join(self.dir, name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False)
        return p

    def test_candidate_order_is_legacy_first(self):
        """旧名**优先** —— 这正是必须清理它的原因。"""
        self.assertEqual(EXAMPLES_FILES, (LEGACY_EXAMPLES_FILE, CURRENT_EXAMPLES_FILE))

    def test_absent_when_no_legacy_file(self):
        self.assertEqual(cleanup_legacy_file(self.dir), ("absent", ""))

    def test_no_replacement_keeps_legacy(self):
        """只有旧文件时**绝不删** —— 删了示例块会凭空消失。"""
        self._put(LEGACY_EXAMPLES_FILE, EX)
        action, detail = cleanup_legacy_file(self.dir)
        self.assertEqual(action, "no_replacement")
        self.assertTrue(detail.endswith(CURRENT_EXAMPLES_FILE))
        self.assertTrue(os.path.exists(os.path.join(self.dir, LEGACY_EXAMPLES_FILE)))
        # 传**主路径 = 旧名**（main._examples_block 就是这么传的）→ 候选序里旧名在前
        _block, st = load_examples_block(os.path.join(self.dir, LEGACY_EXAMPLES_FILE))
        self.assertEqual(st.entries, len(EX), "旧语料必须照旧生效（降级不是消失）")

    def test_removed_with_byte_identical_backup(self):
        legacy = self._put(LEGACY_EXAMPLES_FILE, EX)
        self._put(CURRENT_EXAMPLES_FILE, EX + EX)
        with open(legacy, "rb") as f:
            before = f.read()
        action, dest = cleanup_legacy_file(self.dir, stamp="TESTSTAMP")
        self.assertEqual(action, "removed")
        self.assertFalse(os.path.exists(legacy))
        self.assertTrue(dest.endswith(os.path.join("data_out", "legacy",
                                                   LEGACY_EXAMPLES_FILE + ".TESTSTAMP")))
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), before, "备份必须逐字节相同")

    def test_new_file_takes_effect_after_cleanup(self):
        """验收口径：清理前读旧文件、清理后读新文件（**不需要重启**）。"""
        self._put(LEGACY_EXAMPLES_FILE, EX)                 # 2 条
        self._put(CURRENT_EXAMPLES_FILE, EX + EX)           # 4 条
        path = os.path.join(self.dir, LEGACY_EXAMPLES_FILE)   # main 传入的主路径
        _b, st = load_examples_block(path)
        self.assertEqual(st.entries, 2, "旧文件优先：清理前读到的是旧的")
        cleanup_legacy_file(self.dir, stamp="T")
        _b, st = load_examples_block(path)                  # 现读，无需重启
        self.assertEqual(st.entries, 4, "清理后必须立刻读到新文件")

    def test_idempotent(self):
        self._put(LEGACY_EXAMPLES_FILE, EX)
        self._put(CURRENT_EXAMPLES_FILE, EX)
        self.assertEqual(cleanup_legacy_file(self.dir, stamp="T1")[0], "removed")
        self.assertEqual(cleanup_legacy_file(self.dir, stamp="T2")[0], "absent")

    def test_failure_is_reported_not_raised(self):
        """备份目录建不出来 → 报 failed 且**保持不动**（绝不半途而废只删不备份）。"""
        self._put(LEGACY_EXAMPLES_FILE, EX)
        self._put(CURRENT_EXAMPLES_FILE, EX)
        with open(os.path.join(self.dir, "data_out"), "w") as f:
            f.write("占位：让 backup_dir.mkdir 失败")
        action, detail = cleanup_legacy_file(self.dir, stamp="T")
        self.assertEqual(action, "failed")
        self.assertTrue(detail)
        self.assertTrue(os.path.exists(os.path.join(self.dir, LEGACY_EXAMPLES_FILE)),
                        "出错时旧文件必须原样留着")

    def test_no_backup_dir_written_when_absent(self):
        self._put(CURRENT_EXAMPLES_FILE, EX)
        self.assertEqual(cleanup_legacy_file(self.dir, stamp="T")[0], "absent")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "data_out", "legacy")))


if __name__ == "__main__":
    unittest.main()
