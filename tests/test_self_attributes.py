# -*- coding: utf-8 -*-
"""`self.<attr>` 完整性静态检查（S12 事故后新增，2026-09-15）。

## 为什么需要

`tests/test_static_checks.py` 只查**模块级**未定义名（函数/import），查不出
`self.xxx` 这类**属性**问题。而本项目的两次真实事故都属于这一类：

1. **`_apply_live_config` 定义被删**（只留调用点）→ `/admin reload` 才炸
2. **`_admin_binding` 整段丢失** → 用户跑 `/admin` 才看到
   `'PersonaAgent' object has no attribute '_admin_binding'`

两次都是"改旧代码时漏看消费方"，且**测试全绿**（main.py 不被测试导入）。

## 本检查做什么

用 AST 扫 main.py 的 `PersonaAgent` 类，收集：
  - **写入点**：`self.X = ...`（含 `__init__` 里的类型注记赋值）
  - **读取点**：`self.X`（Load 上下文，含 `self.X()` 调用）

报告两类可疑：
  - **只读不写**：读取了但类里没有任何赋值 → 属性必然不存在（就是本次的 bug）
  - **只写不读**：赋值了但从不读取 → 多半是"读取点被误删"（`_sleep_override`
    那次就是：写入点还在，`_is_sleeping` 的比较逻辑被改成不匹配的类型）

白名单：少数属性由框架/基类提供（如 `self.context`），需显式列出。
"""
import ast
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAIN = REPO / "main.py"

# 由宿主/基类提供的属性（不是本类赋值的）
PROVIDED = {
    "context",       # astrbot Star 基类注入
    "config",
    "name",
}

# 已知的"只写不读"（**如实列出，不粉饰**）—— 它们是真实的死代码，
# 但清理它们不属于本次任务。列在这里让检查器只报告**新增**的这类问题。
#
# 这两条的成因都是"删旧代码时漏删/漏看消费方"（与 `_admin_binding` 同源）：
#   - `_decision_log_path`：只赋不用（日志路径改走 JsonStore）
#   - `_sleep_enabled`：`/admin sleep` 重构后读取点消失，配置的 sleep 开关不再生效
#
# 本检查器是**启发式**，有两处已知漏报（不因遗漏而失去价值，但要知道边界）：
#   1. 括号内的赋值（续行写法 `f(` 换行 `  self.X,`）不会被算作写入
#      → 会误报成"只读不写"（实测 `_REL_STATE` 就是这类）
#   2. 条件分支里的读取（如 `_dream_job` 只在 cron 注册块内使用）可能被漏算
# 因此本测试的价值在于**捕获新增问题**，而非完备证明。
# 已知的"只写不读"白名单 —— **当前为空**（S13 清理批次已处理全部三处）：
#   - `_REL_STATE`：检查器补上"类级属性收集"后不再漏报
#   - `_decision_log_path`：删除（只赋不用的历史遗留）
#   - `_sleep_enabled`：删除（`_is_sleeping` 每次读活配置，该快照无用）
#   - `_dream_job`：随 `services/dream_job.py` 整体删除（S11 起即为死代码）
#
# 保留这个集合是为了**将来**：若某个"只写不读"是刻意为之（如纯观测字段），
# 列在这里而不是让测试失败。
#
# ⚠️ 本检查器是**启发式**，边界如实记录：
#   1. 括号内的赋值（续行写法）不会被算作写入 → 会误报"只读不写"
#   2. 条件分支里的读取可能被漏算
#   3. 只覆盖 `PersonaAgent` 一个类
# 价值在于**捕获新增问题**，而非完备证明。
KNOWN_WRITE_ONLY: set[str] = set()


def _class_attrs(path: Path, cls_name: str) -> tuple[set[str], set[str]]:
    """返回 (被赋值的属性名, 被读取的**非方法**属性名)。

    三个必须注意的点（本检查器前三版都栽过）：
      1. 必须排除**方法名** —— `self._admin_bind(...)` 是方法调用，
         AST 上与属性读取长得一样；不排除会把所有方法调用误报
      2. 必须排除**赋值目标节点本身** —— `self.X = 1` 里的 `self.X` 也是
         `ast.Attribute`，不排除会同时进 writes 和 reads
      3. 必须收集**类级属性** —— `_REL_STATE = "..."` 写在类体里（不带 self.），
         不收集会把它误报成"只读不写"
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            target = node
            break
    if target is None:
        return set(), set()

    methods = {n.name for n in target.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    writes: set[str] = set()
    for node in target.body:                      # 类级属性/常量
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    writes.add(t.id)

    write_nodes: set[int] = set()
    for node in ast.walk(target):                 # self.X = ...
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            tgts = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in tgts:
                cand = t.elts if isinstance(t, (ast.Tuple, ast.List)) else [t]
                for el in cand:
                    if (isinstance(el, ast.Attribute)
                            and isinstance(el.value, ast.Name)
                            and el.value.id == "self"):
                        writes.add(el.attr)
                        write_nodes.add(id(el))

    reads: set[str] = set()
    for node in ast.walk(target):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and id(node) not in write_nodes
                and node.attr not in methods):
            reads.add(node.attr)
    return writes, reads


class TestSelfAttributes(unittest.TestCase):
    def test_no_read_without_write(self):
        """🔴 只读不写 = 属性必然不存在（S12 的 `_admin_binding` 事故）。"""
        writes, reads = _class_attrs(MAIN, "PersonaAgent")
        self.assertTrue(writes, "没扫到任何 self 赋值 —— 检查器本身可能失效")
        missing = sorted(reads - writes - PROVIDED)
        self.assertEqual(
            missing, [],
            "以下 self 属性被读取但类里从未赋值（必然 AttributeError）：\n  "
            + "\n  ".join(missing),
        )

    def test_no_write_without_read(self):
        """只写不读 = 多半是读取点被误删（`_sleep_override` 那次）。"""
        writes, reads = _class_attrs(MAIN, "PersonaAgent")
        unused = sorted(a for a in (writes - reads) if a not in KNOWN_WRITE_ONLY)
        self.assertEqual(
            unused, [],
            "以下 self 属性被赋值但从不读取（读取点可能被误删）：\n  "
            + "\n  ".join(unused),
        )

    def test_checker_actually_detects_missing_attr(self):
        """自证：检查器确实能抓到"只读不写"（否则测试是空转的）。"""
        import tempfile
        src = (
            "class C:\n"
            "    def __init__(self):\n"
            "        self.ok = 1\n"
            "    def f(self):\n"
            "        return self.ok + self.missing\n"
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "m.py"
            p.write_text(src, encoding="utf-8")
            w, r = _class_attrs(p, "C")
            self.assertIn("missing", r - w, "检查器必须能发现只读不写的属性")

    def test_checker_detects_write_only(self):
        import tempfile
        src = (
            "class C:\n"
            "    def __init__(self):\n"
            "        self.x = 1\n"
            "        self.never_read = 2\n"
            "    def f(self):\n"
            "        return self.x\n"
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "m.py"
            p.write_text(src, encoding="utf-8")
            w, r = _class_attrs(p, "C")
            self.assertIn("never_read", w - r)


if __name__ == "__main__":
    unittest.main()
