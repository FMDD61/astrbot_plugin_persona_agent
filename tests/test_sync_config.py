"""Tests for tools/sync_config.py (A2 config-schema merge, stdlib only)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from tools.sync_config import merge_defaults, load_json, write_json, backup, main
except ImportError:
    from astrbot_plugin_persona_agent.tools.sync_config import (
        merge_defaults, load_json, write_json, backup, main,
    )


SCHEMA = {
    "target_group_id": {"type": "string", "default": "881438753"},
    "test_mode": {"type": "int", "default": 0},
    "rag": {
        "type": "object",
        "items": {
            "k_retrieve": {"type": "int", "default": 8},
            "score_threshold": {"type": "float", "default": 0.55},
        },
    },
    "llm": {
        "type": "object",
        "items": {
            "temperature": {
                "type": "object",
                "items": {
                    "at_reply": {"type": "float", "default": 0.8},
                    "cold_start": {"type": "float", "default": 1.1},
                },
            },
            "provider_id": {"type": "string", "default": ""},
        },
    },
}


class MergeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg: dict = {}

    def test_fills_missing_keys_recursively(self) -> None:
        warnings: list[str] = []
        added = merge_defaults(self.cfg, SCHEMA, warnings)
        self.assertEqual(warnings, [])
        self.assertEqual(self.cfg["test_mode"], 0)
        self.assertEqual(self.cfg["rag"]["k_retrieve"], 8)
        self.assertEqual(self.cfg["llm"]["temperature"]["cold_start"], 1.1)
        self.assertIn("rag.k_retrieve", added)
        self.assertIn("llm.temperature.cold_start", added)

    def test_preserves_existing_values(self) -> None:
        self.cfg = {"test_mode": 1, "rag": {"score_threshold": 0.9}}
        warnings: list[str] = []
        merge_defaults(self.cfg, SCHEMA, warnings)
        self.assertEqual(self.cfg["test_mode"], 1)
        self.assertEqual(self.cfg["rag"]["score_threshold"], 0.9)
        self.assertEqual(self.cfg["rag"]["k_retrieve"], 8)

    def test_preserves_unknown_keys(self) -> None:
        self.cfg = {"legacy_key": "keep-me"}
        warnings: list[str] = []
        merge_defaults(self.cfg, SCHEMA, warnings)
        self.assertEqual(self.cfg["legacy_key"], "keep-me")

    def test_type_mismatch_kept_with_warning(self) -> None:
        self.cfg = {"rag": {"score_threshold": "high"}}
        warnings: list[str] = []
        merge_defaults(self.cfg, SCHEMA, warnings)
        self.assertEqual(self.cfg["rag"]["score_threshold"], "high")
        self.assertTrue(any("score_threshold" in w for w in warnings))

    def test_int_float_family_accepted_silently(self) -> None:
        self.cfg = {"rag": {"score_threshold": 1}}
        warnings: list[str] = []
        merge_defaults(self.cfg, SCHEMA, warnings)
        self.assertEqual(warnings, [])
        self.assertEqual(self.cfg["rag"]["score_threshold"], 1)

    def test_non_dict_object_slot_kept_with_warning(self) -> None:
        self.cfg = {"rag": "nope"}
        warnings: list[str] = []
        merge_defaults(self.cfg, SCHEMA, warnings)
        self.assertEqual(self.cfg["rag"], "nope")
        self.assertTrue(any("rag" in w for w in warnings))


class FileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg_path = self.dir / "plugin_config.json"
        self.schema_path = self.dir / "schema.json"
        self.schema_path.write_text(json.dumps(SCHEMA, ensure_ascii=False), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_cfg(self, with_bom: bool = True) -> None:
        data = json.dumps({"test_mode": 1}, ensure_ascii=False, indent=2)
        self.cfg_path.write_bytes((b"\xef\xbb\xbf" if with_bom else b"") + data.encode("utf-8"))

    def test_check_mode_reports_changes_without_writing(self) -> None:
        self._write_cfg()
        before = self.cfg_path.read_bytes()
        rc = main(["--config", str(self.cfg_path), "--schema", str(self.schema_path)])
        self.assertEqual(rc, 1)
        self.assertEqual(self.cfg_path.read_bytes(), before)

    def test_write_mode_applies_atomically_with_backup(self) -> None:
        self._write_cfg()
        rc = main(["--config", str(self.cfg_path), "--schema", str(self.schema_path), "--write"])
        self.assertEqual(rc, 0)
        tmp_leftovers = list(self.dir.glob("*.tmp"))
        self.assertEqual(tmp_leftovers, [])
        backups = list(self.dir.glob("plugin_config.json.bak.*"))
        self.assertEqual(len(backups), 1)
        data = load_json(self.cfg_path)
        self.assertEqual(data["test_mode"], 1)
        self.assertEqual(data["rag"]["k_retrieve"], 8)

    def test_output_preserves_bom(self) -> None:
        self._write_cfg(with_bom=True)
        main(["--config", str(self.cfg_path), "--schema", str(self.schema_path), "--write"])
        raw = self.cfg_path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(load_json(self.cfg_path)["target_group_id"], "881438753")

    def test_idempotent_second_run(self) -> None:
        self._write_cfg()
        main(["--config", str(self.cfg_path), "--schema", str(self.schema_path), "--write"])
        rc = main(["--config", str(self.cfg_path), "--schema", str(self.schema_path)])
        self.assertEqual(rc, 0)

    def test_backup_is_byte_identical(self) -> None:
        self._write_cfg()
        main(["--config", str(self.cfg_path), "--schema", str(self.schema_path), "--write"])
        bak_path = list(self.dir.glob("plugin_config.json.bak.*"))[0]
        before = json.dumps({"test_mode": 1}, ensure_ascii=False, indent=2)
        self.assertEqual(bak_path.read_bytes(), b"\xef\xbb\xbf" + before.encode("utf-8"))

    def test_json_summary_flag(self) -> None:
        self._write_cfg()
        rc = main(["--config", str(self.cfg_path), "--schema", str(self.schema_path), "--json"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
