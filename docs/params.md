# 关键参数矩阵

> 自根 `AGENTS.md` 下沉（2026-09-16）。**改参数前先查线上配置** ——
> 生产配置的**显式值会覆盖 schema 默认**。实测数据基线见
> `docs/specs/measurements.md`（根工作区）。

## 生产配置（2026-09-13 部署，以线上为准）

| 键 | 值 |
|---|---|
| `test_mode` | 0（生产群；**群号见线上配置，不写进仓库**）|
| `active_interjection` | 0（只 @ 才回，不主动插话）|
| `rag.score_threshold` | **0.8（2026-09-22 用户按观察 0.55→0.7→0.8；0.8 ≈ 只回 @）**（可调，非"生产值"）** —— 用户 2026-09-21：
「RAG 设定值从来没有固定的生产值……本项目几乎没有可定为长期不变的度量值，
一切都还有待长期数据统计。」⇒ 下表所有数值都按"**当前取值**"读，不是恒定基线。
该阈值只控制 **Gate 调用量**，不做「该不该回」的判断（见 AGENTS.md 三层链）。|
| `interjection.min_gap_sec` | 5 |
| `gate.enabled` | 1 |
| `sticker.enabled` / `teach` | 1 / 1 |
| `poke.enabled` | 1（cooldown 60s / hourly 30 / proactive 开）|
| `dream.enabled` | 1 |
| `summary.weekly_enabled` / `monthly_enabled` / `yearly_enabled` | 1 / 1 / 1 |
| `llm.max_tokens` | 512（需容纳 200–800 思考 token）|
| `vision.model` | `deepseek/deepseek-v4.1-flash`（**须带网关命名空间前缀**）|
| `gate.timeout_sec` | 30（须 ≥ 模型耗时 4–8s）|
| `emotion.*`（C24 起）| **不再有 LLM 调用**（`timeout_sec`/`cache_ttl_sec`/`temperature`/`reasoning_effort` 已删 —— 设了没人读）；
现有旋钮 = `_conf_schema.json` 的 `emotion.items` = `ScoreEmotionProvider.from_config` 认的键：
`enabled` / `initial_score` / `blocked_penalty` / `recovery_per_min` / `min_score` / `max_score` / `recovery_log_step`
（有测试钉住"schema 键 == 代码键"）。`enabled=0` = 恒中性：乘子 1.0、不注入心情、零日志。
⚠️ 阈值不再写死在代码里 —— `rag.score_threshold` 改了就注入 emotion 的"悬崖"换算。|
| `*.reasoning_effort` | `low`（**off 会被网关拒**，见下）|
| `llm.provider_id` | `commandcode/deepseek/deepseek-v4.1-flash` |
| `privileged_qq` | 见线上配置（**不写进仓库**）|

## 参数语义（易踩）

- **`reasoning_effort`**：`off`/未知 → **不发送该参数**（网关对 `none`/`off`
  一律 400）。填 off 只是不发参数，模型仍按默认档思考 4–8s。
- **`max_tokens` 是上限不是消耗**，且思考 token 是**重尾随机变量**
  （同 prompt 实测 313–3138）→ 预算给足无代价，给紧会截断成空。
- **`hourly_budget`**：`hourly_share[h] × default_daily_budget`（默认 2400）。
  它是**宽松保险丝**；真实节奏由 `score_threshold` / `min_gap_sec` 控制。
- **`rag.score_threshold` 只控制 Gate 调用量，不做"该不该回"的判断**。
- **引用标记（C17 打标制，现役）**：模型只写裸 `[r]` —— 它只说「要引」，引用对象 = 硬闸
  放行的那条（PHI 的【现在要回应的】行），id 由代码给出（`extract_quote` 返回 `n=None`）。
  旧「编号制」`[r:-N]`（`N=1` = 倒数第 1 条）仍兼容，供 `persona.sections_mode=legacy` 回退。
  ⚠️ 解析层**不校验负号**：`[r:1]` 与 `[r:-1]` 同义（`RE_QUOTE_MARK` 里是 `-?`）——
  旧文档写的「缺负号无效」与代码不符（2026-09-23 实测 `extract_quote("[r:1] 你好")` → `("你好", 1)`）。
- **GIF 识图（C27）**：**多帧 GIF 一律**抽 6 帧拼 3 列×2 行网格（每帧长边 320px / JPEG q82）
  + 网格专用提示词。**没有体积闸门**（旧 `image_prep.GIF_INLINE_MAX_BYTES` 已删，别再找它调）；
  只有拼网格失败（无 Pillow / 解码失败）才回退首帧，并落 warning + `stats.gif_grid_fail` + `diag`。
- **出站文本 `postprocess`（C15）**：**不再删 emoji 与 @**（那两个函数已删）；仍剥 AI 味短语、
  元信息括号、`[r]`/`[emote:]`/`[poke:]` 标记，并做口癖封顶 / 换行折叠 / 400 字截断。
  ⚠️ 统计 emoji·@ 出现率时注意末尾截断会把第 401 位之后的内容一起丢掉。
