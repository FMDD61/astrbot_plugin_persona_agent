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


def reads_of(path: Path, field=None, key=None) -> list[tuple[int, str]]:
    """用 **AST** 找出对某字段/某清单键的**真读**：(行号, 形态) 列表。

    只认三种形态，**完全不看注释与 docstring**（`ast` 里注释根本不出现，
    docstring 是 `Expr(Constant)` 也不会命中）：

      ① 属性读：``x.last_summary_fallback``；
      ② ``getattr(x, "last_summary_fallback", ...)``；
      ③ 清单键：``d["memory_error"]`` / ``d.get("memory_error")``。

    为什么必须是 AST（独立核验第 3 轮证伪了子串版）：子串匹配会把
    "注释里提了一句字段名"当成有人读 —— 把真读删掉、只留注释，闸门**仍然绿**。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if field and isinstance(node, ast.Attribute) and node.attr == field \
                and isinstance(node.ctx, ast.Load):
            hits.append((node.lineno, "attr-load"))
        if field and isinstance(node, ast.Call) \
                and isinstance(node.func, ast.Name) and node.func.id == "getattr" \
                and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) \
                and node.args[1].value == field:
            hits.append((node.lineno, "getattr"))
        if key:
            if isinstance(node, ast.Subscript) \
                    and isinstance(node.slice, ast.Constant) \
                    and node.slice.value == key:
                hits.append((node.lineno, "subscript"))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "get" and node.args \
                    and isinstance(node.args[0], ast.Constant) \
                    and node.args[0].value == key:
                hits.append((node.lineno, "dict.get"))
    return sorted(set(hits))


class TestInstrumentationHasReaders(unittest.TestCase):
    """**只写不读的仪表盘 = 没有仪表盘**（2026-09-20 独立核验 R4 的教训）。

    `StyleProfile` 上的降级留痕字段（`last_summary_fallback` / `last_gate_fallback` / …）
    曾经"设了但全仓无人读"：docstring 承诺"日志里看得见"没兑现 —— 而这类失效**不会报错**，
    只会让运维在真降级时一无所获。

    ⚠️ **判据必须是 AST，不能是子串匹配**（独立核验第 3 轮证伪了我的第一版）：
    第一版对整文件文本做 `in` 判断 → **注释与 docstring 里的字段名也算"读者"**；
    变异实验（删掉两处真读、只留注释）下它**仍然绿** —— 判据松了等于没有闸门。
    `ast` 天然不含注释，docstring 是 `Expr(Constant)` 不会被误判。
    """

    #: 字段必须**直接被别处读**（属性读 / getattr）
    DIRECT_FIELDS = ("last_summary_fallback", "last_gate_fallback",
                     "last_persona_report")
    #: 字段经由 `persona_manifest()` 出到清单，再由消费方读**清单键**
    MANIFEST_FIELDS = (("last_memory_error", "memory_error"),
                       ("last_materialize_error", "materialize_error"))

    def _sources(self):
        out = [REPO / "main.py"]
        out += sorted((REPO / "services").glob("*.py"))
        out += sorted((REPO / "tools").glob("*.py"))
        # 定义方除外：它自己写字段，不能算"有人读"
        return [p for p in out if p.is_file() and p.name != "style_profile.py"]

    def _readers(self, field, key) -> list:
        found = []
        for src in self._sources():
            for line, kind in reads_of(src, field=field, key=key):
                found.append(f"{src.relative_to(REPO)}:{line}({kind})")
        return found

    def test_each_fallback_marker_has_a_real_reader(self):
        for field in self.DIRECT_FIELDS:
            self.assertTrue(
                self._readers(field, None),
                f"{field} 只有写入、没有真读（无仪表盘）：给它一个出口，或删掉它",
            )

    def test_manifest_surfaced_markers_have_real_readers(self):
        for field, key in self.MANIFEST_FIELDS:
            self.assertTrue(
                self._readers(None, key),
                f"{field} 经 manifest 输出为 {key!r}，但没人读那个键 → 仍是没有仪表盘",
            )

    def test_checker_is_not_fooled_by_comments_or_docstrings(self):
        """自证（双向）：注释/docstring 提及不算读；真读必须被认出来。

        第一版的自证是同义反复（断言"一个全仓都不存在的字符串找不到读者"，恒真）。
        这里造**合成模块**：写字段 + 只在注释/docstring 里提它 → 必须判无读者；
        再加一处真读 → 必须认出来。
        """
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "sample.py"
            f.write_text(
                'class S:\n'
                '    """docstring 里提到 last_marker，但它不是读。"""\n'
                '    def __init__(self):\n'
                '        self.last_marker = ""   # 写\n'
                '    def touch(self):\n'
                '        # 注释里也说一句 last_marker\n'
                '        return None\n',
                encoding="utf-8",
            )
            self.assertEqual(
                reads_of(f, field="last_marker", key=None), [],
                "注释/docstring 里的提及被误判成读了 —— 判据又松了",
            )
            f.write_text(
                f.read_text(encoding="utf-8")
                + "\ndef read_it(s):\n    return s.last_marker\n",
                encoding="utf-8",
            )
            self.assertTrue(
                reads_of(f, field="last_marker", key=None),
                "真读（属性 load）没被认出来",
            )


if __name__ == "__main__":
    unittest.main()




def gated_rotation_calls(tree) -> list:
    '''返回「被 `if <x>_msgs` 门控住、且没有 else 兜底」的换日界调用点。

    为什么需要这条判据（独立核验第 7 轮 N-1）：
    **B-1 的要害是「被门控」** —— 换日界的兜底路径一旦先跑，cron 就拿到 old_msgs=None，
    此时若组装写在 `if old_msgs:` **里面**，它会被整段跳过 → §3 停在昨天。
    而「函数文本里出现过 _rebuild_memory_digest」这种判据抓不到这一点：
    变异实测（M2）—— 把 `await _rotate_followup(...)` 塞进 `if old_msgs:` 之内，闸门仍然绿。

    规则：对每个测试 *_msgs 的 if：
      * 组装/跟进调用在它的 [lineno, end_lineno] **之外** → 合格；
      * 在**之内**，但该 if 有 else 且 else 里也通向组装 → 合格
        （这正是消息兜底路径的合法形态：if 里起任务、else 里直接组装）；
      * 其余（在 if 里且没有 else）→ **不合格**。
    '''
    bad = []
    follow = {'_rotate_followup', '_rebuild_memory_digest'}
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if '_msgs' not in ast.dump(node.test):
            continue
        inside = {c.func.attr for n in ast.walk(node) if n is not node
                  for c in [n] if isinstance(c, ast.Call)
                  and isinstance(c.func, ast.Attribute)}
        if not (inside & follow):
            continue
        else_calls = {c.func.attr for n in node.orelse for c in ast.walk(n)
                      if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
        if else_calls & follow:
            continue                      # 有 else 兜底 → 合格
        bad.append(('gated_rotation_call', node.lineno))
    return bad


class TestRotationOrderC4(unittest.TestCase):
    """🔴 C23-b / C4：轮转必须**先总结、后组装**（`summary_v2.md` §4.3 的时序）。

    两代缺陷、两种判据：

    1. **fire-and-forget**：`create_task(日记)` 发出去就不管 → 组装可能跑在总结之前；
    2. **只改了一条路径**（独立核验第 6 轮 B-1，**阻塞**）：轮转有**两条**入口
       （02:05 cron 与消息兜底），当时只给 cron 接了组装，而且兜底路径一先换日界，
       cron 就拿到 `old_msgs=None` → **组装被整段跳过** → §3 停在**昨天**。
       陈旧比空更糟：空是可见的，陈旧看起来完全正常。

    ⚠️ 所以判据**不能只看某一个函数名**（第一版就是这么漏掉 B-1 的）：
    这里扫**全文件** —— 每个 `rotate_if_day_changed` 调用点都必须通向组装。

    `main.py` 不被测试导入（依赖 astrbot 运行时），所以用 AST 静态查。
    """

    SRC = REPO / "main.py"

    def _tree(self):
        return ast.parse(self.SRC.read_text(encoding="utf-8"), filename=str(self.SRC))

    def _fns(self):
        return [n for n in ast.walk(self._tree())
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    def _fn(self, name):
        for node in self._fns():
            if node.name == name:
                return node
        self.fail(f"{name} 不见了")

    def _calls(self, node):
        return [(n.lineno, n.func.attr) for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]

    def _src(self, node):
        return ast.get_source_segment(self.SRC.read_text(encoding="utf-8"), node) or ""

    def test_no_fire_and_forget_diary_anywhere(self):
        """全文件禁 `create_task(self._generate_diary`（两条路径都算）。"""
        whole = self.SRC.read_text(encoding="utf-8").replace(" ", "")
        self.assertNotIn("create_task(self._generate_diary", whole,
                         "日记不许再 fire-and-forget —— 组装会跑在总结之前")

    def test_followup_awaits_then_rebuilds(self):
        """共用的那个协程：先 await 日记（有上界），再组装。"""
        src = self._src(self._fn("_rotate_followup"))
        self.assertIn("await asyncio.wait_for", src, "日记必须被 await（带超时上界）")
        calls = self._calls(self._fn("_rotate_followup"))
        diary = [ln for ln, name in calls if name == "_generate_diary"]
        digest = [ln for ln, name in calls if name == "_rebuild_memory_digest"]
        self.assertTrue(diary and digest, "总结与组装都要在")
        self.assertGreater(min(digest), max(diary), "组装必须在总结之后")

    def test_every_rotation_path_leads_to_digest(self):
        """🔴 B-1 的正面判据：**每个**换日界的地方都必须通向组装。"""
        found = 0
        for fn in self._fns():
            names = [name for _ln, name in self._calls(fn)]
            if "rotate_if_day_changed" not in names:
                continue
            found += 1
            src = self._src(fn)
            self.assertTrue(
                "_rotate_followup" in src or "_rebuild_memory_digest" in src,
                f"{fn.name} 换了日界却没通向组装（§3 会停在昨天）",
            )
        self.assertGreaterEqual(found, 2, "轮转路径应有两条（cron + 消息兜底）")


    def test_gate_misses_nothing_gated(self):
        '''闸门必须真的能抓「被 if old_msgs 门控」这一形态（第 7 轮 M2 变异）。

        代码当前是对的 —— 这条守的是**判据**本身：它在 M2 形态下必须报红，
        而在当前唯一的合法形态（if 里起任务 + else 里组装）下必须放行。
        '''
        from textwrap import dedent
        import tempfile as _tf

        def _bad(src):
            with _tf.TemporaryDirectory() as td:
                f = Path(td) / 'm.py'
                f.write_text(dedent(src), encoding='utf-8')
                return gated_rotation_calls(
                    ast.parse(f.read_text(encoding='utf-8')))

        # M2 形态：跟进调用被门控、没有 else → 必须报红
        self.assertTrue(_bad('''
            def job():
                old_msgs = rotate()
                if old_msgs:
                    await self._rotate_followup(gid, old_msgs)
        '''))
        # 合法形态（消息兜底路径）：if 里起任务、else 里组装 → 放行
        self.assertEqual(_bad('''
            def on_msg():
                old_msgs = rotate()
                if old_msgs:
                    asyncio.create_task(self._rotate_followup(gid, old_msgs))
                else:
                    self._rebuild_memory_digest(gid)
        '''), [])
        # 合法形态（cron）：调用在 if 之外 → 放行
        self.assertEqual(_bad('''
            def job():
                old_msgs = rotate()
                if old_msgs:
                    log.info('rotated')
                await self._rotate_followup(gid, old_msgs or [])
        '''), [])

    def test_real_main_has_no_gated_rotation(self):
        '''对真实 main.py 跑同一条判据。'''
        self.assertEqual(gated_rotation_calls(self._tree()), [])
    def test_period_summary_rebuilds_digest(self):
        names = [name for _ln, name in self._calls(self._fn("_period_summary"))]
        self.assertIn("_rebuild_memory_digest", names,
                      "周/月/年记写完也要重算 §3（它会改变分层边界）")


class TestB055B056Wiring(unittest.TestCase):
    """🔴 B-055 / B-056（2026-09-22 线上观察）的接线判据。

    两个缺陷都只在 main.py 里，而**测试从不导入 main.py**（见本文件开头的事故说明）
    → 用 AST 钉住「必须存在的形状」，让变异测试打得到。
    """

    @classmethod
    def setUpClass(cls):
        cls.src = (REPO / "main.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.src)

    def _calls(self, name: str) -> list[ast.Call]:
        out = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Attribute) and f.attr == name:
                    out.append(node)
        return out

    def test_rebuild_digest_is_guarded_by_freshness(self):
        """B-056：消息路径里的兜底重建必须**先问「今天组装过没有」**。

        没有这道闸 → 每条消息都重读摘要 + 刷日志（实测当天 7269 条）。
        """
        def calls_in(node, name):
            out = []
            for x in ast.walk(node):
                if isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute) \
                        and x.func.attr == name:
                    out.append(x)
            return out

        guarded = 0
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.If):
                continue
            test_calls = calls_in(node.test, "_digest_fresh_today")
            if not test_calls:
                continue
            for stmt in node.body:
                guarded += len(calls_in(stmt, "_rebuild_memory_digest"))
        self.assertGreaterEqual(
            guarded, 1,
            "兜底的 _rebuild_memory_digest 必须被 `if not self._digest_fresh_today(...)` 支配"
        )


    def test_diary_skip_is_warning_with_retry(self):
        """B-055：provider 未就绪时不能再只记 INFO 就走 —— 要重试 + WARNING 落到日志。"""
        i = self.src.index("日记**未生成**")
        seg = self.src[max(0, i - 700):i + 400]
        self.assertIn("logger.warning", seg, "跳过必须是 WARNING（INFO 会被淹没）")
        self.assertIn("asyncio.sleep", seg, "给一次重试窗口")
        self.assertIn("_last_provider_id", seg, "诊断要打出三级回退链的状态")

    def test_startup_backfills_missing_diary(self):
        """B-055：provider 就绪后要补写昨天缺失的日记（把「错过就永久缺」变成可自愈）。"""
        # ⚠️ 只取**函数体内**那一段：方法定义本身就在附近，用 "x in 后续 700 字"
        # 会被定义命中 → 闸无牙（第一版就是这么写的，变异实测 GREEN）。
        i = self.src.index("provider resolved at startup")
        j = self.src.index("async def _provider_exists", i)
        seg = self.src[i:j]
        self.assertIn("await self._backfill_missing_diary(", seg,
                      "warmup 成功后必须**调用**补写（只有定义不算）")
        j = self.src.index("async def _backfill_missing_diary")
        body = self.src[j:j + 2200]
        self.assertIn("wait_for", body, "补写必须有超时上界（否则挂住启动）")
        self.assertIn("_has_diary_for", body, "先查盘上有没有，避免重复生成")


class TestDiaryPathIsCurrent(unittest.TestCase):
    """🔴 P1（独立审查 2026-09-22）：`_has_diary_for` 必须走**现行路径**。

    第一版读 `<data_dir>/daily_diary.jsonl`（B-024 旧路径，内容停在 08-29）→
    判据对近期日期恒为「缺失」→ 每次启动白烧一次全量日记 LLM 调用。
    变异实测：把路径改回旧写法，整套餐件**全绿**（= 没有牙）→ 这里补上。
    """

    def test_has_diary_for_uses_current_path(self):
        src = (REPO / "main.py").read_text(encoding="utf-8")
        i = src.index("def _has_diary_for")
        j = src.index("async def _provider_exists", i)
        body = src[i:j]
        self.assertIn("read_diaries", body,
                      "必须用 summary.read_diaries（双路径 + 同日去重），不要自己拼路径")
        self.assertNotIn('"daily_diary.jsonl"', body, "不得再出现裸的旧路径文件名")

    def test_rotate_retries_when_diary_missing(self):
        """P2：轮转里没产出要**本进程内退避重试**，不能只靠重启补写。"""
        src = (REPO / "main.py").read_text(encoding="utf-8")
        i = src.index("async def _rotate_followup")
        j = src.index("async def _retry_diary_later", i)
        body = src[i:j]
        self.assertIn("_has_diary_for", body)
        self.assertIn("_retry_diary_later", body)


class TestDiaryDayIsPassedIn(unittest.TestCase):
    """🔴 P2（独立审查第 2 轮）：**日必须由调用方传入**。

    `_generate_diary` 原先自己现算 `day_key(now-86400)`，而重试/补写可能跨过 02:00 边界 →
    把「09-20 的归档」写成 day=09-21 的记录：目标日永久缺失，还会顶掉 02:05 真正的日记。
    触发窄（会话陈旧 + 01:58–02:00 触发轮转 + 首发失败）但后果是**记忆内容错误**。
    """

    @classmethod
    def setUpClass(cls):
        cls.src = (REPO / "main.py").read_text(encoding="utf-8")

    def test_record_day_comes_from_parameter(self):
        i = self.src.index("async def _generate_diary")
        j = self.src.index("def ", self.src.index("record = {", i))
        body = self.src[i:i + 6000]
        self.assertIn('"day": day or self.session_mgr.day_key(', body,
                      "不许在写侧现算日；必须 `day or …` 兜底")

    def test_all_callers_pass_day(self):
        import re as _re
        calls = _re.findall(r"_generate_diary\(([^)]*)\)", self.src)
        self.assertGreaterEqual(len(calls), 3, "三处调用：轮转 / 重试 / 补写")
        real = [c for c in calls if ":" not in c and "msgs" in c]   # 定义行有类型注解，排除
        self.assertGreaterEqual(len(real), 3, "应有 轮转/重试/补写 三处调用")
        for c in real:
            args = [x.strip() for x in c.split(",") if x.strip()]
            self.assertEqual(len(args), 3, "每个调用点都要显式传日：" + c)
            # 🔴 G2d（审查第 3 轮）：只数个数钉不住"日从哪来" —— 把重试处换成
            # `self.session_mgr.day_key(time.time() - 86400.0)` 仍是 3 个参数，
            # 而那正是第 2 轮 bug 的复活形态。必须断言第三参**不是现算的**。
            self.assertNotIn("day_key(", args[2],
                             "第三个实参必须是调用方算好的日，不能在本处现算：" + c)

    def test_retry_prechecks_and_rebuilds(self):
        """M13/M16：重试里必须先查盘（多实例）并**在成功后重算 §3**。"""
        i = self.src.index("async def _retry_diary_later")
        j = self.src.index("def _digest_fresh_today", i)
        body = self.src[i:j]
        # ⚠️ 只断言那行 `if` 没牙：函数里还有第二处同名检查（生成后复查）→ 变异实测 GREEN。
        # 这里连"前置检查的语义标记"一起钉住，拆掉整块才会红。
        self.assertIn("已被别的路径补上", body, "M13：多实例前置检查（领先于首次生成）")
        self.assertLess(body.index("已被别的路径补上"), body.index("self._generate_diary"),
                        "前置检查必须在**第一次生成之前")
        self.assertIn("_rebuild_memory_digest", body, "M16：成功后重算 §3")

    def test_freshness_default_uses_cst(self):
        """M15：默认 `now` 必须走显式 +8（否则测试都传 now= 就钉不住）。"""
        src = (REPO / "services" / "summary.py").read_text(encoding="utf-8")
        i = src.index("def digest_fresh_today")
        body = src[i:i + 2000]
        self.assertIn("datetime.now(_CST)", body, "默认参考时刻必须是 CST")


class TestSelfcheckStalenessUsesCST(unittest.TestCase):
    """🔴 2026-09-23 观察抓到误报：自检原先比 **UTC 日期**，而轮转在 02:05 CST
    = 前一天的 18:05 UTC → 每天 00:00-08:00 CST 都误报「§3 是旧的」。

    变异实测：改回 `time.gmtime()` 时整套餐件**全绿**（无闸）→ 这里补上。
    """

    def test_selfcheck_compares_in_cst(self):
        src = (REPO / "main.py").read_text(encoding="utf-8")
        i = src.index("§3 是**旧的**")
        seg = src[max(0, i - 900):i + 200]
        self.assertIn("datetime.now(_cst)", seg, "必须显式 +8")
        # ⚠️ 只钉"今天用 CST"不够 —— 第一版就是这么修的，左边仍拿 `_gen[:10]` 当日期 →
        # 照旧误报（我同一处错了两次）。必须钉住**两边都换算**。
        self.assertIn("astimezone(_cst)", seg, "生成时刻也必须换算到 CST 再比")
        self.assertNotIn("_gen[:10] < _today_cst", seg, "不得再拿 UTC 裸串当日期")
        self.assertNotIn("time.gmtime()", seg, "不得再比 UTC 日期")

    def test_digest_fresh_accepts_session_day(self):
        """P3：判据要能吃**会话日**（否则 00:00-02:05 每天两小时仍重建）。"""
        src = (REPO / "services" / "summary.py").read_text(encoding="utf-8")
        self.assertIn("day=None", src)
        self.assertIn("str(day).strip() if day else", src)
        m = (REPO / "main.py").read_text(encoding="utf-8")
        self.assertIn("day=day or None", m, "调用点必须把会话日传下去")
