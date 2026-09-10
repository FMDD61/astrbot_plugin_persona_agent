# -*- coding: utf-8 -*-
"""tools/llbot_config 测试（NapCat → LLBot v8 迁移，2026-09-10）。

重点覆盖：
  - BOM 兼容读取（AstrBot cmd_config.json 实测带 BOM）
  - url/host 归一化（0.0.0.0 → 127.0.0.1，IPv6）
  - messageFormat 恒为 array（AstrBot 硬要求）
  - token 不回显（掩码函数）
  - check 模式的各类不一致检出
  - merge 幂等且保留其它 connect 条目
  - --out 不覆盖已存在文件
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

spec = importlib.util.spec_from_file_location(
    "llbot_config", str(REPO_ROOT / "tools" / "llbot_config.py")
)
llbot_config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(llbot_config)


def _astrbot_cfg(host="127.0.0.1", port=6199, token="tok-123456", enable=True):
    return {
        "platform": [
            {"type": "qq_official", "enable": True},
            {
                "type": "aiocqhttp",
                "id": "aiocqhttp",
                "name": "NapCat (OneBot V11)",
                "enable": enable,
                "ws_reverse_host": host,
                "ws_reverse_port": port,
                "ws_reverse_token": token,
            },
        ]
    }


class _Args:
    def __init__(self, host="", port=0):
        self.host = host
        self.port = port


class DerivationTests(unittest.TestCase):
    def test_find_platform_skips_other_types(self):
        p = llbot_config.find_aiocqhttp_platform(_astrbot_cfg())
        self.assertIsNotNone(p)
        self.assertEqual(p["type"], "aiocqhttp")

    def test_missing_platform_returns_none(self):
        self.assertIsNone(llbot_config.find_aiocqhttp_platform({"platform": []}))
        self.assertIsNone(llbot_config.find_aiocqhttp_platform({}))
        self.assertIsNone(llbot_config.find_aiocqhttp_platform(None))

    def test_entry_url_matches_aiohttp_default(self):
        entry, problems = llbot_config.derive_entry(_astrbot_cfg(), _Args())
        self.assertEqual(problems, [])
        self.assertEqual(entry["url"], "ws://127.0.0.1:6199/ws")
        self.assertEqual(entry["type"], "ws-reverse")
        self.assertTrue(entry["enable"])
        self.assertEqual(entry["token"], "tok-123456")

    def test_message_format_is_always_array(self):
        """AstrBot 对非 list 的 message 会 critical + 往群里发错误文本 + raise。"""
        entry, _ = llbot_config.derive_entry(_astrbot_cfg(), _Args())
        self.assertEqual(entry["messageFormat"], "array")

    def test_wildcard_host_maps_to_loopback(self):
        for host in ("0.0.0.0", "", "*"):
            self.assertEqual(llbot_config.normalize_host(host), "127.0.0.1")
        self.assertEqual(llbot_config.normalize_host("192.0.2.10"), "192.0.2.10")

    def test_ipv6_host_mapping(self):
        self.assertEqual(llbot_config.normalize_host("::"), "[::1]")
        self.assertEqual(llbot_config.normalize_host("::1"), "[::1]")

    def test_cli_host_port_override(self):
        entry, _ = llbot_config.derive_entry(
            _astrbot_cfg(), _Args(host="10.0.0.5", port=7000))
        self.assertEqual(entry["url"], "ws://10.0.0.5:7000/ws")

    def test_disabled_platform_is_reported(self):
        _, problems = llbot_config.derive_entry(_astrbot_cfg(enable=False), _Args())
        self.assertTrue(any("enable=false" in p for p in problems))

    def test_empty_token_is_reported_not_fatal(self):
        entry, problems = llbot_config.derive_entry(_astrbot_cfg(token=""), _Args())
        self.assertEqual(entry["token"], "")
        self.assertTrue(any("token 为空" in p for p in problems))

    def test_bad_port_falls_back(self):
        cfg = _astrbot_cfg()
        cfg["platform"][1]["ws_reverse_port"] = "not-a-port"
        entry, problems = llbot_config.derive_entry(cfg, _Args())
        self.assertEqual(entry["url"], f"ws://127.0.0.1:{llbot_config.DEFAULT_PORT}/ws")
        self.assertTrue(any("非法" in p for p in problems))


class MaskTests(unittest.TestCase):
    def test_mask_never_reveals_value(self):
        self.assertEqual(llbot_config.mask_secret("tok-123456"), "<set:10 chars>")
        self.assertNotIn("tok-123456", llbot_config.mask_secret("tok-123456"))

    def test_mask_empty(self):
        self.assertEqual(llbot_config.mask_secret(""), "<empty>")
        self.assertEqual(llbot_config.mask_secret(None), "<empty>")

    def test_describe_hides_token(self):
        entry, _ = llbot_config.derive_entry(_astrbot_cfg(), _Args())
        text = llbot_config.describe(entry)
        self.assertNotIn("tok-123456", text)
        self.assertIn("messageFormat : array", text)


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.entry, _ = llbot_config.derive_entry(_astrbot_cfg(), _Args())

    def _llbot(self, **over):
        connect = dict(self.entry)
        connect.update(over)
        return {"ob11": {"enable": True, "connect": [connect]}}

    def test_in_sync(self):
        self.assertEqual(llbot_config.check_llbot_config(self._llbot(), self.entry), [])

    def test_url_mismatch(self):
        issues = llbot_config.check_llbot_config(
            self._llbot(url="ws://127.0.0.1:9999/ws"), self.entry)
        self.assertTrue(any("url 不一致" in i for i in issues))

    def test_token_mismatch_message_is_masked(self):
        issues = llbot_config.check_llbot_config(
            self._llbot(token="other-secret-value"), self.entry)
        joined = " ".join(issues)
        self.assertIn("token 不一致", joined)
        self.assertNotIn("other-secret-value", joined)

    def test_string_message_format_is_flagged(self):
        issues = llbot_config.check_llbot_config(
            self._llbot(messageFormat="string"), self.entry)
        self.assertTrue(any("必须为 'array'" in i for i in issues))

    def test_disabled_ob11_is_flagged(self):
        cfg = self._llbot()
        cfg["ob11"]["enable"] = False
        issues = llbot_config.check_llbot_config(cfg, self.entry)
        self.assertTrue(any("ob11.enable=false" in i for i in issues))

    def test_missing_ws_reverse_entry(self):
        cfg = {"ob11": {"enable": True, "connect": [{"type": "ws", "port": 3001}]}}
        issues = llbot_config.check_llbot_config(cfg, self.entry)
        self.assertTrue(any("没有 type=ws-reverse" in i for i in issues))

    def test_missing_ob11_section(self):
        issues = llbot_config.check_llbot_config({}, self.entry)
        self.assertTrue(any("没有 ob11 段" in i for i in issues))

    def test_disabled_entry_is_flagged(self):
        issues = llbot_config.check_llbot_config(
            self._llbot(enable=False), self.entry)
        self.assertTrue(any("enable=false" in i for i in issues))


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.entry, _ = llbot_config.derive_entry(_astrbot_cfg(), _Args())

    def test_append_preserves_other_entries(self):
        cfg = {"ob11": {"enable": True, "connect": [
            {"type": "ws", "enable": False, "port": 3001},
            {"type": "http", "enable": False, "port": 3000},
        ]}}
        merged, changed = llbot_config.merge_entry(cfg, self.entry)
        self.assertTrue(changed)
        self.assertEqual(len(merged["ob11"]["connect"]), 3)
        self.assertEqual(merged["ob11"]["connect"][0]["type"], "ws")
        self.assertEqual(merged["ob11"]["connect"][2]["type"], "ws-reverse")

    def test_merge_is_idempotent(self):
        cfg = {"ob11": {"enable": True, "connect": []}}
        cfg, changed1 = llbot_config.merge_entry(cfg, self.entry)
        cfg, changed2 = llbot_config.merge_entry(cfg, self.entry)
        self.assertTrue(changed1)
        self.assertFalse(changed2)
        self.assertEqual(len(cfg["ob11"]["connect"]), 1)

    def test_merge_updates_existing_ws_reverse_in_place(self):
        cfg = {"ob11": {"enable": True, "connect": [
            dict(self.entry, url="ws://127.0.0.1:1/ws"),
        ]}}
        merged, changed = llbot_config.merge_entry(cfg, self.entry)
        self.assertTrue(changed)
        self.assertEqual(len(merged["ob11"]["connect"]), 1)
        self.assertEqual(merged["ob11"]["connect"][0]["url"], "ws://127.0.0.1:6199/ws")

    def test_merge_creates_ob11_section(self):
        cfg = {}
        merged, changed = llbot_config.merge_entry(cfg, self.entry)
        self.assertTrue(changed)
        self.assertTrue(merged["ob11"]["enable"])


class FileIOTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_json_accepts_bom(self):
        """AstrBot cmd_config.json 实测带 BOM：json.load 会直接报错。"""
        p = self.dir / "cmd_config.json"
        p.write_text(json.dumps(_astrbot_cfg(), ensure_ascii=False), encoding="utf-8-sig")
        with self.assertRaises(Exception):
            json.loads(p.read_text("utf-8"))  # 证明 BOM 确实会炸
        cfg = llbot_config.load_json(p)
        self.assertIsNotNone(llbot_config.find_aiocqhttp_platform(cfg))

    def test_write_json_private_is_0600(self):
        p = self.dir / "out.json"
        llbot_config.write_json_private(p, {"token": "x"})
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)

    def test_cli_out_refuses_to_overwrite(self):
        cfg_path = self.dir / "cmd_config.json"
        cfg_path.write_text(json.dumps(_astrbot_cfg()), encoding="utf-8")
        out = self.dir / "frag.json"
        out.write_text("{}", encoding="utf-8")
        rc = llbot_config.main(["--astrbot-config", str(cfg_path), "--out", str(out)])
        self.assertEqual(rc, 2)

    def test_cli_check_returns_0_when_in_sync(self):
        cfg_path = self.dir / "cmd_config.json"
        cfg_path.write_text(json.dumps(_astrbot_cfg()), encoding="utf-8")
        llbot_path = self.dir / "llbot.json"
        entry, _ = llbot_config.derive_entry(_astrbot_cfg(), _Args())
        llbot_path.write_text(json.dumps(
            {"ob11": {"enable": True, "connect": [entry]}}), encoding="utf-8")
        rc = llbot_config.main(["--astrbot-config", str(cfg_path),
                                "--check", str(llbot_path)])
        self.assertEqual(rc, 0)

    def test_cli_check_returns_1_when_drifted(self):
        cfg_path = self.dir / "cmd_config.json"
        cfg_path.write_text(json.dumps(_astrbot_cfg()), encoding="utf-8")
        llbot_path = self.dir / "llbot.json"
        llbot_path.write_text(json.dumps(
            {"ob11": {"enable": True, "connect": []}}), encoding="utf-8")
        rc = llbot_config.main(["--astrbot-config", str(cfg_path),
                                "--check", str(llbot_path)])
        self.assertEqual(rc, 1)

    def test_cli_merge_into_writes_with_backup(self):
        cfg_path = self.dir / "cmd_config.json"
        cfg_path.write_text(json.dumps(_astrbot_cfg()), encoding="utf-8")
        llbot_path = self.dir / "llbot.json"
        llbot_path.write_text(json.dumps(
            {"ob11": {"enable": False, "connect": []}}), encoding="utf-8")
        rc = llbot_config.main(["--astrbot-config", str(cfg_path),
                                "--merge-into", str(llbot_path)])
        self.assertEqual(rc, 0)
        written = json.loads(llbot_path.read_text("utf-8"))
        self.assertEqual(written["ob11"]["connect"][0]["messageFormat"], "array")
        self.assertTrue(written["ob11"]["enable"])
        self.assertEqual(os.stat(llbot_path).st_mode & 0o777, 0o600)
        baks = list(self.dir.glob("llbot.json.bak.*"))
        self.assertEqual(len(baks), 1)


if __name__ == "__main__":
    unittest.main()
