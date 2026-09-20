"""text_style — pure text-formatting helpers extracted from main.py (G4 unit-testable).

No astrbot imports: safe to import in tests and offline tools.
"""
from __future__ import annotations

import re
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


KOUPI_LIST = ("口癖己", "口癖庚", "口癖丁", "口癖丙", "口癖甲", "口癖戊", "口癖乙")
KOUPI_MAX_TOTAL = 2

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


def cap_koupi(text: str) -> str:
    occurrences: list[tuple[int, int]] = []
    for phrase in KOUPI_LIST:
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


def strip_emoji(text: str) -> str:
    return RE_EMOJI.sub("", text)


def strip_at_mentions(text: str) -> str:
    return RE_AT_USER.sub("", text)


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
    emoji strip, newline collapse, length caps)."""
    if not text:
        return ""
    out = text.strip()
    for b in _AI_PHRASES:
        out = out.replace(b, "")
    out = strip_at_mentions(out)
    out = strip_meta_parens(out)
    out = RE_REPLY_MARKER.sub("", out)
    out = RE_ASTRBOT_MARKER.sub("", out)
    out = RE_TOOL_INTENT_MARK.sub("", out)
    out = re.sub(r"(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])", "", out)
    out = cap_koupi(out)
    out = strip_emoji(out)
    out = collapse_newlines(out)
    if len(out) > 400:
        out = out[:400].rstrip()
    return out