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
# A7④ 安全阀化：同时判 conflict（是否正在冲突/口角——冲突中机器人绝不发言，
# 避免煽风点火）。单 prompt 双任务：reply（参与度）+ conflict（安全闸）。
GATE_SYSTEM_PROMPT = (
    "你是一个 QQ 群聊参与度与安全分析师。你的任务（两个独立判断）：\n"
    "A) 在当前对话语境下，机器人是否应该接这句话；\n"
    "B) 当前对话是否正在发生冲突（严重口角、人身攻击、侮辱、群体对立，"
    "不含正常争论/玩笑）。\n"
    "判断依据（按权重）：\n"
    "1. 这句话是否在向机器人提问、点名、寻求回应；\n"
    "2. 话题是否新鲜、有可接的空间（不是纯寒暄/已结束话题）；\n"
    "3. 是否有情绪需求（求安慰/分享欲）值得回应；\n"
    "4. 参考风格片段：机器人（模仿某群友风格）能不能自然接上这句话；\n"
    "5. 不要因为消息多就想回，克制优先：拿不准就选择不回。\n"
    "冲突规则（安全优先）：\n"
    "- 若正在发生冲突（B 为 true），机器人绝不发言（reply 强制 false）——"
    "避免煽风点火；\n"
    "- 冲突判定从严：疑似冲突即 true。\n"
    "只输出一个 JSON 对象，不要输出其他内容：\n"
    '{"reply": true或false, "conflict": true或false, "reason": "一句话理由（不超过20字）"}'
)


@dataclass
class GateDecision:
    reply: bool            # True=放行 RP 生成；False=静默
    reason: str            # 理由（LLM 给的，或 fallback 说明）
    conflict: bool = False # A7④ 安全阀：True=正在冲突，强制不发言（勿煽风点火）
    fallback: bool = False  # True=本次是降级结果（超时/解析失败等）
    cached: bool = False    # True=命中同群节流窗口缓存
    ts: float = 0.0

    def to_log(self, group_id: str, sender_uin: str = "") -> dict:
        return {
            "reply": self.reply,
            "conflict": self.conflict,
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
        now_utc_fn: Optional[Callable[[], float]] = None,
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
        # A7④: 时钟注入（离线重放按场景时刻推进节流窗口；缺省真实时钟）
        self._now = now_utc_fn or time.time

    # ---- prompt building ----

    def _build_prompt(
        self,
        recent_msgs: list[dict],
        current_speaker: str,
        current_text: str,
        rag_hits: Optional[list[dict]] = None,
        is_at: bool = False,
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
        # A7④: @ 提示——被 @ 通常应回（reply 倾向 true），但冲突时仍不发言。
        if is_at:
            prompt += "\n（本条消息 @ 了机器人：通常应当回复；但若正发生冲突，reply 仍必须为 false）"
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
        """Strict JSON parse -> GateDecision; None on any malformation.

        Accepts {reply, conflict?, reason}; conflict 缺省 false（向后兼容旧格式）。
        安全规则：conflict=true 时 reply 强制 false（冲突中绝不发言）。
        """
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
        # conflict 解析（缺省 false；容错同 reply）
        conflict = False
        conflict_raw = obj.get("conflict")
        if conflict_raw is not None:
            if isinstance(conflict_raw, bool):
                conflict = conflict_raw
            elif isinstance(conflict_raw, str):
                low = conflict_raw.strip().lower()
                if low in ("true", "1"):
                    conflict = True
                elif low in ("false", "0"):
                    conflict = False
            elif isinstance(conflict_raw, int) and conflict_raw in (0, 1):
                conflict = bool(conflict_raw)
        # 安全规则：冲突中强制不发言
        if conflict:
            reply = False
        reason = str(obj.get("reason", "") or "").strip()
        return GateDecision(
            reply=reply,
            conflict=conflict,
            reason=reason or ("接" if reply else "不接"),
            ts=time.time(),
        )

    # ---- public ----

    async def decide(
        self,
        group_id: str,
        recent_msgs: list[dict],
        current_speaker: str,
        current_text: str,
        rag_hits: Optional[list[dict]] = None,
        is_at: bool = False,
    ) -> GateDecision:
        """Return a decision; never raises (conservative silent on failure).

        Reuses the per-group cached decision within the cooldown window, so
        consecutive messages don't each trigger an LLM call.
        A7④: 缓存键含 is_at——@ 与非 @ 语境不同，不共享窗口结果（@ 时若命中
        非 @ 的 no-reply 缓存会误拦 @ 回复）。
        """
        import asyncio

        now = self._now()
        cache_key = f"{group_id}:{'@' if is_at else '-'}"
        with self._lock:
            hit = self._cache.get(cache_key)
            if hit and now - hit[0] < self._cooldown:
                d = hit[1]
                d.cached = True
                d.ts = now
                return d
        try:
            prompt = self._build_prompt(recent_msgs, current_speaker, current_text, rag_hits, is_at=is_at)
            raw = await asyncio.wait_for(self._llm_fn(prompt), timeout=self._timeout)
            d = self._parse((raw or "").strip())
            if d is None:
                d = GateDecision(reply=False, reason="gate parse failed", fallback=True)
        except Exception as e:
            d = GateDecision(reply=False, reason=f"gate error: {type(e).__name__}", fallback=True)
        d.ts = now
        with self._lock:
            self._cache[cache_key] = (now, d)
        return d

    def clear_cache(self, group_id: Optional[str] = None) -> None:
        """Clear cached decisions. group_id 为 None 全清；否则清该群所有
        is_at 变体（缓存键为 "<group>:<@|->"）。"""
        with self._lock:
            if group_id is None:
                self._cache.clear()
                return
            prefix = f"{group_id}:"
            for k in [k for k in self._cache if k.startswith(prefix)]:
                del self._cache[k]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cached_groups": list(self._cache.keys()),
                "cooldown_sec": self._cooldown,
                "recent_n": self._recent_n,
            }
