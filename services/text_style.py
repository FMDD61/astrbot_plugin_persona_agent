"""text_style — pure text-formatting helpers extracted from main.py (G4 unit-testable).

No astrbot imports: safe to import in tests and offline tools.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

RE_QUOTE_BLOCK = re.compile(r"\[引用消息\(.+?\)\]", re.DOTALL)
RE_AT_MARKER = re.compile(r"\[At:\d+\]")
RE_EMOJI = re.compile(
    "["
    "\U0001F300-\U0001F9FF"   # Misc Symbols, Pictographs, Emoticons, Supplemental
    "\U0001FA00-\U0001FAFF"   # Symbols and Pictographs Extended-A
    "\U00002600-\U000027BF"   # Misc Symbols + Dingbats
    "\U0000FE0F\U0000200D"    # Variation Selector + ZWJ
    "\U0001F1E0-\U0001F1FF"   # Regional Indicator Symbols
    "\U00002B50\U00002764"    # ⭐ ❤
    "]"
)
RE_AT_USER = re.compile(r"(?<!\w)@\S+")
RE_PAREN_META = re.compile(
    r"[（(]\s*"
    r"(?:\d{5,}"                   # QQ number (5+ digits)
    r"|day\s*\d+"                 # day counter
    r"|第?\d+\s*天"              # 第N天 / N天
    r"|群地位[↑↓]+"              # status tracker
    r"|\d+/\d+"                   # fraction
    r"|\b\d{2,4}\b"              # standalone number 2-4 digits
    r")"
    r"\s*[）)]"
)
RE_REPLY_MARKER = re.compile(r"\[(?:回复|r:)[^\]]*\]|\[r\]")
# AstrBot message_str renders media as [图片: <file>] / [ComponentType.X] etc.
RE_ASTRBOT_MARKER = re.compile(r"\[(?:图片|表情|ComponentType\.[A-Za-z]+)[^\]]*\]")
# 引用标记：**两种形态**
#   * `[r:-N]` —— 旧"编号制"（模型自己数第几条）；
#   * `[r]`    —— 新"打标制"（C17/D37/D40/D43）：模型只说"要引"，
#                 引用对象 = 硬闸放行的那条（PHI 的【现在要回应的】行），
#                 id 由代码给出 —— 模型不再数数，B-001 那一族缺陷从根上消失。
# 保留旧形态：人格文案可用 `persona.sections_mode=legacy` 整体回退，
# 旧文案教的是 `[r:-N]`，两条路都要能走。
RE_QUOTE_MARK = re.compile(r"^\s*\[r(?::\s*(-?\d+))?\]\s*")
# 工具意图标记（spec §4.3）。**先有剥离规则，才敢把协议教给模型** —— 否则
# 模型学会 [emote:...] 的那一天，标记会被原样发进群里。S3 实现执行器前
# 这里就先兜住（当前提示词还没教，属预防性收口）。
RE_TOOL_INTENT_MARK = re.compile(r"\[(?:emote|poke)\s*:[^\]]*\]")
# 工具意图的**抓取**正则（S3）：与上面的剥离正则共用同一语法
RE_EMOTE_MARK = re.compile(r"\[\s*emote\s*:\s*([^\]]*?)\s*\]")
RE_POKE_MARK = re.compile(r"\[\s*poke\s*:\s*([^\]]*?)\s*\]")
# 识图注入块：`（配图：<描述>）` / `（配图：1:…；2:…）`（main._augment_with_vision 产出）
RE_IMAGE_DESC_BLOCK = re.compile(r"（配图：([^）]*)）")
# 表情（QQ Face）注入块：`（表情：呲牙）`
RE_FACE_BLOCK = re.compile(r"（表情：([^）]*)）")


def split_media_annotations(text: str) -> tuple[str, list[str], list[str]]:
    """把识图/表情注入块从消息正文里**拆出来**（S2 输入打包重划）。

    背景：识图描述此前被直接拼进用户消息文本
    （`"你好！ （配图：一只猫）"`），于是它同时是"用户说的话"和
    "系统给的信息"——RP 模型分不清，KG 也照抽不误（B-014）。

    现在拆开：正文归正文，图片/表情描述作为**独立的结构化块**注入，
    语义上等价于「模型自己看到了这张图」（dsh read_image 的直投性质），
    而不是别人转述的一句话。

    返回 ``(正文, 图片描述列表, 表情列表)``。描述里的 `1:`/`2:` 前缀会被保留
    （多图时用于区分），空描述（`无法识别`）**不丢弃** —— 它是有意义的信号，
    说明"这里确实有张图但看不清"，RP 不该脑补。
    """
    if not text:
        return text, [], []
    imgs: list[str] = []
    faces: list[str] = []
    for m in RE_IMAGE_DESC_BLOCK.finditer(text):
        d = (m.group(1) or "").strip()
        imgs.append(d)
    for m in RE_FACE_BLOCK.finditer(text):
        d = (m.group(1) or "").strip()
        faces.append(d)
    body = RE_IMAGE_DESC_BLOCK.sub("", text)
    body = RE_FACE_BLOCK.sub("", body)
    # 拆完会留下多余空格（如 "你好！ "），规整一下
    body = re.sub(r"[ \t]{2,}", " ", body).strip()
    return body, imgs, faces


# ---------------------------------------------------------------------------
# 口癖封顶（C8）—— **名单是数据，不是框架**（2026-09-23 解耦）
#
# 原先这里是**一行硬编码**的名单常量。那几个词源自**风格源真实的语言习惯**，
# 属真实数据特征 —— 仓库是 public，不能留（用户 2026-09-23 口径：「口癖常量解耦，
# 将具体口癖外挂到 data_out/ 下，加载时插件从外部引入数据。若外部无文件，
# 降级到无口癖功能」）。现在整批移到仓库外 `data_out/koupi.json`（规范副本），
# 插件运行时从 `<data_dir>/koupi.json` 加载 —— **代码里一个真实口癖字面量都不留**
# （含注释与测试夹具；单测用假口癖验证机制）。
#
# 🔴 **降级必须可见**（本项目第一病根）：外部没有文件 → 口癖功能降级为
# 「不裁剪」（`cap_koupi()` 直通）。这条降级路径有三个留痕点，**绝不静默**：
#   * `main.initialize()` 打一行 WARNING（启动一次）；
#   * `koupi_manifest()` 把 `koupi_source` 记进启动日志；
#   * `pipeline` 每轮把 `koupi_degraded` 写进 trace。
# ---------------------------------------------------------------------------

#: 口癖名单文件名（`<data_dir>/koupi.json`）。插件**只认 `<data_dir>`** ——
#: 不硬编码 `data_out/`（那是仓库外的迁移/备份规范位置，见 workspace AGENTS.md）。
KOUPI_FILE = "koupi.json"

#: 单条回复里保留的口癖条数上限（**框架常量**：与"用哪些词"无关，不进数据文件）
KOUPI_MAX_TOTAL = 2


@dataclass
class KoupiState:
    """口癖名单的加载结果 + 留痕（降级必须可见）。"""

    #: 当前生效的口癖（外部文件里的原样顺序）
    phrases: tuple = ()
    #: "file"（数据目录文件可用）/ "missing"（**没有外部文件** → 降级不裁剪）
    #: / "broken"（文件在但用不了：坏 JSON / 结构不对 / 空名单 → 同样降级）
    source: str = "missing"
    #: 实际生效的文件（降级时为空串）—— 日志里点名用
    path: str = ""
    #: 文件指纹（`st_mtime_ns` + `st_size`）：热重载判定用
    mtime_ns: int = 0
    size: int = 0
    #: "文件在但用不了"的原因（与"压根没文件"可区分，N6 同族纪律）
    error: str = ""

    @property
    def degraded(self) -> bool:
        """是否处于降级态（口癖功能不可用 → `cap_koupi` 直通）。"""
        return self.source != "file"

    def manifest(self) -> dict:
        """留痕用的扁平字典（启动日志与 pipeline trace **共用同一份口径**）。"""
        return {
            "koupi_source": self.source,
            "koupi_phrases": len(self.phrases),
            "koupi_path": self.path,
            "koupi_error": self.error,
        }


def parse_koupi_payload(data) -> tuple[list[str], str]:
    """解析 koupi.json，返回 ``(phrases, error)``。接受两种形态：

    * ``{"phrases": [...]}`` —— `data_out/koupi.json` 的交付形态（自带说明字段）；
    * 裸数组 —— 只想要一份名单时的最简写法。

    非法条目（非字符串 / 空串）**跳过并记账**：一个坏条目不该让整份名单失效
    —— 那会把"少一个词"放大成"没有口癖"（降级面被无谓放大）。
    """
    raw = data.get("phrases") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return [], '结构不对：期望 {"phrases": [...]} 或裸数组'
    out: list[str] = []
    bad = 0
    for item in raw:
        text = item.strip() if isinstance(item, str) else ""
        if text:
            out.append(text)
        else:
            bad += 1
    return out, (f"{bad} 个条目非法（已跳过）" if bad else "")


_koupi_lock = threading.Lock()
_koupi_path: Optional[Path] = None
_koupi_state = KoupiState()


def configure_koupi(data_dir) -> KoupiState:
    """绑定 `<data_dir>/koupi.json` 并**立即加载一次**（插件启动时调用）。

    宿主必须显式调用：路径只从 `<data_dir>` 来 —— 插件不得硬编码 `data_out/`。
    返回加载结果，调用方据此打「降级必须可见」的那一行日志。
    """
    global _koupi_path
    _koupi_path = Path(data_dir) / KOUPI_FILE
    return koupi_state(force=True)


def reset_koupi() -> None:
    """回到「未配置」态（单测 / 离线工具用；避免全局状态跨用例泄漏）。"""
    global _koupi_path, _koupi_state
    with _koupi_lock:
        _koupi_path = None
        _koupi_state = KoupiState()


def koupi_state(force: bool = False) -> KoupiState:
    """当前生效的口癖名单 + 留痕（**mtime 热重载**，与项目人工 JSON 约定一致）。

    * 指纹 = ``(st_mtime_ns, st_size)``。加 size 是 N-1 那条教训的补救：文件 mtime
      走内核**粗时钟**，同一刻度内的改写 `st_mtime_ns` 完全相同 → 只看 mtime 会返回
      **旧名单**（改了不生效，且不报错）。size 能抓住"同一刻度内长度变了"这一大类；
      长度也没变的同刻度改写仍看不见，故**不宣称完备**（要绝对新鲜就改完 `touch` 一下）。
    * 文件消失 → **立刻降级为 missing，不保留内存里的旧名单**：「数据没了」必须当场
      可见，而不是继续拿旧名单装作一切正常（陈旧比空更糟）。
    """
    global _koupi_state
    with _koupi_lock:
        path = _koupi_path
        if path is None:
            # 宿主没配置数据目录（离线工具 / 单测）—— 与"文件不存在"同一种形态：
            # 没有外部数据 → 口癖功能不可用，且这个事实写在 state 里（不静默）。
            _koupi_state = KoupiState(
                source="missing", error="未配置数据目录（configure_koupi 未调用）")
            return _koupi_state
        try:
            st = path.stat()
        except FileNotFoundError:
            _koupi_state = KoupiState(source="missing")
            return _koupi_state
        except OSError as e:                      # 权限 / 目录等真错误：也说清楚
            _koupi_state = KoupiState(
                source="missing", error=f"{type(e).__name__}: {e}")
            return _koupi_state
        fingerprint = (st.st_mtime_ns, st.st_size)
        if not force and _koupi_state.source == "file"                 and (_koupi_state.mtime_ns, _koupi_state.size) == fingerprint:
            return _koupi_state
        try:
            data = json.loads(path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError) as e:
            _koupi_state = KoupiState(
                source="broken", path=str(path), mtime_ns=st.st_mtime_ns,
                size=st.st_size, error=f"{type(e).__name__}: {e}")
            return _koupi_state
        phrases, perr = parse_koupi_payload(data)
        if not phrases:
            _koupi_state = KoupiState(
                source="broken", path=str(path), mtime_ns=st.st_mtime_ns,
                size=st.st_size, error=perr or "名单为空（没有可用条目）")
            return _koupi_state
        _koupi_state = KoupiState(
            phrases=tuple(phrases), source="file", path=str(path),
            mtime_ns=st.st_mtime_ns, size=st.st_size, error=perr)
        return _koupi_state


def koupi_phrases() -> tuple:
    """当前生效的口癖名单（热重载；**降级时为空元组**）。"""
    return koupi_state().phrases


def koupi_manifest() -> dict:
    """口癖加载留痕（启动日志 / pipeline trace 共用口径）。"""
    return koupi_state().manifest()

_AI_PHRASES = (
    "作为一个AI", "作为AI", "作为一名AI", "作为人工智能", "我是AI", "我是一个AI",
    "作为助手", "作为大模型", "作为语言模型", "我是一个大模型", "我是语言模型",
)


def clean_message_text(text: str) -> str:
    """Strip AstrBot/OneBot render markers from raw message text:
    [引用消息(...)], [At:QQ], [图片: file], [ComponentType.X], [表情...]."""
    t = RE_QUOTE_BLOCK.sub("", text)
    t = RE_AT_MARKER.sub("", t)
    t = RE_ASTRBOT_MARKER.sub("", t)
    return t.strip()


MAX_EMOTE_INTENT_CHARS = 40


def extract_tool_intents(text: str) -> tuple[str, Optional[str], Optional[str]]:
    """提取并剥离工具意图标记（S3，spec §4.3）。

    返回 ``(正文, emote 意图短语, poke QQ 号)``。**无论是否解析成功，标记一律剥离**
    —— 泄漏到群里就是乱码（B-012 的教训）。

    - ``[emote:无奈地摇头]`` → 正文去掉标记，emote="无奈地摇头"
    - ``[poke:100000002]``   → 正文去掉标记，poke="100000002"
    - 多个同类标记：取**第一个**（一条回复最多一个动作，多余的直接丢弃）
    - 空内容/超长/非法 QQ 号 → 视为无效，返回 None（但仍剥离标记）

    位置约定（写进提示词的规则）：``[emote:]`` 期望在行尾、``[poke:]`` 任意位置；
    但**解析不依赖位置** —— 模型不守规矩时也不该把标记漏进群。
    """
    if not text:
        return text, None, None
    emote: Optional[str] = None
    poke: Optional[str] = None
    m = RE_EMOTE_MARK.search(text)
    if m:
        cand = (m.group(1) or "").strip()
        if cand and len(cand) <= MAX_EMOTE_INTENT_CHARS:
            emote = cand
    m = RE_POKE_MARK.search(text)
    if m:
        cand = (m.group(1) or "").strip()
        # QQ 号：5–12 位纯数字（容忍模型写成 @123 或 "123 "）
        cand = cand.lstrip("@").strip()
        if cand.isdigit() and 5 <= len(cand) <= 12:
            poke = cand
    body = RE_TOOL_INTENT_MARK.sub("", text)
    body = re.sub(r"[ \t]{2,}", " ", body).strip()
    return body, emote, poke


def extract_quote(text: str, meta: Optional[dict] = None) -> tuple[str, Optional[int]]:
    """Extract a leading `[r]` / `[r:-N]` quote marker from the raw LLM output.

    Returns ``(clean_text, n_or_None)``. ``n=1`` means the newest buffered
    message（旧"编号制"）。

    **打标制（C17/D37）**：模型只写 ``[r]``（不带 N）时 ``n`` 返回 ``None``，
    调用方用"硬闸放行的那条消息 id"直接引用。两种形态靠可选 ``meta`` 区分；
    **不改返回类型** —— 该签名被 pipeline / main / 测试多处依赖
    （code-quality：跨轮携带信息用可选字典参数）。

    ``meta`` 键：``marker``（命中的原文，未命中为 None）、``bare``（是否裸 ``[r]``）。
    """
    if meta is not None:
        meta["marker"] = None
        meta["bare"] = False
    if not text:
        return text, None
    m = RE_QUOTE_MARK.match(text)
    if not m:
        return text, None
    rest = text[m.end():].lstrip("\n ")
    if meta is not None:
        meta["marker"] = m.group(0).strip()
    if m.group(1) is None:
        # 裸 [r]：打标制。标记**必须**剥离（泄漏到群里就是乱码）。
        if meta is not None:
            meta["bare"] = True
        return rest, None
    n = abs(int(m.group(1)))  # [r:-1] == 倒数第 1 条 == index 1
    if n <= 0:
        return text, None
    return rest, n


def cap_koupi(text: str, phrases: Optional[tuple] = None) -> str:
    """把口癖总量封顶到 ``KOUPI_MAX_TOTAL``（超出的**删掉**，保留靠前的）。

    🔴 **外部无名单 → 直通**：``phrases`` 为空即**原样返回**，不抛异常、不改一个字符
    —— 口癖功能整体降级为「不裁剪」。降级本身**不在这里报**（这里每轮都跑，报了就是
    刷屏）：留痕在 ``koupi_state()`` / ``koupi_manifest()`` —— 宿主启动打 WARNING，
    pipeline 每轮把 ``koupi_source`` 写进 trace。**可见，但不恒亮**。

    ``phrases`` 显式传入 = 用给定名单（单测 / 离线工具），不走全局加载器。
    """
    if not text:
        return text
    if phrases is None:
        phrases = koupi_phrases()
    if not phrases:
        return text
    occurrences: list[tuple[int, int]] = []
    for phrase in phrases:
        idx = 0
        while True:
            pos = text.find(phrase, idx)
            if pos == -1:
                break
            occurrences.append((pos, len(phrase)))
            idx = pos + len(phrase)
    if len(occurrences) <= KOUPI_MAX_TOTAL:
        return text
    occurrences.sort(key=lambda x: x[0])
    parts: list[str] = []
    prev_end = 0
    for i, (pos, length) in enumerate(occurrences):
        parts.append(text[prev_end:pos])
        if i < KOUPI_MAX_TOTAL:
            parts.append(text[pos:pos + length])
        prev_end = pos + length
    parts.append(text[prev_end:])
    return "".join(parts)


# ⚠️ **已删除**（2026-09-23 批次三清理）：`strip_emoji()` 与 `strip_at_mentions()`。
# C15（2026-09-21）把它们从 `postprocess` 里摘掉之后，生产侧就再没有调用点，
# 只剩测试在调 —— 于是**删函数、留墓志铭**：`tests/test_text_style.py` 用 AST 断言
# `postprocess` 不再调用这两个名字、且它们在本模块里不存在。删掉的理由见
# `postprocess` 的 docstring（它们让提示词 §7【句末的表情】/ §6「@ 他一句」自上线起
# 不可能生效 → B-048）。
# ⚠️ `RE_EMOJI` / `RE_AT_USER` 这两个**正则保留**：`main.py:28-35` 在 import 期就
# `from .services.text_style import (RE_EMOJI, RE_AT_USER, ...)`（那边其实没用到它们）。
# 删正则会把 main.py 直接打炸 —— 清理由 main.py 的所有者一并做。


def strip_meta_parens(text: str) -> str:
    return RE_PAREN_META.sub("", text)


def collapse_newlines(text: str, max_lines: int = 8) -> str:
    """Join wrapped lines with punctuation-aware separators (prompt forbids
    newlines; replaced with '，' unless the previous line ends in punctuation).
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    tail_ok = "，。！？～~、…：；,.!?~"
    joined = ""
    for ln in lines:
        if joined and joined[-1] not in tail_ok:
            joined += "，"
        joined += ln
    return joined.strip()


