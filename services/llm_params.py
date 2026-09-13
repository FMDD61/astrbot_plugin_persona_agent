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


# ---------------------------------------------------------------------------
# provider 解析（S0）：把"配置值 / 进程已知值 / 会话默认值"的三级回退抽成纯函数，
# 使 main 的分支逻辑可离线单测（此前整段埋在 main.py 里，无任何测试覆盖）。
# ---------------------------------------------------------------------------


def resolve_provider_id(
    configured: str = "",
    known: Optional[str] = None,
    session_default: Optional[str] = None,
    configured_exists: Optional[bool] = None,
) -> Optional[str]:
    """三级回退解析 provider id。

    顺序：**配置值** → 本进程已知值 → AstrBot 当前会话默认。

    ``configured_exists`` 是配置值的**存在性校验**结果：
      - ``False`` → 配置写错了，**跳过它**走回退（否则 `llm_generate` 会抛
        ``ProviderNotFoundError``，被 except 吞成空回复 → 静默哑掉）
      - ``True`` / ``None``（未知，例如拿不到 provider_manager）→ 照用配置值

    返回 None 表示三级都没有 → 调用方应放弃本轮生成并告警。
    """
    cfg = str(configured or "").strip()
    if cfg and configured_exists is not False:
        return cfg
    if known:
        return str(known)
    if session_default:
        return str(session_default)
    return None
