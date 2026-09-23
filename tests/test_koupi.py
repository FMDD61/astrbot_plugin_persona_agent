# -*- coding: utf-8 -*-
"""koupi 外置加载器单测 —— 2026-09-23「口癖常量解耦」。

用户口径（逐字）：
> 「口癖常量解耦，将具体口癖外挂到 data_out/ 下，加载时插件从外部引入数据。
>  若外部无文件，降级到无口癖功能。」

⚠️ **本文件不许出现任何真实口癖**（仓库是 public）：全部用**假口癖**
（`测试口癖A`/`测试口癖B`/`测试口癖C`）验证**机制** —— 真名单在仓库外
`data_out/koupi.json`，部署时 scp 到 `<data_dir>/koupi.json`。

被钉住的四条契约：
  1. 外部文件在 → 按**外部名单**裁剪（不是任何内置名单）；
  2. 外部缺失  → `cap_koupi` 直通 + 留痕 `koupi_source=missing`；
  3. mtime 变  → 热重载生效（改完就生效，不重启）；
  4. 坏 JSON   → 直通 + 留痕 `koupi_source=broken`，**不许抛**。
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services import text_style as T

FAKE_A = "测试口癖A"
FAKE_B = "测试口癖B"
FAKE_C = "测试口癖C"


def write_json(path, payload, mtime=None):
    with open(path, "w", encoding="utf-8") as f:
        if isinstance(payload, str):
            f.write(payload)
        else:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


class KoupiCase(unittest.TestCase):
    """公共夹具：临时 data_dir + 每个用例后**复位全局配置**（不许跨用例泄漏）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.koupi_path = os.path.join(self.dir, T.KOUPI_FILE)
        T.reset_koupi()

    def tearDown(self):
        T.reset_koupi()
        self.tmp.cleanup()


class TestExternalFilePresent(KoupiCase):
    def test_list_comes_from_data_dir(self):
        write_json(self.koupi_path, {"phrases": [FAKE_A, FAKE_B, FAKE_C]})
        st = T.configure_koupi(self.dir)
        self.assertEqual(st.source, "file")
        self.assertEqual(st.phrases, (FAKE_A, FAKE_B, FAKE_C))
        self.assertTrue(st.path.endswith(T.KOUPI_FILE))
        self.assertFalse(st.degraded)

    def test_bare_array_also_accepted(self):
        write_json(self.koupi_path, [FAKE_A, FAKE_B])
        self.assertEqual(T.configure_koupi(self.dir).phrases, (FAKE_A, FAKE_B))

    def test_cap_uses_external_list(self):
        write_json(self.koupi_path, {"phrases": [FAKE_A]})
        T.configure_koupi(self.dir)
        out = T.cap_koupi(FAKE_A * 5)
        self.assertEqual(out.count(FAKE_A), T.KOUPI_MAX_TOTAL)

    def test_cap_keeps_the_earliest_occurrences(self):
        write_json(self.koupi_path, {"phrases": [FAKE_A]})
        T.configure_koupi(self.dir)
        out = T.cap_koupi(f"{FAKE_A}1{FAKE_A}2{FAKE_A}3")
        self.assertIn("1", out)
        self.assertIn("3", out)
        # 保留靠前的 2 次；第 3 次连同它的字面被删掉，两侧的 "2"/"3" 因此并排
        self.assertEqual(out, f"{FAKE_A}1{FAKE_A}23")

    def test_postprocess_caps_with_external_list(self):
        write_json(self.koupi_path, {"phrases": [FAKE_A]})
        T.configure_koupi(self.dir)
        out = T.postprocess(FAKE_A * 4)
        self.assertEqual(out.count(FAKE_A), T.KOUPI_MAX_TOTAL)

    def test_word_not_in_list_is_untouched(self):
        write_json(self.koupi_path, {"phrases": [FAKE_A]})
        T.configure_koupi(self.dir)
        self.assertEqual(T.cap_koupi(FAKE_B * 5), FAKE_B * 5)

    def test_manifest_reports_file_source(self):
        write_json(self.koupi_path, {"phrases": [FAKE_A, FAKE_B]})
        T.configure_koupi(self.dir)
        m = T.koupi_manifest()
        self.assertEqual(m["koupi_source"], "file")
        self.assertEqual(m["koupi_phrases"], 2)


