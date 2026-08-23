"""examples — G14 static example-dialog injection loader.

Pure stdlib; mtime-based hot reload so A/B switching is just renaming the
file (no restart). Block is injected as one fixed system message between the
session and the KG tail (stable cache prefix; see main.py wiring).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

MAX_ENTRIES = 12
HEADER = (
    "示例对话（风格参考，禁止复读原文）：\n"
    "规则A：钨钼钨钼 仅用于肯定/恍然大悟，禁止在问好、吃饭、闲聊中滥用。\n"
    "规则B：谐音问候（枣商蚝~等）仅在对方整词说出「早上好/中午好/下午好/晚上好」时使用。"
)


@dataclass
class ExamplesState:
    mtime: float = 0.0
    block: str = ""


def load_examples_block(
    path: Path,
    max_entries: int = MAX_ENTRIES,
    prev: Optional[ExamplesState] = None,
) -> tuple[str, ExamplesState]:
    """Return (block, state). block == '' when the file is absent, empty,
    unreadable or disabled via a zero-length sentinel. `prev` carries the
    previous mtime/block for the hot-reload check."""
    path = Path(path)
    try:
        mt = path.stat().st_mtime_ns  # nanosecond: catches same-second rewrites
    except OSError:
        return "", ExamplesState()
    if prev is not None and prev.mtime > 0 and mt == prev.mtime:
        return prev.block, prev
    block = ""
    try:
        data = json.loads(path.read_text("utf-8"))
        lines = []
        for ex in (data or [])[:max_entries]:
            msgs = ex.get("messages") or []
            if len(msgs) < 2:
                continue
            topic = (ex.get("topic") or "").strip()
            parts = []
            for m in msgs:
                role = (m.get("role") or "").strip()
                content = (m.get("content") or "").strip()
                if content:
                    parts.append(f"{role}: {content}".strip())
            if len(parts) >= 2:
                lines.append((f"[{topic}] " if topic else "") + " ".join(parts))
        if lines:
            block = HEADER + "\n" + "\n".join(lines)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        block = ""
    return block, ExamplesState(mtime=mt, block=block)
