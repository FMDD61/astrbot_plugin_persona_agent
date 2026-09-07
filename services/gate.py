"""GateService — 独立"要不要回"决策层（双层 LLM 的上层闸）。

Spec: docs/specs/chatbox-rp-tool-dual-channel.md §4.1。

职责：
  - 仅在规则硬闸（interjection 的 RAG 候选触发）之后、RP 生成之前被调用，
    对"这一句值不值得接"做中性分析师判断（非角色视角）。
  - 输出结构化 {reply: yes/no, reason}，只回/不回 + 理由；
    不输出生成性文本、不带表情/动作指令（表达留给 RP 层）。
  - 同群决策节流：`decide_cooldown_sec` 窗口内只决策一次，窗口内复用结果
    （连续消息不反复调 LLM）。
  - 任何失败（超时/网络/解析/LLM 异常）→ 保守静默（reply=False, fallback=True），
    由调用方记入 trace/decision log。绝不抛出。

与 emotion.py 同构：llm_fn 由宿主注入（纯逻辑，无 astrbot import，可单测）。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

# 中性分析师系统提示：判断"这句话值不值得接"，不代入角色。
GATE_SYSTEM_PROMPT = (
    "你是一个 QQ 群聊参与度分析师。你的任务：判断在当前对话语境下，"
    "机器人是否应该接这句话。\n"
    "判断依据（按权重）：\n"
    "1. 这句话是否在向机器人提问、点名、寻求回应；\n"
    "2. 话题是否新鲜、有可接的空间（不是纯寒暄/已结束话题）；\n"
    "3. 是否有情绪需求（求安慰/分享欲）值得回应；\n"
    "4. 参考风格片段：机器人（模仿某群友风格）能不能自然接上这句话；\n"
    "5. 不要因为消息多就想回，克制优先：拿不准就选择不回。\n"
    "只输出一个 JSON 对象，不要输出其他内容：\n"
    '{"reply": true或false, "reason": "一句话理由（不超过20字）"}'
)


@dataclass
class GateDecision:
    reply: bool            # True=放行 RP 生成；False=静默
    reason: str            # 理由（LLM 给的，或 fallback 说明）
    fallback: bool = False  # True=本次是降级结果（超时/解析失败等）
    cached: bool = False    # True=命中同群节流窗口缓存
    ts: float = 0.0

    def to_log(self, group_id: str, sender_uin: str = "") -> dict:
        return {
            "reply": self.reply,
            "reason": self.reason,
            "fallback": self.fallback,
            "cached": self.cached,
            "group_id": str(group_id or ""),
            "sender_uin": str(sender_uin or ""),
        }


class GateService:
    def __init__(
        self,
        llm_fn: Callable[[str], Awaitable[str]],
        *,
        timeout: float = 3.0,
        decide_cooldown_sec: float = 8.0,
        recent_n: int = 15,
        max_rag_hits: int = 3,
        system_prompt: str = GATE_SYSTEM_PROMPT,
    ) -> None:
        self._llm_fn = llm_fn
        self._timeout = float(timeout)
        self._cooldown = float(decide_cooldown_sec)
        self._recent_n = int(recent_n)
        self._max_rag_hits = int(max_rag_hits)
        self._system_prompt = system_prompt
        self._lock = threading.Lock()
        # per-group decision cache for cooldown-window reuse
        self._cache: dict[str, tuple[float, GateDecision]] = {}

    # ---- prompt building ----

    def _build_prompt(
        self,
        recent_msgs: list[dict],
        current_speaker: str,
        current_text: str,
        rag_hits: Optional[list[dict]] = None,
    ) -> str:
        lines: list[str] = []
        for m in recent_msgs[-self._recent_n:]:
            role = m.get("role", "")
            name = m.get("name", "") or ("机器人" if role == "assistant" else "群友")
            content = (m.get("content", "") or "").strip()
            if not content:
                continue
            lines.append(f"{name}: {content}")
        prompt = "最近对话：\n" + ("\n".join(lines) if lines else "（空）")
        prompt += f"\n\n当前说话人：{current_speaker}，本条消息：{current_text}"
        if rag_hits:
            hit_lines = []
            for h in rag_hits[: self._max_rag_hits]:
                # RagService.query returns {"id","document","metadata","score"}
                # — read document first, tolerate text/content aliases.
                txt = (
                    (h.get("document") or h.get("text") or h.get("content") or "")
                ).strip()
                score = h.get("score", "")
                if txt:
                    if isinstance(score, float):
                        hit_lines.append(f"- [{score:.2f}] {txt[:120]}")
                    else:
                        hit_lines.append(f"- {txt[:120]}")
            if hit_lines:
                prompt += "\n\n风格参考片段（机器人风格源的相似历史发言）：\n" + "\n".join(hit_lines)
        prompt += "\n\n请判断机器人是否应该接这句话，只输出 JSON。"
        return prompt

    # ---- parsing ----

    @staticmethod
    def _parse(text: str) -> Optional[GateDecision]:
        """Strict JSON parse -> GateDecision; None on any malformation."""
        try:
            obj = json.loads(text)
        except Exception:
            return None
        if not isinstance(obj, dict):
            return None
        reply_raw = obj.get("reply")
        if reply_raw is None:
            return None
        reply = bool(reply_raw) if isinstance(reply_raw, bool) else None
        if reply is None:
            # tolerate "true"/"false" strings / 0/1
            if isinstance(reply_raw, str):
                low = reply_raw.strip().lower()
                reply = True if low == "true" else (False if low == "false" else None)
            elif isinstance(reply_raw, int) and reply_raw in (0, 1):
                reply = bool(reply_raw)
            if reply is None:
                return None
        reason = str(obj.get("reason", "") or "").strip()
        return GateDecision(reply=reply, reason=reason or ("接" if reply else "不接"), ts=time.time())

    # ---- public ----

    async def decide(
        self,
        group_id: str,
        recent_msgs: list[dict],
        current_speaker: str,
        current_text: str,
        rag_hits: Optional[list[dict]] = None,
    ) -> GateDecision:
        """Return a decision; never raises (conservative silent on failure).

        Reuses the per-group cached decision within the cooldown window, so
        consecutive messages don't each trigger an LLM call.
        """
        import asyncio

        now = time.time()
        with self._lock:
            hit = self._cache.get(str(group_id))
            if hit and now - hit[0] < self._cooldown:
                d = hit[1]
                d.cached = True
                d.ts = now
                return d
        try:
            prompt = self._build_prompt(recent_msgs, current_speaker, current_text, rag_hits)
            raw = await asyncio.wait_for(self._llm_fn(prompt), timeout=self._timeout)
            d = self._parse((raw or "").strip())
            if d is None:
                d = GateDecision(reply=False, reason="gate parse failed", fallback=True)
        except Exception as e:
            d = GateDecision(reply=False, reason=f"gate error: {type(e).__name__}", fallback=True)
        d.ts = now
        with self._lock:
            self._cache[str(group_id)] = (now, d)
        return d

    def clear_cache(self, group_id: Optional[str] = None) -> None:
        with self._lock:
            if group_id is None:
                self._cache.clear()
            else:
                self._cache.pop(str(group_id), None)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cached_groups": list(self._cache.keys()),
                "cooldown_sec": self._cooldown,
                "recent_n": self._recent_n,
            }
