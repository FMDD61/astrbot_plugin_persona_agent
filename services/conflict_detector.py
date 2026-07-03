"""ConflictDetector — keyword + burst + LLM semantic verification.

Three-stage pipeline:
  1. Keyword scan: feed each message through a configurable conflict keyword list.
  2. Burst check: require >= 2 distinct speakers within 60s.
  3. LLM verification: ask a cheap model "YES/NO — is this a serious quarrel?"

Results are cached for 30-minute cooldown to avoid admin notification spam.
"""

from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path
from typing import Optional

_KW_FILE = "conflict_keywords.json"
_COOLDOWN_SEC = 1800
_BURST_WINDOW_SEC = 60
_BURST_MIN_SPEAKERS = 2
_MAX_WINDOW_MSGS = 50


class ConflictDetector:
    def __init__(self, keywords_dir: str) -> None:
        self._dir = Path(keywords_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._window: deque[tuple[float, str, str]] = deque()
        self._last_notify_ts: float = 0.0
        self._keywords_mtime: float = 0.0
        self._keywords: list[str] = []
        self._load_keywords()

    # ------------------------------------------------------------------ public

    def feed(self, ts: float, speaker: str, text: str) -> Optional[str]:
        """Ingest a message; return a context summary if stage 1+2 pass, else None."""
        self._maybe_reload()
        if not self._keywords or not text:
            return None
        lower = text.lower()
        if not any(kw in lower for kw in self._keywords):
            return None

        self._window.append((ts, speaker, text))
        while len(self._window) > _MAX_WINDOW_MSGS:
            self._window.popleft()

        if not self._check_burst(ts):
            return None

        return self._build_context(ts)

    async def verify_with_llm(self, context, conflict_ctx: str) -> bool:
        """Call LLM to semantically verify whether this is real conflict."""
        if time.time() - self._last_notify_ts < _COOLDOWN_SEC:
            return False

        try:
            provider_id = await context.get_current_chat_provider_id(None)
        except Exception:
            provider_id = None

        if not provider_id:
            return False

        try:
            resp = await context.llm_generate(
                chat_provider_id=provider_id,
                prompt=None,
                system_prompt=(
                    "你是一个群聊冲突监控助手。阅读以下群聊上下文片段，"
                    "判断是否发生了严重的口角、人身攻击或侮辱性言论。"
                    "只回答 YES 或 NO，不要解释。"
                ),
                contexts=[
                    {"role": "user", "content": conflict_ctx},
                ],
            )
        except Exception:
            return False

        text = (getattr(resp, "completion_text", "") or "").strip().upper()
        if "YES" in text:
            self._last_notify_ts = time.time()
            return True
        return False

    # ----------------------------------------------------------------- private

    def _load_keywords(self) -> None:
        path = self._dir / _KW_FILE
        if not path.exists():
            self._save_default(path)
        try:
            self._keywords_mtime = path.stat().st_mtime
            data = json.loads(path.read_text("utf-8"))
            self._keywords = [kw.strip().lower() for kw in data.get("keywords", []) if kw.strip()]
        except Exception:
            pass

    def _maybe_reload(self) -> None:
        path = self._dir / _KW_FILE
        if not path.exists():
            return
        try:
            mtime = path.stat().st_mtime
            if mtime > self._keywords_mtime:
                self._load_keywords()
        except Exception:
            pass

    @staticmethod
    def _save_default(path: Path) -> None:
        default = {
            "_note": "人工编辑版。小写关键词，一行一个。mtime 变化自动热重载。",
            "keywords": [
                "你他妈", "你妈的", "cnm", "操你", "傻逼", "sb", "沙比",
                "弱智", "脑残", "nt", "废物", "垃圾人", "畜牲", "畜生",
                "恶心", "滚", "去死", "死妈", "你算什么东西",
                "你配吗", "闭嘴", "别逼逼",
            ],
        }
        path.write_text(
            json.dumps(default, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _check_burst(self, now_ts: float) -> bool:
        speakers: set[str] = set()
        cutoff = now_ts - _BURST_WINDOW_SEC
        for t, speaker, _ in self._window:
            if t >= cutoff:
                speakers.add(speaker)
        return len(speakers) >= _BURST_MIN_SPEAKERS

    def _build_context(self, now_ts: float) -> str:
        cutoff = now_ts - 120
        lines: list[str] = []
        for t, speaker, text in self._window:
            if t >= cutoff:
                lines.append(f"{speaker}: {text}")
        return "\n".join(lines)
