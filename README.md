# astrbot_plugin_persona_agent

AstrBot 插件 —— 让 LLM 扮演群成员，模仿指定群友（`style_source_qq`）的说话风格，
在真实 QQ 群里**自主决定**何时发言、说什么。

> 详细文档：[`docs/services.md`](docs/services.md)（组件与锁定决策）、
> [`docs/params.md`](docs/params.md)（配置矩阵）、
> [`docs/history.md`](docs/history.md)（演进史）、
> [`docs/merge-gotchas.md`](docs/merge-gotchas.md)（数据集字段陷阱）。

## 三层回复链（核心设计）

```
外部硬闸（结构过滤器 + 音量阀）
  → GateLLM（语义决策 + 冲突安全阀）
  → RP LLM（纯表演）
```

| 层 | 职责 | 不做什么 |
|---|---|---|
| **外部硬闸** `interjection` | 睡眠窗/冷却/日程/预算；`rag.score_threshold` 控制 Gate 调用量 | **不做"该不该回"的判断** |
| **GateLLM** `gate` | 非 @ 的 reply 判定 + 全场景 conflict 安全阀（@ 也过） | 不生成回复；**必须有独立裁判 system** |
| **RP** `pipeline` → LLM | 纯粹的表演 | 不做决策 |

> ⚠️ **Gate 的 system 必须是裁判身份**，绝不能传 RP 的人格 ——
> 实测把人格当 Gate 的 system 会让模型"参与聊天而非判断"，
> 解析失败率 0% → 40~60%。

**意图不用 function-calling**：走文本标记（如 `[r:-N]` 引用、`[emote:]` 贴纸）
+ main 侧旁路执行。

## 上下文装配（前缀缓存纪律）

```
[system]  system prompt（会话首条，持久；变更走**追加块**）
[system]  示例块（恒定）
[system]  关系图谱（追加式增长）
[user/assistant]  session 全量历史（只追加）
[system]  KG 尾注（含 RAG 命中原文）              ← 每轮变
[system]  【现在要回应的】发话人+正文+图片+时间心情  ← 逐轮变
```

**恒定在前、易变在尾**：

- system prompt **是会话状态**（写进会话第一条，从日志加载）——
  旧实现每轮作为参数热加载，prompt 一改**整个请求第一个 token 就变**，
  其后 8 万 token 前缀全废（实测一天 8~11 次变更、单次 61k~99k 全价 token）
- 变更 → **追加** `［设定更新］…以此为准`，旧的不动（历史 token 不可变）
- 易变量**绝不进 system prompt**：它是第一个 token 位置

## 快速开始

1. AstrBot v4.25+，协议端见工作区根 `DESKTOP_STATE.md`（现役 SnowLuma + 真实 QQ）
2. 部署本插件目录，`git pull` 更新（**不碰数据目录**）
3. WebUI 填 `_conf_schema.json` 各项；**关键参数与易踩语义见 [`docs/params.md`](docs/params.md)**
4. 数据构建（离线，可选）：`tools/build_dataset.py` → `tools/rebuild_chroma.py`
5. 先 `test_mode=1` 在测试群验证，再切 `test_mode=0` 接管生产群

## 运行时数据目录

与代码目录**独立**（`git pull` 不覆盖）：

```
usages/<group_id>.json       interjection 用量（按群隔离，原子写 + 热重载）
logs/<group_id>/             trace / decision / gate / cache_probe / diary
session_<group>_<day>.json   按日会话（含 system_prompt 与思维链）
topic_bank.json / member_relations.json / system_prompt_fragments.json / ...
```

## 工具（`tools/`）

| 类别 | 工具 |
|---|---|
| 数据/索引 | `build_dataset` `verify_dataset` `clean_pairs` `rebuild_chroma` `smoke_rag` |
| 风格 | `analyze_style` `find_style_windows` `select_examples` `ab_test_examples` `ab_judge_style` |
| 观测/评测 | `trace_view` `trace_stats` `export_session_md` `ab_prompt` `replay_scene` |
| 运维 | `sync_config` `migrate_member_fields` |
| 一致性 | `conformance/check_inbound_shapes.py`（换协议端后解析是否仍成立）|

## 许可

[MIT](LICENSE) © 2026 FMDD61
