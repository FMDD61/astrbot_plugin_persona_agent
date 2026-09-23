# -*- coding: utf-8 -*-
"""仓库外示例语料 ↔ 草案一致性 + 内容指纹（C1/C2 的防漂移闸）。

同 `test_persona_source_sync`：真身在仓库外的 `data_out/examples.json`，
插件 `services/examples_default.py` 只剩 HEADER 与加载契约（`ENTRIES` 恒空）。

* 一致性闸需要 `docs/specs/` + `data_out/` → 缺一 skip；
* 内容指纹只需要 `data_out/` → 台式机没有它，所以也 skip（开发机上仍然是硬闸）。
"""
import hashlib
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
SPECS = os.path.abspath(os.path.join(REPO, "..", "docs", "specs"))
DATA_OUT = os.path.abspath(os.path.join(REPO, "..", "data_out"))
EXAMPLES = os.path.join(DATA_OUT, "examples.json")
FROZEN = os.path.join(DATA_OUT, "examples_frozen.json")
GEN = os.path.join(REPO, "tools", "gen_examples_default.py")


def _load():
    with open(EXAMPLES, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return str(data.get("header") or ""), list(data.get("entries") or [])
    return "", list(data)


@unittest.skipUnless(os.path.isdir(SPECS), "设计文档目录不存在（非开发机）")
@unittest.skipUnless(os.path.isfile(EXAMPLES), "仓库外示例语料不存在（非开发机）")
class TestExamplesSourceSync(unittest.TestCase):
    def test_data_out_matches_drafts(self):
        r = subprocess.run(
            [sys.executable, GEN, "--check", "--specs", SPECS, "--out", DATA_OUT],
            capture_output=True, text=True, cwd=REPO,
        )
        self.assertEqual(
            r.returncode, 0,
            "data_out/examples.json 与 docs/specs 草案不一致 —— 重跑 "
            "tools/gen_examples_default.py：\n" + r.stdout + r.stderr)


@unittest.skipUnless(os.path.isfile(EXAMPLES), "仓库外示例语料不存在（非开发机）")
class TestExternalExamplesFrozen(unittest.TestCase):
    """不依赖 docs/specs 的内容指纹（仓库外语料的防手改闸）。"""

    def _frozen(self) -> dict:
        with open(FROZEN, encoding="utf-8") as f:
            return json.load(f)

    def test_header_count_and_emote_density(self):
        header, entries = _load()
        fp = self._frozen()
        self.assertEqual(header, fp["header"])
        self.assertEqual(len(entries), fp["count"], "语料条数变了")
        n = sum(1 for e in entries if "[emote:" in json.dumps(e, ensure_ascii=False))
        self.assertEqual(n, fp["emote_count"],
                         "示例块里的 [emote:] 条数变了（草案 §C 说要 8 条）")

    def test_entries_hash(self):
        _header, entries = _load()
        blob = json.dumps(entries, ensure_ascii=False, sort_keys=True)
        got = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
        self.assertEqual(got, self._frozen()["entries_sha256_12"],
                         "示例文案变了：若是有意改的，重跑生成器并更新指纹")

    def test_rendered_block_hash(self):
        """N-3：钉住**渲染后的块文本** —— 只哈希 ENTRIES 的话，改 `_render()`
        （分隔符/丢条目规则）不会触发任何闸门。
        """
        from services.examples import load_examples_block
        block, st = load_examples_block(EXAMPLES)
        self.assertEqual(st.source, "file", "仓库外语料必须能真的被 loader 吃下")
        got = hashlib.sha256(block.encode("utf-8")).hexdigest()[:12]
        self.assertEqual(got, self._frozen()["rendered_sha256_12"],
                         "渲染结果变了（可能是 _render() 改了）：确认后更新指纹")


class TestSchemaConsistency(unittest.TestCase):
    """不依赖任何仓库外数据（生产机上唯一恒亮的闸）。"""

    def test_schema_default_matches_max_entries(self):
        """N-7：schema 的 default 与代码常量必须一致（此前只是"目前恰好一致"）。"""
        from services.examples import MAX_ENTRIES
        with open(os.path.join(REPO, "_conf_schema.json"), encoding="utf-8") as f:
            schema = json.load(f)
        self.assertEqual(schema["examples"]["items"]["max_entries"]["default"],
                         MAX_ENTRIES)


if __name__ == "__main__":
    unittest.main()
