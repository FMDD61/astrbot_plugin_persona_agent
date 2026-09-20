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


class TestInstrumentationHasReaders(unittest.TestCase):
    """**只写不读的仪表盘 = 没有仪表盘**（2026-09-20 独立核验 R4 的教训）。

    `StyleProfile` 上的降级留痕字段（`last_summary_fallback` / `last_gate_fallback` / …）
    曾经"设了但全仓无人读"：docstring 承诺"日志里看得见"没兑现 —— 而这类失效**不会报错**，
    只会让运维在真降级时一无所获。本测试用最粗的判据把它钉住：
    **每个留痕字段必须在 `services/style_profile.py` 之外至少有一个出现点**（即真有人读）。
    """

    #: 字段必须**直接被别处读**（属性名出现在别的模块里）
    DIRECT_FIELDS = ("last_summary_fallback", "last_gate_fallback",
                     "last_persona_report")
    #: 字段经由 `persona_manifest()` 出到清单，再由消费方读**清单键**
    MANIFEST_FIELDS = (("last_memory_error", "memory_error"),
                       ("last_materialize_error", "materialize_error"))

    def _sources(self):
        out = [REPO / "main.py"]
        out += sorted((REPO / "services").glob("*.py"))
        out += sorted((REPO / "tools").glob("*.py"))
        return [p for p in out if p.is_file()]

    def _readers(self, needle: str) -> list:
        return [p.relative_to(REPO) for p in self._sources()
                if needle in p.read_text(encoding="utf-8")
                and p.name != "style_profile.py"]

    def test_each_fallback_marker_has_a_reader(self):
        for field in self.DIRECT_FIELDS:
            self.assertTrue(
                self._readers(field),
                f"{field} 只写不读（无仪表盘）：给它一个出口（日志/trace），或删掉它",
            )

    def test_manifest_surfaced_markers_have_readers(self):
        for field, key in self.MANIFEST_FIELDS:
            self.assertTrue(
                self._readers(f'"{key}"'),
                f"{field} 只经 manifest 输出为 {key!r}，但没人读那个键 → 仍是没有仪表盘",
            )

    def test_checker_actually_catches_write_only_field(self):
        """自证：凭空造的字段名应当找不到读者（否则本测试是空转的）。"""
        readers = [p for p in self._sources()
                   if "last_nonexistent_marker" in p.read_text(encoding="utf-8")]
        self.assertEqual(readers, [])


if __name__ == "__main__":
    unittest.main()
