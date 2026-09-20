# -*- coding: utf-8 -*-
"""生成物 ↔ 草案一致性（C1/C2 示例块的防漂移闸）。

同 `test_persona_source_sync`：真身在 `docs/specs/` 的文案草案里，
`services/examples_default.py` 是生成物。草案改了却没重跑生成器 → 线上跑的与文档写的
不是同一份文案。设计文档不在插件仓库内，所以 **specs 缺失时跳过**。

另加一条**不依赖 specs** 的内容指纹（见 `test_examples_content_frozen`）：
台式机 `git pull` 后没有 docs/specs，上面的闸会 skip。
"""
import hashlib
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services import examples_default  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
SPECS = os.path.abspath(os.path.join(REPO, "..", "docs", "specs"))


@unittest.skipUnless(os.path.isdir(SPECS), "设计文档目录不存在（非开发机）")
class TestExamplesSourceSync(unittest.TestCase):
    def test_generated_matches_drafts(self):
        r = subprocess.run(
            [sys.executable, os.path.join(REPO, "tools", "gen_examples_default.py"),
             "--check", "--specs", SPECS],
            capture_output=True, text=True, cwd=REPO,
        )
        self.assertEqual(
            r.returncode, 0,
            "examples_default.py 与 docs/specs 草案不一致 —— 重跑 "
            "tools/gen_examples_default.py：\n" + r.stdout + r.stderr)


class TestExamplesContentFrozen(unittest.TestCase):
    """不依赖 docs/specs 的内容指纹（生产机上唯一有效的闸）。"""

    #: sha256(HEADER + "\n" + "\n".join(渲染行))[:12]
    FROZEN_HEADER = "示例对话（就是这种语感，照着说）："
    FROZEN_COUNT = 20

    def test_header_and_count(self):
        self.assertEqual(examples_default.HEADER, self.FROZEN_HEADER)
        self.assertEqual(len(examples_default.ENTRIES), self.FROZEN_COUNT)

    def _frozen(self) -> dict:
        with open(os.path.join(HERE, "examples_frozen.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_content_hash(self):
        blob = json.dumps(examples_default.ENTRIES, ensure_ascii=False, sort_keys=True)
        got = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
        self.assertEqual(got, self._frozen()["entries_sha256_12"],
                         "示例文案变了：若是有意改的，重跑生成器并更新 examples_frozen.json")

    def test_rendered_block_hash(self):
        """N-3：钉住**渲染后的块文本** —— 只哈希 ENTRIES 的话，改 `_render()`
        （分隔符/丢条目规则）不会触发任何闸门。
        """
        from services.examples import load_examples_block
        block, _st = load_examples_block("/nonexistent/example_dialogs.json")
        got = hashlib.sha256(block.encode("utf-8")).hexdigest()[:12]
        self.assertEqual(got, self._frozen()["rendered_sha256_12"],
                         "渲染结果变了（可能是 _render() 改了）：确认后更新指纹")

    def test_emote_density_is_pinned(self):
        """N-4：emote 密度是 C2/要求② 要控的东西，必须有闸门。

        背景：草案 §C 声称「8 条带 [emote:]」，而 §B 的实际文本是 **5 条**
        （已向使用者报备）。把实际值钉住 —— 将来草案改成 4/6 条并更新指纹时，
        这条会逼着人显式确认「交付的 emote 密度变了」。
        """
        n = sum(1 for e in examples_default.ENTRIES
                if "[emote:" in json.dumps(e, ensure_ascii=False))
        self.assertEqual(n, self._frozen()["emote_count"],
                         "示例块里的 [emote:] 条数变了（草案 §C 说要 8 条）")

    def test_schema_default_matches_max_entries(self):
        """N-7：schema 的 default 与代码常量必须一致（此前只是"目前恰好一致"）。"""
        from services.examples import MAX_ENTRIES
        p = os.path.join(REPO, "_conf_schema.json")
        with open(p, encoding="utf-8") as f:
            schema = json.load(f)
        self.assertEqual(schema["examples"]["items"]["max_entries"]["default"],
                         MAX_ENTRIES)


if __name__ == "__main__":
    unittest.main()
