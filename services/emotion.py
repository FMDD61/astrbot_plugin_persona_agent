"""EmotionProvider — 情绪分 v2（C24：**纯代码计算，无 LLM 调用**）。

设计依据（仓库外）：`docs/specs/emotion_v2.md` —— §6 定案 / §7 两条算术 /
§8 mood 映射表 / §9 sticker 废弃；缺陷 `BUGS.md` B-053。

机制（§6 定案）
--------------
    score = 1.0 起
    Gate 每判一次 blocked   →  score -= blocked_penalty        （默认 0.1）
    随时间自然恢复          →  score += recovery_per_min / 分钟 （默认 0.1）
    范围                    →  [min_score, max_score] = [0.0, 1.0]
    硬闸乘子                →  multiplier = 0.6 + 0.4 × score

量纲（**别搞混**）
----------------
`score` 与 `multiplier` 不是一个量：

    score       ∈ [0, 1]       "我最近被拦了几次"的投影（本模块的状态）
    multiplier  ∈ [0.6, 1.0]   进硬闸乘子位
    interjection.decide() 判据：effective = top_rag_score × emotion_multiplier ≥ 0.6

旧版的 `global_willingness` 直接就是乘子位（`raw × w`），故本版定义：

    EmotionState.global_willingness := multiplier()      # **不是** score

这样 pipeline 现有接线 `emotion_multiplier=emotion_state.global_willingness`
**不改就是对的**；**绝不要**把 score 塞进那一格（score=0 时 raw × 0 = 0，通道全关，
与"乘子 0.6"是完全不同的语义）。

🔴 悬崖（§7.2 —— 不写清楚就是静默失效）
------------------------------------
**emotion_score < 0.40 ⇒ 乘子 < 0.76 ⇒ 在 raw 上限 ≈0.79（B-044）下 RAG 通道数学上不可能触发**
（精确临界：mult ≥ 0.60/0.79 = 0.75949 ⇒ score ≥ 0.39873；§7.2 的书面口径是 0.40，
 相差 0.3%，工程上是同一件事）。低于它的时刻，非 @ 插话**完全不可能**触发；
@ 路径不受影响 —— `interjection.decide()` 里 AT 分支在 RAG 计算之前就 return 了，
所以"吵完架连求安慰都不回"不会发生。

这是**有意行为**（吵得厉害就该闭嘴），不是 bug；因此必须三处可见：本注释、
`emotion_v2.md` §7.2、以及跨过临界时的一条 WARNING 日志（见 register_blocked/_log_recovery）。

参数与测量（未定值，先量分布）
----------------------------
所有阈值**可配置**（`_conf_schema.json` → `emotion` 段，见 `from_config`），且每次
分数变化落日志。用户口径（§7.1）：按 −0.1/+0.1 每分钟这组默认值，分数会**长期钉在 1.0**
（实测 RAG 命中 ≈5/小时，blocked 只是其中一小部分）→ 所以先跑几天拿到 blocked 真实频率，
再用配置定值。**改默认值前先查线上配置**（生产显式值覆盖 schema 默认）。

C25 / B-053：`sticker` 文生图通道整条废弃（用户未接 T2I 模型），本模块不再产出该字段。
"""
from __future__ import annotations

import logging
import math
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .kg_provider import KGContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常数（§6 定案 + 实测口径）。乘子 0.6/0.4 **不做成配置**：一改，§7.2 的悬崖说明就得重算。
# ---------------------------------------------------------------------------
MULTIPLIER_BASE = 0.6
MULTIPLIER_SPAN = 0.4

RAG_EFFECTIVE_THRESHOLD = 0.60   # services/interjection.py 的 rag_score_threshold（生产值）
RAG_RAW_SCORE_MAX = 0.79         # B-044：实测原始相似度上限
RAG_CLIFF_SCORE = (RAG_EFFECTIVE_THRESHOLD / RAG_RAW_SCORE_MAX - MULTIPLIER_BASE) / MULTIPLIER_SPAN
# ↑ = 0.39873…：低于它，0.79 × 乘子 < 0.60，RAG 通道数学上不可能触发（§7.2 写作 0.40）

