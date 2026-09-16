# -*- coding: utf-8 -*-
"""token_accounting —— 精确 token 对账（S15）。

## 用户要求（2026-09-15）

> "由于不涉及共用前缀，因此对缓存命中的分析要求会更加严格，在缓存未被网关
> 抛弃的前提下，我们要能**精确对应上缓存命中和未命中的 tokens**"

## 问题：网关只给一个数字，不说它对应哪一段

网关返回 `cached_tokens=N`，但**不告诉我们这 N 对应我们哪一段内容**。于是不一致时
无法区分两种完全不同的原因：

  ① **我们改了历史** → 前缀本就不该命中（我们的错，可修）
  ② **网关丢了缓存**（TTL 过期 / 服务端淘汰）→ 我们没问题（不可控）

不区分就无法定位，也无法判断"改缓存策略有没有用"。

## 做法：保存上次同 lineage 的请求，逐条比对算公共前导

```
common_prefix(prev, cur) → (消息条数, 字符数)
```

再与网关的 `cached_tokens` 交叉验证，给出 verdict：

| verdict | 条件 | 含义 |
|---|---|---|
| `first_of_lineage` | 该 lineage 首次 | 无对照，正常 |
| `ok` | 公共前导 > 0 且缓存占比合理 | 账号正常 |
| `gateway_dropped` | **公共前导很大但 cached 接近 0** | 🔴 不是我们的问题（TTL/淘汰） |
| `prefix_changed` | 公共前导为 0 | 我们改了历史（预期内，如 prompt 追加） |
| `partial` | 有公共前导但缓存明显低于它 | 需具体看 |

## 为什么按 lineage 分离（用户同时要求）

RP 与 Gate 现在**共用同一份历史、各用各的 system**（S14）→ 它们是**两条不同前缀**、
各自独立命中。混记就无法归属。`lineage` = `"rp:100000001"` / `"gate:100000001"`。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional

#: 已知的 LLM 用途（用户要求"日志内 session ID 区分开"）
LINEAGES = ("rp", "gate")

STATE_FILE = "token_accounting_state.json"

#: 唯一无歧义的判定阈值：**公共前导很大（说明我们没动前缀）但 cached 为 0**
#: → 只能是网关侧没给缓存。这个判据不依赖字符→token 换算，故可靠。
#: 其余情况只报事实，不下结论 —— 因为**我不知道网关的字符→token 比例**
#: （拍脑袋的阈值必然误判，本模块第一版就栽在这）。
_MEANINGFUL_PREFIX_CHARS = 200


def lineage_of(kind: str, group_id: str) -> str:
    """lineage 标识 —— 日志里的 session ID。"""
    return f"{str(kind or 'rp')}:{str(group_id or '')}"


def normalize_messages(messages) -> list[dict]:
    """只保留 ``role``/``content``/``name``（前缀比较口径）。

    - 丢内部元数据（``_mid``/``_uin``）—— 它们不进 LLM 请求
    - 丢掉非 dict、无 role 的条目（容忍脏数据，绝不抛）
    """
    out: list[dict] = []
    for m in messages or ():
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if not role:
            continue
        d = {"role": str(role), "content": str(m.get("content") or "")}
        if m.get("name"):
            d["name"] = str(m["name"])
        out.append(d)
    return out


def _chars(m: dict) -> int:
    return len(m.get("content") or "") + len(m.get("name") or "")


def common_prefix(prev: list[dict], cur: list[dict]) -> tuple[int, int]:
    """公共前导 = **从第一条开始逐条相同**的长度。返回 ``(条数, 字符数)``。

    ⚠️ 只比整条：同一位置若 role/content 不同即止。
    不做"同一条内部的最长公共子串"—— 前缀缓存的粒度也是"到第一个不同字节"，
    但商用网关的实际粒度是消息级；**保守按消息级估算**，宁可低估。
    """
    n = 0
    chars = 0
    for a, b in zip(prev or (), cur or ()):
        if a != b:
            break
        n += 1
        chars += _chars(a)
    return n, chars


def _state_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / STATE_FILE


def _load_state(data_dir: str | Path) -> dict:
    p = _state_path(data_dir)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return d
    except (OSError, json.JSONDecodeError):
        pass
    return {"lineages": {}}


def _save_state(data_dir: str | Path, state: dict) -> None:
    p = _state_path(data_dir)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def record_and_compare(
    data_dir: str | Path,
    *,
    kind: str,
    group_id: str,
    messages,
    cached_tokens: Optional[int] = None,
    prompt_tokens: Optional[int] = None,
) -> dict:
    """记录本次请求并与上次比对。返回对账结果（**绝不抛**）。

    返回字段：
      - ``lineage``：session ID（``rp:gid`` / ``gate:gid``）
      - ``first_of_lineage``：该 lineage 是否首次
      - ``common_prefix_msgs`` / ``common_prefix_chars``：与上次的公共前导
      - ``prefix_hash16``：本次**完整请求**的短哈希（便于跨日志追踪）
      - ``cached_tokens`` / ``uncached_tokens``：网关口径
      - ``verdict``：``ok`` / ``gateway_dropped`` / ``prefix_changed`` /
        ``first_of_lineage`` / ``partial``
    """
    out: dict = {
        "lineage": lineage_of(kind, group_id),
        "kind": str(kind or ""),
        "first_of_lineage": False,
        "common_prefix_msgs": 0,
        "common_prefix_chars": 0,
    }
    try:
        cur = normalize_messages(messages)
        blob = json.dumps(cur, ensure_ascii=False, sort_keys=True)
        out["prefix_hash16"] = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
        out["msgs"] = len(cur)
        out["chars"] = sum(_chars(m) for m in cur)

        state = _load_state(data_dir)
        lin = (state.get("lineages") or {}).get(out["lineage"]) or {}
        prev = lin.get("messages") or []

        if not prev:
            out["first_of_lineage"] = True
        else:
            n, chars = common_prefix(prev, cur)
            out["common_prefix_msgs"] = n
            out["common_prefix_chars"] = chars

        # 网关口径
        if isinstance(cached_tokens, int):
            out["cached_tokens"] = cached_tokens
        if isinstance(prompt_tokens, int):
            out["prompt_tokens"] = prompt_tokens
        if isinstance(cached_tokens, int) and isinstance(prompt_tokens, int):
            out["uncached_tokens"] = max(0, prompt_tokens - cached_tokens)

        out["verdict"] = _verdict(out)

        # 落状态（只留每 lineage 的上一次请求 —— 文件大小恒定）
        lin["messages"] = cur
        lin["hash16"] = out["prefix_hash16"]
        lin["ts"] = round(time.time(), 1)
        (state.setdefault("lineages", {}))[out["lineage"]] = lin
        _save_state(data_dir, state)
    except Exception as e:                      # 观测绝不拖垮主流程
        out["accounting_error"] = f"{type(e).__name__}: {e}"
    return out


def _verdict(r: dict) -> str:
    """只给**可观测事实**，不硬套阈值。

    ⚠️ 本模块第一版用 `cached / (公共前导字符 × 0.75)` 算比例并给 ok/partial
    判定 —— 但**我不知道该网关真实的字符→token 比例**，阈值必然误判
    （测试当场抓到）。改为：只报事实，唯一例外是**无歧义**的那种情况。

    无歧义判据：**公共前导可观（≥200 字符 = 我们确实没动前缀）且 cached=0**
    → 只能是网关侧没给缓存。它不依赖任何换算。
    """
    if r.get("first_of_lineage"):
        return "first_of_lineage"
    cp = int(r.get("common_prefix_chars") or 0)
    cached = r.get("cached_tokens")
    if not isinstance(cached, int):
        return "no_usage"            # 网关没返回 usage
    if cached == 0 and cp >= _MEANINGFUL_PREFIX_CHARS:
        return "gateway_dropped"     # 🔴 前缀没动却没命中 → 网关侧
    if cp == 0:
        return "prefix_changed"      # 我们动了前缀（预期内，如 prompt 追加）
    return "prefix_reused"           # 有公共前导 → 正常复用
