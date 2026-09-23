# -*- coding: utf-8 -*-
"""仓库外人格文案 ↔ 草案一致性（C7/C10 的防漂移闸）。

🔴 2026-09-23 脱敏后，这条链上的三样东西**都不在插件仓库里**：

| 角色 | 位置 |
|---|---|
| 文案真身（**数据**） | 仓库外 `data_out/persona/*.md` |
| 文案草案（**设计**） | 仓库外 `docs/specs/` |
| 生成器 | `tools/gen_persona_sections.py`（默认写 `data_out/`） |

草案改了却没重跑生成器 → 生产跑的与文档写的不是同一份文案（本项目的老毛病，
B-030 同族）。这条测试把两者钉死；**两者缺一即 skip** —— 台式机 `git pull` 后
只有代码，没有 `data_out/` 与 `docs/specs/`，不该因此红掉整个套件。
"""
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
SPECS = os.path.abspath(os.path.join(REPO, "..", "docs", "specs"))
DATA_OUT = os.path.abspath(os.path.join(REPO, "..", "data_out"))
PERSONA = os.path.join(DATA_OUT, "persona")
GEN = os.path.join(REPO, "tools", "gen_persona_sections.py")


@unittest.skipUnless(os.path.isdir(SPECS), "设计文档目录不存在（非开发机）")
@unittest.skipUnless(os.path.isdir(PERSONA), "仓库外人格文案不存在（非开发机）")
class TestPersonaSourceSync(unittest.TestCase):
    def test_data_out_matches_drafts(self):
        r = subprocess.run(
            [sys.executable, GEN, "--check", "--specs", SPECS, "--out", DATA_OUT],
            capture_output=True, text=True, cwd=REPO,
        )
        self.assertEqual(
            r.returncode, 0,
            "data_out/persona 与 docs/specs 草案不一致 —— 重跑 "
            "tools/gen_persona_sections.py：\n" + r.stdout + r.stderr)


@unittest.skipUnless(os.path.isdir(SPECS), "设计文档目录不存在（非开发机）")
class TestGeneratorRefusesToWriteInsideRepo(unittest.TestCase):
    """🔴 public 仓库的硬闸：真实文案**不许**写回插件仓库。"""

    def test_out_pointing_inside_repo_is_rejected(self):
        r = subprocess.run(
            [sys.executable, GEN, "--specs", SPECS, "--out", REPO],
            capture_output=True, text=True, cwd=REPO,
        )
        self.assertNotEqual(r.returncode, 0, "写进仓库必须报错退出")
        self.assertIn("拒绝写入仓库内路径", r.stdout + r.stderr)
        # 没留下任何东西
        self.assertFalse(os.path.isdir(os.path.join(REPO, "persona")),
                         "拒绝之后不得在仓库里留下数据目录")


if __name__ == "__main__":
    unittest.main()
