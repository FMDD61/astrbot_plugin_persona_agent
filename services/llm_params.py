"""llm_params — A7④ LLM 参数工具（纯函数，可离线单测）。

reasoning_effort 容错映射（2026-09-10 实测修正）：

  commandcode 网关（api.commandcode.ai/provider/v1，OpenAI 兼容）**拒绝**任何
  非标准 reasoning_effort 取值——"none"/"off"/"minimal"/"disabled"/false 全部
  HTTP 400；关闭显式思考档位的唯一正确方式是**不发送该参数**（网关按模型默认
  跑）。dsh 自身配置可佐证：`reasoningEfforts: {False: None, 'low': 'low', ...}`
  —— False 映射到 None（即不传）。

  历史 bug：旧实现 off→"none" → 每次 RP/Gate/Emotion 调用都 400 → 被
  try/except 吞掉 → 生成空 → 机器人静默（A7 若上线会导致全哑）。

映射规则：
  off/空/未知/非法值 → None（调用方跳过该键，不写进请求体）
  low / medium / high / max → 原样透传（网关接受的档位）
"""
from __future__ import annotations

from typing import Optional

# 网关（OpenAI 兼容端点）明确接受的档位；"none"/"minimal"/"off" 会 400
_VALID = ("low", "medium", "high", "max")


def reasoning_value(val) -> Optional[str]:
    """Map configured reasoning_effort to a gateway-accepted value.

    返回 None 表示"不要发送 reasoning_effort"（用网关/模型默认）。
    调用方必须跳过 None，而不是把它写进请求体。
    """
    v = str(val or "").strip().lower()
    if v in _VALID:
        return v
    return None