class TestMissingFileDegradesVisibly(KoupiCase):
    """外部没有文件 → 降级为「无口癖功能」（直通），且**必须留下痕迹**。"""

    def test_source_is_missing(self):
        st = T.configure_koupi(self.dir)
        self.assertEqual(st.source, "missing")
        self.assertTrue(st.degraded)
        self.assertEqual(st.phrases, ())
        self.assertEqual(st.path, "")

    def test_cap_koupi_is_passthrough(self):
        T.configure_koupi(self.dir)
        text = FAKE_A * 6
        self.assertEqual(T.cap_koupi(text), text)

    def test_postprocess_does_not_raise(self):
        T.configure_koupi(self.dir)
        self.assertEqual(T.postprocess(FAKE_A * 6), FAKE_A * 6)

    def test_manifest_records_the_degradation(self):
        T.configure_koupi(self.dir)
        self.assertEqual(T.koupi_manifest()["koupi_source"], "missing")

    def test_unconfigured_is_also_missing_not_silent(self):
        """宿主忘了 configure → 同样是「没有外部数据」，不许当成"没有降级"。"""
        T.reset_koupi()
        self.assertEqual(T.koupi_state().source, "missing")
        self.assertTrue(T.koupi_state().degraded)

    def test_recovers_when_file_appears(self):
        T.configure_koupi(self.dir)
        self.assertEqual(T.koupi_state().source, "missing")
        write_json(self.koupi_path, {"phrases": [FAKE_A]})
        self.assertEqual(T.koupi_state().source, "file")
        self.assertEqual(T.cap_koupi(FAKE_A * 3).count(FAKE_A), 2)

    def test_file_vanishing_drops_the_list(self):
        """文件没了 → 立刻降级，**不保留内存里的旧名单**（陈旧比空更糟）。"""
        write_json(self.koupi_path, {"phrases": [FAKE_A]})
        T.configure_koupi(self.dir)
        self.assertEqual(T.koupi_state().source, "file")
        os.unlink(self.koupi_path)
        self.assertEqual(T.koupi_state().source, "missing")
        self.assertEqual(T.cap_koupi(FAKE_A * 3), FAKE_A * 3)


class TestHotReload(KoupiCase):
    def test_mtime_change_reloads(self):
        write_json(self.koupi_path, {"phrases": [FAKE_A]})
        T.configure_koupi(self.dir)
        self.assertEqual(T.cap_koupi(FAKE_A * 3).count(FAKE_A), 2)
        # 换成 B 并把 mtime 显式推后（粗时钟下同刻度改写看不见，用例要确定性地跨刻度）
        write_json(self.koupi_path, {"phrases": [FAKE_B]}, mtime=time.time() + 30)
        st = T.koupi_state()
        self.assertEqual(st.phrases, (FAKE_B,))
        self.assertEqual(T.cap_koupi(FAKE_B * 3).count(FAKE_B), 2)
        self.assertEqual(T.cap_koupi(FAKE_A * 3), FAKE_A * 3, "旧名单必须已失效")

    def test_same_fingerprint_keeps_cache(self):
        write_json(self.koupi_path, {"phrases": [FAKE_A]})
        T.configure_koupi(self.dir)
        first = T.koupi_state()
        second = T.koupi_state()
        self.assertIs(first, second, "指纹没变就不该重读文件")


class TestBrokenFileDegradesVisibly(KoupiCase):
    """坏文件 = 同样降级，但**原因不同**（missing vs broken 必须可区分）。"""

    def test_bad_json_passthrough_and_no_raise(self):
        write_json(self.koupi_path, "{这不是 JSON")
        st = T.configure_koupi(self.dir)
        self.assertEqual(st.source, "broken")
        self.assertTrue(st.error)
        self.assertEqual(T.cap_koupi(FAKE_A * 4), FAKE_A * 4)
        self.assertEqual(T.postprocess(FAKE_A * 4), FAKE_A * 4)

    def test_wrong_structure(self):
        write_json(self.koupi_path, {"phrases": "不是数组"})
        st = T.configure_koupi(self.dir)
        self.assertEqual(st.source, "broken")
        self.assertIn("结构", st.error)

    def test_empty_list_is_broken_not_silently_ok(self):
        write_json(self.koupi_path, {"phrases": []})
        st = T.configure_koupi(self.dir)
        self.assertEqual(st.source, "broken")
        self.assertTrue(st.degraded)

    def test_illegal_entries_are_skipped_but_list_survives(self):
        write_json(self.koupi_path, {"phrases": [FAKE_A, "", None, 7, FAKE_B]})
        st = T.configure_koupi(self.dir)
        self.assertEqual(st.source, "file")
        self.assertEqual(st.phrases, (FAKE_A, FAKE_B))
        self.assertIn("非法", st.error, "跳过的条目要记账，不能一声不吭")

    def test_recovers_after_fix(self):
        write_json(self.koupi_path, "{坏的")
        T.configure_koupi(self.dir)
        self.assertEqual(T.koupi_state().source, "broken")
        write_json(self.koupi_path, {"phrases": [FAKE_A]}, mtime=time.time() + 30)
        self.assertEqual(T.koupi_state().source, "file")


class TestNoHardcodedListInModule(unittest.TestCase):
    """解耦的**结构性**断言：模块里不许再有硬编码名单。"""

    def test_no_koupi_list_constant(self):
        self.assertFalse(hasattr(T, "KOUPI_LIST"),
                         "KOUPI_LIST 回来了 —— 真实口癖又进了 public 仓库")

    def test_cap_koupi_reads_the_loader_by_default(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(T.cap_koupi))
        called = {n.func.id for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertIn("koupi_phrases", called,
                      "cap_koupi 必须走外部加载器，不许自带名单")

    def test_file_name_is_data_dir_relative_not_hardcoded_data_out(self):
        self.assertEqual(T.KOUPI_FILE, "koupi.json")
        self.assertNotIn("data_out", T.KOUPI_FILE)


if __name__ == "__main__":
    unittest.main()