def postprocess(text: str) -> str:
    """Full output sanitation chain (AI-phrase removal, markers, koupi cap,
    newline collapse, length caps).

    ## C15（2026-09-21）：**放开 emoji 与 @** —— 删掉了两步

    原先这里还有 `strip_at_mentions()` 与 `strip_emoji()`，两者是 S0 的
    **预防性**收口（怕模型把 AI 味的 emoji/乱 @ 发出去）。但实测后果是：
    提示词 §7【句末的表情】与 §6「@ 他一句」两处教学**自上线起不可能生效**
    （实测 bot 输出 465 条：含 emoji **0**、含 @ **0**；而风格源 2337 条里
    含 emoji 112（4.8%）、含 @ 50（2.1%））→ `BUGS.md` **B-048**。

    用户 2026-09-21：「**emoji 和 @ 都打开，我们留给 RP 更大的发挥空间**」。

    ⚠️ 放开后要观察一段时间（这两步原本防的是 emoji 滥用与误 @ 人）：
    观测口径 = 出站文本里 emoji / @ 的**出现率**（trace 的 `final_text` 可直接统计）。

    ⚠️ **观测口径的偏差（独立审查 N-22）**：本函数末尾会把文本截到 400 字 / 折到 8 行，
    所以"出现率"读到的其实是**截断后**的文本 —— 第 401 位的 emoji/@ 会被一起丢掉
    （实测 `postprocess("好" * 400 + "@某人")` 长度 400、`@某人` 没了）。
    要量"模型本来写了多少 emoji/@ "，得看截断前的原始生成文本，不能只统计 `final_text`。

    📌 2026-09-23 批次三清理：上面提到的两个函数 `strip_emoji()` / `strip_at_mentions()`
    **已从本文件删除**（C15 之后只剩测试在用）。它们**不该回来** ——
    `tests/test_text_style.py::TestStripStepsRemovedC15` 用 AST + `hasattr` 钉住了这一点。
    """
    if not text:
        return ""
    out = text.strip()
    for b in _AI_PHRASES:
        out = out.replace(b, "")
    # C15：不再 strip_at_mentions（@ 是 §6 教的表达手段之一）；该函数已于 2026-09-23 删除
    out = strip_meta_parens(out)
    out = RE_REPLY_MARKER.sub("", out)
    out = RE_ASTRBOT_MARKER.sub("", out)
    out = RE_TOOL_INTENT_MARK.sub("", out)
    out = re.sub(r"(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])", "", out)
    out = cap_koupi(out)
    # C15：不再 strip_emoji（§7【句末的表情】教的就是它）；该函数已于 2026-09-23 删除
    out = collapse_newlines(out)
    if len(out) > 400:
        out = out[:400].rstrip()
    return out