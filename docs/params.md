# 关键参数矩阵

> 自根 `AGENTS.md` 下沉（2026-09-16）。**改参数前先查线上配置** ——
> 生产配置的**显式值会覆盖 schema 默认**。实测数据基线见
> `docs/specs/measurements.md`（根工作区）。

## 生产配置（2026-09-13 部署，以线上为准）

| 键 | 值 |
|---|---|
| `test_mode` | 0（生产群 100000001）|
| `active_interjection` | 0（只 @ 才回，不主动插话）|
| `rag.score_threshold` | 0.55（用户后改）|
| `interjection.min_gap_sec` | 5 |
| `gate.enabled` | 1 |
| `sticker.enabled` / `teach` | 1 / 1 |
| `poke.enabled` | 1（cooldown 60s / hourly 30 / proactive 开）|
| `dream.enabled` | 1 |
| `summary.weekly_enabled` / `monthly_enabled` / `yearly_enabled` | 1 / 1 / 1 |
| `llm.max_tokens` | 512（需容纳 200–800 思考 token）|
| `vision.model` | `deepseek/deepseek-v4.1-flash`（**须带网关命名空间前缀**）|
| `emotion.timeout_sec` / `gate.timeout_sec` | 30 / 30（须 ≥ 模型耗时 4–8s）|
| `*.reasoning_effort` | `low`（**off 会被网关拒**，见下）|
| `llm.provider_id` | `commandcode/deepseek/deepseek-v4.1-flash` |
| `privileged_qq` | 100000002 |

## 参数语义（易踩）

- **`reasoning_effort`**：`off`/未知 → **不发送该参数**（网关对 `none`/`off`
  一律 400）。填 off 只是不发参数，模型仍按默认档思考 4–8s。
- **`max_tokens` 是上限不是消耗**，且思考 token 是**重尾随机变量**
  （同 prompt 实测 313–3138）→ 预算给足无代价，给紧会截断成空。
- **`hourly_budget`**：`hourly_share[h] × default_daily_budget`（默认 2400）。
  它是**宽松保险丝**；真实节奏由 `score_threshold` / `min_gap_sec` 控制。
- **`rag.score_threshold` 只控制 Gate 调用量，不做"该不该回"的判断**。
- **`[r:-N]` 教学**：`N=1` 是倒数第 1 条；**缺负号（`[r:1]`）无效**。
