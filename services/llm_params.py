"""llm_params — A7④ LLM 参数工具（纯函数，可离线单测）。

reasoning_effort 容错映射：配置取值 off/low/medium/high；
OpenAI 规范枚举 none/low/medium/high——部分网关不认 "off" → 映射 "none"
（等价关闭思考）。确定性优先：未知值保守映射 none。
"""
from __future__ import annotations


def reasoning_value(val) -> str:
    """Map configured reasoning_effort to a gateway-accepted value."""
    v = str(val or "").strip().lower()
    if v == "off":
        return "none"
    if v in ("none", "low", "medium", "high"):
        return v
    return "none"  # 未知值保守关思考（确定性优先）
