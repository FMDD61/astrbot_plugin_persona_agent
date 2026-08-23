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
RE_REPLY_MARKER = re.compile(r"\[(?:回复|r:)[^\]]*\]")
RE_QUOTE_MARK = re.compile(r"^\s*\[r:\s*(-?\d+)\]\s*")

KOUPI_LIST = ("啃啃", "搓搓", "呜嘿", "bakabaka", "钨钼钨钼", "嗷呜", "捏猫猫的")
KOUPI_MAX_TOTAL = 2

_AI_PHRASES = (
    "作为一个AI", "作为AI", "作为一名AI", "作为人工智能", "我是AI", "我是一个AI",
    "作为助手", "作为大模型", "作为语言模型", "我是一个大模型", "我是语言模型",
)


def clean_message_text(text: str) -> str:
    """Strip [引用消息(...)] and [At:QQ] blocks from raw message text."""
    t = RE_QUOTE_BLOCK.sub("", text)
    t = RE_AT_MARKER.sub("", t)
    return t.strip()


def extract_quote(text: str) -> tuple[str, Optional[int]]:
    """Extract a leading [r:-N] quote marker from the raw LLM output.

    Returns (clean_text, n_or_None). n=1 means the newest buffered message.
    """
    if not text:
        return text, None
    m = RE_QUOTE_MARK.match(text)
    if not m:
        return text, None
    n = abs(int(m.group(1)))  # [r:-1] == 倒数第 1 条 == index 1
    if n <= 0:
        return text, None
    rest = text[m.end():].lstrip("\n ")
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
    out = re.sub(r"(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])", "", out)
    out = cap_koupi(out)
    out = strip_emoji(out)
    out = collapse_newlines(out)
    if len(out) > 400:
        out = out[:400].rstrip()
    return out