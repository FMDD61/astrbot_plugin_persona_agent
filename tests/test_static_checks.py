# -*- coding: utf-8 -*-
"""静态检查：捕获只有运行时才暴露的"未定义名"类错误。

## 为什么需要（2026-09-14 实测教训）

`main._send_sticker()` 里写了 `os.path.exists(path)`，但 **main.py 从未
`import os`** → 线上每次 `[emote:]` 都抛 `NameError`，**贴纸一张没发出**，
而当时的测试套件**全绿** —— 因为 `main.py` 从不被测试导入（它依赖 astrbot
运行时），所以任何"main 里用错名字"的错误都测不到。

这不是理论风险，是实际发生的事故：从 09-13 晚到 09-14 中午，4 次表情意图
全部静默失败（`sticker_log.jsonl` 里 4 条 NameError），而日志级别是 INFO，
没有告警。

本测试用 AST 粗查所有模块的"加载了但未绑定"的名字 —— 覆盖 pyflakes 的核心
能力，无需额外依赖。
"""
import ast
import builtins
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_WHITELIST = {
    "__file__", "__name__", "__doc__", "__package__", "__builtins__",
    "__spec__", "__loader__", "__annotations__",
}


def undefined_names(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bound = set(dir(builtins)) | _WHITELIST
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                bound.add(a.asname or a.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Global):
            bound.update(node.names)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in bound:
                bad.append((node.id, node.lineno))
    return bad


class TestNoUndefinedNames(unittest.TestCase):
    def _files(self):
        out = [REPO / "main.py"]
        out += sorted((REPO / "services").glob("*.py"))
        out += sorted((REPO / "tools").glob("*.py"))
        return [p for p in out if p.is_file()]

    def test_no_undefined_names(self):
        problems = []
        for p in self._files():
            try:
                for name, line in sorted(set(undefined_names(p))):
                    problems.append(f"{p.relative_to(REPO)}:{line} 未定义名 {name!r}")
            except SyntaxError as e:
                problems.append(f"{p.relative_to(REPO)}: 语法错误 {e}")
        self.assertEqual(
            problems, [],
            "发现未定义名（多半是漏了 import；这类错误只在运行时暴露，"
            "而 main.py 不被测试导入 → 必须静态查）：\n  " + "\n  ".join(problems),
        )

    def test_checker_actually_catches_missing_import(self):
        """自证：检查器能抓到"用了没导入的名字"（否则测试是空转的）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "sample.py"
            f.write_text("def f():\n    return os.path.exists('x')\n", encoding="utf-8")
            self.assertIn("os", [n for n, _ in undefined_names(f)])
            f.write_text("import os\ndef f():\n    return os.path.exists('x')\n",
                         encoding="utf-8")
            self.assertEqual(undefined_names(f), [])

    def test_main_py_specific_regression(self):
        """回归：main.py 必须 import os（_send_sticker 用它判文件存在）。"""
        src = (REPO / "main.py").read_text(encoding="utf-8")
        self.assertIn("import os", src)
        self.assertNotIn("os.path.exists(os", src)   # 顺带防止写错


if __name__ == "__main__":
    unittest.main()
