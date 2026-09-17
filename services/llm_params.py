"""llm_params — A7④ LLM 参数与响应取值工具（纯函数，可离线单测）。

本模块两份职责：
  1. **请求侧**：reasoning_value（reasoning_effort 容错映射）、resolve_provider_id
  2. **响应侧**：extract_reasoning（B-029：网关把思维链放在非标准字段）

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


# ---------------------------------------------------------------------------
# 思维链取值（B-029）：网关字段名与 AstrBot 标准字段名不一致
# ---------------------------------------------------------------------------

def extract_reasoning(resp) -> str:
    """从 LLM 响应里取出**思维链文本**（取不到返回空串，绝不抛）。

    🔴 B-029（2026-09-17 实测；根因表述经独立核验更正）：
      - AstrBot 的 openai_chat_completion **确实会**给
        LLMResponse.reasoning_content 赋值（源码 :876-878，流式 :672），
        但它只按 `self.reasoning_key`（默认 "reasoning_content"，源码 :399）
        去取属性 —— 见 `_extract_reasoning_content`（:700-724）
      - 而 commandcode 网关（OpenAI 兼容端点）把思维链放在**非标准字段**
        message.reasoning（str）+ message.reasoning_details（list），
        标准属性名 `reasoning_content` 取不到 → LLMResponse 里恒为 None

    实测同一模型同一参数：`reasoning_content=None` 而 `reasoning` 非空
    （140 字符）、`usage.reasoning_tokens` 44（不同调用 27–58 波动）。
    于是 S16 的"思维链留存"**从未生效**（401/401 次 reasoning_chars=0），
    导出永远显示"思维链：无"，看起来像"模型没思考"——而计费里明明有
    60,482 个 reasoning token。
    ⚠️ 本函数与 AstrBot 版本无关：即使上游改了 `reasoning_key`，这里也能兜住。

    取值优先级：标准字段 → 网关 reasoning → reasoning_details[].text。
    只读、不写、不改响应对象；任何异常都吞掉返回空串（观测不该拖垮主链）。
    """
    try:
        std = str(getattr(resp, "reasoning_content", "") or "").strip()
        if std:
            return std
    except Exception:
        pass
    try:
        msg = _raw_message(getattr(resp, "raw_completion", None))
        if msg is None:
            return ""
        # 网关非标准字段 1：reasoning（str）
        val = (msg.get("reasoning") if isinstance(msg, dict)
               else getattr(msg, "reasoning", None))
        if isinstance(val, str) and val.strip():
            return val.strip()
        # 网关非标准字段 2：reasoning_details（[{"type": "reasoning.text", "text": ...}]）
        det = (msg.get("reasoning_details") if isinstance(msg, dict)
               else getattr(msg, "reasoning_details", None))
        if isinstance(det, list):
            parts = []
            for d in det:
                t = d.get("text") if isinstance(d, dict) else getattr(d, "text", None)
                if isinstance(t, str) and t.strip():
                    parts.append(t.strip())
            if parts:
                return "\n".join(parts)
    except Exception:
        pass
    return ""


def _raw_message(raw):
    """从 raw_completion 里取出 message（兼容对象与 dict；取不到返回 None）。"""
    try:
        choices = (raw.get("choices") if isinstance(raw, dict)
                   else getattr(raw, "choices", None))
        if not choices:
            return None
        first = choices[0]
        return (first.get("message") if isinstance(first, dict)
                else getattr(first, "message", None))
    except Exception:
        return None


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