# §8 映射表：阈值（含）→ 心情词。**只用情绪词，不用意愿词**
# （意愿词会让 RP"演一个不想说话的人"，用户早先明确反对把闸门参数投影到表演层）。
MOOD_TABLE: "tuple[tuple[float, str], ...]" = (
    (1.00, "轻快"),
    (0.80, "平常"),
    (0.60, "有点蔫"),
    (0.40, "提不起劲"),
    (0.20, "低落"),
    (0.00, "沉沉的"),
)

_EPS = 1e-9
# 一次性告警开关（测试钩子：清空该 dict 可重放）。避免每轮 query 都刷同一条日志。
_WARNED_ONCE: "dict[str, bool]" = {}


def multiplier_for_score(score: float) -> float:
    """硬闸乘子 = 0.6 + 0.4 × score（score 先钳到 [0,1]）。

    🔴 emotion_score < 0.40 ⇒ 乘子 < 0.76 ⇒ 在 raw 上限 ≈0.79 下 RAG 通道数学上不可能触发。
    """
    s = min(1.0, max(0.0, float(score)))
    return MULTIPLIER_BASE + MULTIPLIER_SPAN * s


def mood_for_score(score: float) -> str:
    """§8 映射表直出（越界钳制；只在情绪词里取值）。"""
    s = min(1.0, max(0.0, float(score)))
    for threshold, word in MOOD_TABLE:
        if s >= threshold - _EPS:
            return word
    return MOOD_TABLE[-1][1]


# ---------------------------------------------------------------------------
# 对外状态
# ---------------------------------------------------------------------------
@dataclass
class EmotionState:
    """一次情绪查询的快照。

    global_willingness: **硬闸乘子**（0.6 + 0.4×score），不是 score —— 见模块 docstring「量纲」；
                        pipeline 把它当 `emotion_multiplier` 传给 interjection.decide()。
    current_mood:       RP 注入的心情词（§8 由 score 直出）；空串 = 不注入。
    emotion_score:      v2 新增：原始分数 0~1，供 trace / 观测使用（v1 无此字段）。
    """

    global_willingness: float = 1.0
    current_mood: str = ""
    emotion_score: float = 1.0

    @staticmethod
    def neutral() -> "EmotionState":
        """中性态：乘子 1.0（不调制）、心情空串（不注入）—— 关闭/缺省/降级时的可见取值。"""
        return EmotionState()

class EmotionProvider(ABC):
    @abstractmethod
    async def query(
        self,
        group_id: str,
        recent_msgs: list[dict],
        kg_ctx: Optional["KGContext"] = None,
    ) -> EmotionState:
        ...

    # ---- v2（C24）附带接口：默认「中性空实现」 ----
    # pipeline/main 可以**无条件**调用（emotion.enabled=0 时退化成满格且完全静默），
    # 不必在每个调用点写 hasattr / try-except。
    def register_blocked(
        self, reason: str = "", *, group_id: str = "", event_key: str = "", now=None
    ) -> float:
        return 1.0

    def score(self, *, now=None) -> float:
        return 1.0

    def multiplier(self, *, now=None) -> float:
        return 1.0

    def snapshot(self, *, now=None) -> dict:
        return {
            "score": 1.0,
            "multiplier": 1.0,
            "mood": "",
            "rag_channel_open": True,
            "last_change_ts": None,
            "last_error": None,
            "stats": {},
            "recent_blocked": [],
        }


class DefaultEmotionProvider(EmotionProvider):
    """emotion.enabled=0：恒中性、零日志、零副作用（铁律：功能关闭时必须静默）。"""

    async def query(
        self,
        group_id: str,
        recent_msgs: list[dict],
        kg_ctx: Optional["KGContext"] = None,
    ) -> EmotionState:
        return EmotionState.neutral()


