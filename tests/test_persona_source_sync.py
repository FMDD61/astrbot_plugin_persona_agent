# -*- coding: utf-8 -*-
"""生成物 ↔ 草案一致性（C7/C10 的防漂移闸）。

`services/persona_sections.py` 是**生成物**：真身在 `docs/specs/` 的文案草案里。
草案改了却没重跑生成器 → 线上跑的与文档写的不是同一份文案（本项目的老毛病：
文档与代码相反，B-030 同族）。这条测试把两者钉死。

设计文档不在插件仓库内（根目录不是 git 仓库），所以 **specs 缺失时跳过**，
不让"没有文档的环境"红掉整个套件。
"""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
SPECS = os.path.abspath(os.path.join(REPO, "..", "docs", "specs"))


@unittest.skipUnless(os.path.isdir(SPECS), "设计文档目录不存在（非开发机）")
class TestPersonaSourceSync(unittest.TestCase):
    def test_generated_matches_drafts(self):
        r = subprocess.run(
            [sys.executable, os.path.join(REPO, "tools", "gen_persona_sections.py"),
             "--check", "--specs", SPECS],
            capture_output=True, text=True, cwd=REPO,
        )
        self.assertEqual(
            r.returncode, 0,
            "persona_sections.py 与 docs/specs 草案不一致 —— 重跑 "
            "tools/gen_persona_sections.py：\n" + r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