# ---------------------------------------------------------------------------
# v2：代码计算（C24）。无 LLM、无网络、无缓存 —— 纯算术，query 即时且可重放。
# ---------------------------------------------------------------------------
class ScoreEmotionProvider(EmotionProvider):
    """情绪分 = 代码计算（blocked 扣分 + 时间恢复），乘子 = 0.6 + 0.4 × score。"""

    # schema 键名（_conf_schema.json → emotion.*）；from_config 只认这些，其余忽略。
    CONFIG_KEYS = (
        "initial_score",
        "blocked_penalty",
        "recovery_per_min",
        "min_score",
        "max_score",
        "recovery_log_step",
    )
    DEFAULT_INITIAL_SCORE = 1.0
    DEFAULT_BLOCKED_PENALTY = 0.1
    DEFAULT_RECOVERY_PER_MIN = 0.1
    DEFAULT_MIN_SCORE = 0.0
    DEFAULT_MAX_SCORE = 1.0
    DEFAULT_RECOVERY_LOG_STEP = 0.05

    def __init__(
        self,
        *,
        initial_score: float = DEFAULT_INITIAL_SCORE,
        blocked_penalty: float = DEFAULT_BLOCKED_PENALTY,
        recovery_per_min: float = DEFAULT_RECOVERY_PER_MIN,
        min_score: float = DEFAULT_MIN_SCORE,
        max_score: float = DEFAULT_MAX_SCORE,
        recovery_log_step: float = DEFAULT_RECOVERY_LOG_STEP,
        now_utc_fn=None,
        recent_events: int = 50,
        event_key_memory: int = 256,
    ) -> None:
        for name, value in (
            ("initial_score", initial_score),
            ("blocked_penalty", blocked_penalty),
            ("recovery_per_min", recovery_per_min),
            ("min_score", min_score),
            ("max_score", max_score),
            ("recovery_log_step", recovery_log_step),
        ):
            if not math.isfinite(float(value)):
                raise ValueError(f"emotion 参数非法：{name}={value!r} 不是有限数")
        self.min_score = float(min_score)
        self.max_score = float(max_score)
        if not (0.0 <= self.min_score <= self.max_score <= 1.0):
            raise ValueError(
                f"emotion 参数非法：需 0 ≤ min_score({self.min_score}) "
                f"≤ max_score({self.max_score}) ≤ 1.0"
            )
        self.blocked_penalty = float(blocked_penalty)
        if self.blocked_penalty < 0.0:
            raise ValueError(f"emotion 参数非法：blocked_penalty={self.blocked_penalty} < 0")
        self.recovery_per_min = float(recovery_per_min)
        if self.recovery_per_min < 0.0:
            raise ValueError(f"emotion 参数非法：recovery_per_min={self.recovery_per_min} < 0")
        # 恢复是连续量：默认只在累计恢复 ≥ recovery_log_step 时落 INFO，避免逐条消息刷屏；
        # 0 = 每次 accrual 都落（排查用），<0 = 关掉周期性恢复日志（跨心情/跨悬崖仍会落）。
        self.recovery_log_step = float(recovery_log_step)

        initial = float(initial_score)
        if initial < self.min_score or initial > self.max_score:
            clamped = min(max(initial, self.min_score), self.max_score)
            logger.warning(
                "[emotion] initial_score=%.3f 超出 [min_score=%.3f, max_score=%.3f]，"
                "已钳制为 %.3f（降级必须可见）",
                initial, self.min_score, self.max_score, clamped,
            )
            initial = clamped
        self.initial_score = initial
        self._score = initial

        # A7④：时钟注入（重放按场景虚拟时间推进恢复；缺省真实时钟）
        self._now_fn = now_utc_fn or time.time
        self._last_ts = float(self._now_fn())
        self._last_change_ts: Optional[float] = None
        self._pending_recovery_log = 0.0

        # 并发安全：一个 provider 会被事件循环/多线程共享（register_blocked 可能来自
        # 不同任务的回调）→ 用锁把「结算恢复 → 扣分 → 记日志」串起来，杜绝丢更新。
        self._lock = threading.RLock()
        self.event_key_memory = max(0, int(event_key_memory))
        self._seen_keys: "OrderedDict[str, None]" = OrderedDict()
        self.recent_blocked: deque = deque(maxlen=max(1, int(recent_events)))

        # S0 观测：最近一次降级原因（None = 正常）。v2 的降级路径只剩「时钟回退」，
        # 但留痕机制保留 —— pipeline 仍会在 trace 里记 emotion_degraded。
        self.last_error: Optional[str] = None
        self.stats = {
            "blocked": 0,            # blocked 事件数（含重复）
            "penalty_applied": 0,    # 真正扣分次数（幂等去重之后）
            "duplicate_skipped": 0,
            "recovery_events": 0,
            "recovered_total": 0.0,
            "cliff_closed": 0,       # 跨过 0.40 悬崖（RAG 通道关闭）次数
            "clock_backwards": 0,
            "queries": 0,
        }
        logger.info(
            "[emotion] v2 代码计算已启用（C24/C25）：initial=%.3f penalty=%.3f/次 "
            "recovery=%.3f/分 范围=[%.3f, %.3f] 乘子=0.6+0.4×score"
            "（score < %.4f ⇒ RAG 通道关闭，只剩 @）",
            initial, self.blocked_penalty, self.recovery_per_min,
            self.min_score, self.max_score, RAG_CLIFF_SCORE,
        )

    # ---- 配置 ----
    @classmethod
    def from_config(cls, cfg: Optional[dict], *, now_utc_fn=None, **extra) -> "ScoreEmotionProvider":
        """从 `_conf_schema.json` 的 emotion 段建 provider（enabled 由调用方判断）。

        只认 `CONFIG_KEYS`；LLM 时代的键（timeout_sec/cache_ttl_sec/temperature/
        reasoning_effort）**一律忽略**（它们已无消费点）。
        """
        cfg = cfg or {}
        kw: dict = {}
        for key in cls.CONFIG_KEYS:
            if cfg.get(key) is not None:
                kw[key] = float(cfg[key])
        kw.update(extra)
        return cls(now_utc_fn=now_utc_fn, **kw)

    # ---- 纯查询 ----
    def score(self, *, now=None) -> float:
        """当前分数（惰性结算到 now）。"""
        with self._lock:
            return self._advance_locked(self._now_fn() if now is None else float(now))

    def multiplier(self, *, now=None) -> float:
        """当前硬闸乘子 = 0.6 + 0.4 × score。"""
        return multiplier_for_score(self.score(now=now))

    def snapshot(self, *, now=None) -> dict:
        """供 trace / 观测 / 排障的一次性快照。"""
        with self._lock:
            s = self._advance_locked(self._now_fn() if now is None else float(now))
            return {
                "score": s,
                "multiplier": multiplier_for_score(s),
                "mood": mood_for_score(s),
                "rag_channel_open": s >= RAG_CLIFF_SCORE,
                "last_change_ts": self._last_change_ts,
                "last_error": self.last_error,
                "stats": dict(self.stats),
                "recent_blocked": list(self.recent_blocked)[-10:],
            }

    # ---- 状态变更 ----
    def register_blocked(
        self, reason: str = "", *, group_id: str = "", event_key: str = "", now=None
    ) -> float:
        """Gate 判 blocked 时由调用方（pipeline/main）调用：**一次调用 = 一次 blocked 事件**。

        · 幂等：给了 `event_key`（建议用本轮消息 id / trace id）时同一 key 只扣一次分；
          重复调用只计数并落日志，不重复扣分。
        · 并发安全：内部 RLock 串行化「结算恢复 → 扣分 → 落日志」，多线程不丢更新。
        · **每次扣分都落 INFO 日志**（reason/group/前后分数与心情）——这是「先跑几天量
          blocked 真实频率」的测量仪器，不许静默（§7.1）。
        返回扣分后的 score。
        """
        with self._lock:
            now_ts = self._now_fn() if now is None else float(now)
            if event_key:
                if event_key in self._seen_keys:
                    self.stats["duplicate_skipped"] += 1
                    self._advance_locked(now_ts)
                    logger.info(
                        "[emotion] blocked 事件重复（event_key=%s）已忽略（幂等）：score 仍 %.3f",
                        event_key, self._score,
                    )
                    return self._score
                self._seen_keys[event_key] = None
                while len(self._seen_keys) > self.event_key_memory:
                    self._seen_keys.popitem(last=False)

            self._advance_locked(now_ts)
            before = self._score
            after = max(self.min_score, before - self.blocked_penalty)
            self._score = after
            self._last_change_ts = now_ts
            self.stats["blocked"] += 1
            self.stats["penalty_applied"] += 1
            self.recent_blocked.append({
                "ts": now_ts,
                "reason": str(reason or ""),
                "group_id": str(group_id or ""),
                "before": round(before, 6),
                "after": round(after, 6),
            })
            logger.info(
                "[emotion] blocked(−%.3f) score %.3f→%.3f 乘子 %.3f→%.3f 心情 %s→%s reason=%s group=%s",
                self.blocked_penalty, before, after,
                multiplier_for_score(before), multiplier_for_score(after),
                mood_for_score(before), mood_for_score(after),
                reason or "-", group_id or "-",
            )
            if before >= RAG_CLIFF_SCORE > after:
                self.stats["cliff_closed"] += 1
                logger.warning(
                    "[emotion] ⚠️ RAG 通道关闭：score %.3f→%.3f < 悬崖 %.4f ⇒ 乘子 %.4f < 0.76 "
                    "⇒ raw 上限 0.79 也够不到阈值 0.60 —— 只剩 @ 路径"
                    "（emotion_v2 §7.2 的有意行为，不是 bug）",
                    before, after, RAG_CLIFF_SCORE, multiplier_for_score(after),
                )
            return after

    # ---- 内部：惰性时间恢复（调用方必须持锁） ----
    def _advance_locked(self, now: float) -> float:
        elapsed = now - self._last_ts
        if elapsed < 0.0:
            # 时钟回退（NTP 校正 / 重放虚拟时钟跳变 / 系统挂起）：不计「负恢复」，
            # 但**必须留痕** —— 静默吞掉会让"分数怎么不动"变成无解之谜（S0 教训）。
            self.stats["clock_backwards"] += 1
            self.last_error = f"clock moved backwards {-elapsed:.1f}s (recovery skipped)"
            logger.warning(
                "[emotion] 时钟回退 %.1fs：跳过本次恢复（score 保持 %.3f）—— 重放虚拟时钟或 NTP 校正？",
                -elapsed, self._score,
            )
            self._last_ts = now
            return self._score
        self._last_ts = now
        if elapsed > 0.0 and self._score < self.max_score:
            before = self._score
            gained = elapsed / 60.0 * self.recovery_per_min
            after = min(self.max_score, before + gained)
            self._score = after
            self._last_change_ts = now
            self.stats["recovery_events"] += 1
            self.stats["recovered_total"] = round(self.stats["recovered_total"] + (after - before), 6)
            self._pending_recovery_log += (after - before)
            self._log_recovery(before, after, elapsed)
        return self._score

    def _log_recovery(self, before: float, after: float, elapsed: float) -> None:
        """恢复的落日志策略：状态变化（心情档 / 悬崖）必落；连续 accrual 按步长节流。"""
        if before < RAG_CLIFF_SCORE <= after:
            logger.warning(
                "[emotion] RAG 通道恢复：score %.3f→%.3f ≥ 悬崖 %.4f（乘子 %.3f）"
                "—— 非 @ 插话重新可能触发",
                before, after, RAG_CLIFF_SCORE, multiplier_for_score(after),
            )
        mood_before, mood_after = mood_for_score(before), mood_for_score(after)
        if mood_before != mood_after:
            logger.info(
                "[emotion] 心情 %s→%s（时间恢复，score %.3f→%.3f）",
                mood_before, mood_after, before, after,
            )
        due = self.recovery_log_step == 0.0 or (
            self.recovery_log_step > 0.0 and self._pending_recovery_log >= self.recovery_log_step
        )
        if due and self._pending_recovery_log > 0.0:
            self._pending_recovery_log = 0.0
            logger.info(
                "[emotion] 恢复 +%.4f（%.0fs @ %.3f/分）：score %.3f→%.3f 乘子 %.3f 心情 %s",
                after - before, elapsed, self.recovery_per_min,
                before, after, multiplier_for_score(after), mood_after,
            )

    # ---- pipeline 接口（签名与 v1 一致：不改接线也能用） ----
    async def query(
        self,
        group_id: str,
        recent_msgs: list[dict],
        kg_ctx: Optional["KGContext"] = None,
    ) -> EmotionState:
        """无 LLM、无 I/O、无缓存 —— 纯算术，故两次调用结果必然一致（可重放）。"""
        self.last_error = None
        with self._lock:
            s = self._advance_locked(self._now_fn())
        self.stats["queries"] += 1
        return EmotionState(
            global_willingness=multiplier_for_score(s),
            current_mood=mood_for_score(s),
            emotion_score=s,
        )


