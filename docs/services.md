# 服务组件与设计决策

> 自根 `AGENTS.md` 下沉（2026-09-16）—— AGENTS.md 只保留基本框架与底线。

### Key components
| Component | File | Role |
|-----------|------|------|
| SessionManager | services/session_manager.py | Per-group daily-rotating session; `name` 区分发言人；自动恢复；`_mid`/`_uin` 引用元数据（对外出口剥离）；空 content 自愈（B-002） |
| KGProvider | services/kg_provider.py | MultiSignalKGProvider (dense+BGE + BM25+FTS5 + entity); injects style guidance |
| EmotionProvider | services/emotion.py | LLMEmotionProvider v1: 3-dimension (willingness/mood/sticker_prompt)，30s 同群缓存，**超时须 ≥ 模型耗时 4–8s（默认 30s）**，降级中性并记 `last_error`/`stats` → `trace.emotion_degraded` |
| InterjectionManager | services/interjection.py | **S2 定位：结构过滤器 + 音量阀**（睡眠/冷却/@冷却/日程/预算 + RAG 预筛控制 Gate 调用量）；`min_gap_sec` 是真实输出的硬天花板 |
| MemoryStore | services/memory_store.py | SQLite ADD-only entity+relation graph + FTS5 BM25 |
| ConflictDetector | services/conflict_detector.py | 3-stage conflict (keyword+burst+LLM verify)——**仅 gate.enabled=0 时兜底**（A7④）；gate.enabled=1 时 conflict 由 GateLLM 承担 |
| StyleProfile | services/style_profile.py | 8 style files hot-reload；add_new_member 原子追加（G9）；WHO/HOW separated system_prompt, 6-segment time |
| RagService | services/rag_service.py | ChromaDB vector retrieval（warmup 预热；查询走 asyncio.to_thread） |
| ExamplesLoader | services/examples.py | G14 静态示例注入：mtime_ns 热重载，**块位于 session 之前**（S2 修正：排在增长段之后 = 永远落在缓存失效区），头部含规则A/B 约束 |
| VisionService | services/vision.py | G15 识图：**模型名必须带网关命名空间前缀**（默认 `deepseek/deepseek-v4.1-flash`；无前缀一律 HTTP 400，曾被静默吞成「无法识别」），reasoning=low + 512 tok，三源字节解析，30s 内存 TTL + **持久 LRU 哈希缓存**（image_desc_cache.json 跨重启复用；A7③） |
| text_style | services/text_style.py | 纯文本工具：clean_message_text / extract_quote / postprocess（AI 味/口癖/emoji/换行/长度） |
| ContextBuffer | services/context_buffer.py | 滑动窗口（仅 interjection 决策；LLM 上下文来自 session）+ **`QuoteIndex` 不可变引用快照**（B-001：编号基在生成前冻结） |
| PokeService | services/poke.py | G11 戳一戳：300s 同人冷却/小时配额 4/未知成员不回戳/conflict_keywords 抑制/poke_log.jsonl；enabled=0 全静默 |
| TopicBank | services/topic_bank.py | G12 冷场话题：§10 评分（0.45 silence+0.25 priority+0.20 hints+0.10 freshness）、topic_bank.json mtime_ns 热加载、发送后归档 topic_sent.json |
| SummaryService | services/summary.py | 周/月/**年**摘要：周月读日日记 + 原文抽样防失真；**年报读 12 篇月报**（月→年可整除无错位；周与月不可整除故周报是旁支）；推 admin_binding |
| familiarity | services/familiarity.py | 熟悉度体系：提案生成/解析（4 道过滤 + 只升不降）、`ProposalStore`（新批覆盖旧批）|
| token_accounting | services/token_accounting.py | 精确 token 对账：保存上次请求 → 公共前导 → 与网关 cached 交叉验证（按 lineage 分离 RP/Gate）|
| DreamMaker | services/dream.py | 做梦：7 篇日记 + 随机碎片化 → 锚点 → 生成；**不负责熟悉度**（与 DreamJob 分离）|
| GateService | services/gate.py | A7 GateLLM 决策层+安全阀：单 prompt 双任务 `{reply, conflict, reason}`；覆盖所有 REPLY（含 @）与 TOPIC；conflict=true 强制不发言；同群节流（缓存键含 is_at）；失败保守静默并记 `last_error`/`stats` → `trace.gate_degraded`；0.2 温度 + low 思考；gate.enabled=0 默认关 |
| PersonaPipeline | services/pipeline.py | A7 共享主链路（RAG→emotion→硬闸→Gate(含conflict)→KG→生成→quote→SendIntent+trace）；纯数据进出 + 构造注入；rag_enabled 开关；线上 main 与离线测试台共用 |
| llm_params | services/llm_params.py | A7④ reasoning_effort 映射：**off/未知 → None（不发送该参数）**，仅 low/medium/high/max 透传（网关拒 none/off 会 HTTP 400 → 空回复；2026-09-10 致命 bug 修复）；纯函数可单测 |
| JsonStore | services/json_store.py | 原子 JSON/JSONL 读写；save_json/append_jsonl 自动建子目录（usages/、logs/<gid>/） |

### Design decisions (locked)
- **Session reuse**: LLM gets full chat history via `contexts`, not one-shot buffer snapshots. `prompt=None`. system_prompt stays fixed → prefix caching。**上下文装配顺序（S2）：system_prompt → 示例块 → session → turn_block → KG 尾注**（恒定在前、增长段居中、易变在尾）。
- **Generating lock**: `_generating[group_id]` per-group lock prevents concurrent LLM calls. 500ms debounce before generation.
- **回复链三层职责（S2 锁定）**: 外部硬闸 = **结构过滤器 + 音量阀**（睡眠/冷却/@冷却/日程/预算；
  `rag.score_threshold` 只用于控制 Gate 调用量，**不做"该不该回"的判断**）；
  GateLLM = 语义决策（非 @ 的 reply 判定 + 全场景 conflict 安全阀，@ 也过）；
  RP = 纯粹表演。**spec §4.2 的"把决策窗口作为追加用户输入"已否决** —— 实测窗口
  96 条的文本 59% 已在 session 里，再注入是重复付费；改为"不加输入、只重新划界"。
- **Single reply**: No segment splitting. One yield per generation；leading `[r:-N]` 标记 → `event.chain_result([Comp.Reply(id), Comp.Plain(text)])` 引用链（目标为真实群消息 id 时），否则纯文本。
- **Pipeline (A7)**: 主决策/生成链路收敛到 `PersonaPipeline`（纯数据进出，构造注入 + generate 回调），线上 main 与离线测试台同一份代码防漂移；返回 `SendIntent`（含 trace）由 main 翻译成 event 结果；**不用 AstrBot ToolSet/function-calling**（意图走文本标记 + 旁路执行）。
- **GateLLM (A7④ 安全阀化)**: 所有候选发言（REPLY 含 @、TOPIC）在生成前经独立中性 LLM 双任务判定 `{reply, conflict, reason}`；conflict=true 强制不发言（防煽风点火）；@ 时提示通常应回但冲突除外（缓存键含 is_at 分离 @ 语境）；同群决策节流；失败保守静默；**gate.enabled=0 默认关**（手动开启验证；关时旧 conflict_detector 兜底）。
- **LLM 参数矩阵 (A7④，2026-09-13 修订)**: 采样参数（temperature/reasoning_effort）不进 prompt；RP/Diary/Summary 与 Gate/Emotion 结构化任务**统一 `low`** + 温度分档（RP 按场景，结构化 0.2）；**reasoning 映射语义：off/未知 → None = 不发送该参数**（`services/llm_params.py`；网关对 none/off/minimal/disabled/false 一律 400，dsh 自身 `reasoningEfforts:{False:None}` 同义）；`llm.max_tokens` 默认 **512**（需容纳 200–800 思考 token，256 会截空 content；此前是**死配置**，2026-09-13 才接线）；provider 同源（六处统一走 `_resolve_provider_id()`：RP/Gate/Emotion/Vision/Diary/Summary，启动即预热）。
- **hourly_budget 语义 (2026-09-10)**: `hourly_budget[h] = hourly_share[h] × default_daily_budget`，日预算默认 **2400**（原 24 是 v0.1 随手拍的保守值，导致低谷小时预算 <1 条/时结构性静音）。预算是**宽松保险丝**，真实发言节奏由决策门槛（`rag.score_threshold`、`interjection.min_gap_sec`）控制；`analyze_style --daily-budget` 可调。
- **Group isolation (A7)**: interjection 用量状态按群隔离 + `usages/<gid>.json` 持久化（原子写 + mtime 热重载）；topic_bank 已发归档按群隔离；日志按群分目录 `logs/<gid>/`；**poke 刻意跨群共享**（QQ 拍一拍按目标用户限，非按群）。
- **trace (A7)**: 每次处理落全链路 trace_log（含 RAG 命中原文/决策链/生成文本）→ 评估 RAG/BGE 价值与污染的数据依据；`trace.enabled=1` 默认开；工具 `tools/trace_view.py` 渲染。
- **Examples block (G14, S2 修正位置)**: 固定示例对话**插入 session 之前**（内容恒定 → 进入缓存稳定前缀）；原先在会话与 KG 之间，但 session 每轮变长 → 恒定内容落在失效区等于每轮白付。按文件 mtime_ns 热重载，A/B = 改名即切换。
- **test_mode**: `_conf_schema.json` has `test_mode` (0/1) + `test_group_id`. `_is_target_group` switches accordingly. **2026-08-25 已切 0（生产 881438753 接管）**。
- **G16 插话（2026-09-13 状态：已关）**: `active_interjection=0`（只 @ 才回，不主动插话）；`rag.score_threshold=0.60`（S2：实测非 @ RAG top1 p50=0.652 压在中位数上；**该值只是 Gate 调用量的音量阀，不做"该不该回"的判断**）；决策日志每个 entry 的 extra 落盘 `top_rag_score` + `emotion_multiplier`（调阈数据驱动）。
- **G11 poke（2026-09-12 已开）**: `poke.enabled=1`，`cooldown_sec=300`；回戳走 `group_poke` **action 通道**（消息段通道在部分协议端会被静默丢弃，见 `services/protocol_compat.py`）；真机实测通过（`poke_log.jsonl`）。
- **G12 topic_bank**: `topic_bank.enabled` 仍为 0；`topic_bank.json` 建议稿在 data_out/。
- **G13/G17 已开**: `summary.{weekly_enabled,monthly_enabled}=1`（周一 02:10 / 月首 02:15 cron，产物 jsonl + 推 bind_dream 私聊）；`dream.enabled=1`（周一 03:00 cron）；`dream_binding.json` 预绑定 `aiocqhttp:FriendMessage:337934842`。
- **Memory layer (#2)**: 3-layer design (Hermes/Cognee/Dreaming inspired). 已落地：KGProvider (retrieval) → MemoryStore (SQLite ADD-only + FTS5 BM25) → `services/dream.py`（周 cron，**已与原 DreamJob 分离**：做梦不负责熟悉度汇报）。Postgres 图+pgvector 仍是远期规划，未实现。

### Design decisions (locked)
- **Session reuse**: LLM gets full chat history via `contexts`, not one-shot buffer snapshots. `prompt=None`. system_prompt stays fixed → prefix caching。**上下文装配顺序（S2）：system_prompt → 示例块 → session → turn_block → KG 尾注**（恒定在前、增长段居中、易变在尾）。
- **Generating lock**: `_generating[group_id]` per-group lock prevents concurrent LLM calls. 500ms debounce before generation.
- **回复链三层职责（S2 锁定）**: 外部硬闸 = **结构过滤器 + 音量阀**（睡眠/冷却/@冷却/日程/预算；
  `rag.score_threshold` 只用于控制 Gate 调用量，**不做"该不该回"的判断**）；
  GateLLM = 语义决策（非 @ 的 reply 判定 + 全场景 conflict 安全阀，@ 也过）；
  RP = 纯粹表演。**spec §4.2 的"把决策窗口作为追加用户输入"已否决** —— 实测窗口
  96 条的文本 59% 已在 session 里，再注入是重复付费；改为"不加输入、只重新划界"。
- **Single reply**: No segment splitting. One yield per generation；leading `[r:-N]` 标记 → `event.chain_result([Comp.Reply(id), Comp.Plain(text)])` 引用链（目标为真实群消息 id 时），否则纯文本。
- **Pipeline (A7)**: 主决策/生成链路收敛到 `PersonaPipeline`（纯数据进出，构造注入 + generate 回调），线上 main 与离线测试台同一份代码防漂移；返回 `SendIntent`（含 trace）由 main 翻译成 event 结果；**不用 AstrBot ToolSet/function-calling**（意图走文本标记 + 旁路执行）。
- **GateLLM (A7④ 安全阀化)**: 所有候选发言（REPLY 含 @、TOPIC）在生成前经独立中性 LLM 双任务判定 `{reply, conflict, reason}`；conflict=true 强制不发言（防煽风点火）；@ 时提示通常应回但冲突除外（缓存键含 is_at 分离 @ 语境）；同群决策节流；失败保守静默；**gate.enabled=0 默认关**（手动开启验证；关时旧 conflict_detector 兜底）。
- **LLM 参数矩阵 (A7④，2026-09-13 修订)**: 采样参数（temperature/reasoning_effort）不进 prompt；RP/Diary/Summary 与 Gate/Emotion 结构化任务**统一 `low`** + 温度分档（RP 按场景，结构化 0.2）；**reasoning 映射语义：off/未知 → None = 不发送该参数**（`services/llm_params.py`；网关对 none/off/minimal/disabled/false 一律 400，dsh 自身 `reasoningEfforts:{False:None}` 同义）；`llm.max_tokens` 默认 **512**（需容纳 200–800 思考 token，256 会截空 content；此前是**死配置**，2026-09-13 才接线）；provider 同源（六处统一走 `_resolve_provider_id()`：RP/Gate/Emotion/Vision/Diary/Summary，启动即预热）。
- **hourly_budget 语义 (2026-09-10)**: `hourly_budget[h] = hourly_share[h] × default_daily_budget`，日预算默认 **2400**（原 24 是 v0.1 随手拍的保守值，导致低谷小时预算 <1 条/时结构性静音）。预算是**宽松保险丝**，真实发言节奏由决策门槛（`rag.score_threshold`、`interjection.min_gap_sec`）控制；`analyze_style --daily-budget` 可调。
- **Group isolation (A7)**: interjection 用量状态按群隔离 + `usages/<gid>.json` 持久化（原子写 + mtime 热重载）；topic_bank 已发归档按群隔离；日志按群分目录 `logs/<gid>/`；**poke 刻意跨群共享**（QQ 拍一拍按目标用户限，非按群）。
- **trace (A7)**: 每次处理落全链路 trace_log（含 RAG 命中原文/决策链/生成文本）→ 评估 RAG/BGE 价值与污染的数据依据；`trace.enabled=1` 默认开；工具 `tools/trace_view.py` 渲染。
- **Examples block (G14, S2 修正位置)**: 固定示例对话**插入 session 之前**（内容恒定 → 进入缓存稳定前缀）；原先在会话与 KG 之间，但 session 每轮变长 → 恒定内容落在失效区等于每轮白付。按文件 mtime_ns 热重载，A/B = 改名即切换。
- **test_mode**: `_conf_schema.json` has `test_mode` (0/1) + `test_group_id`. `_is_target_group` switches accordingly. **2026-08-25 已切 0（生产 881438753 接管）**。
- **G16 插话（2026-09-13 状态：已关）**: `active_interjection=0`（只 @ 才回，不主动插话）；`rag.score_threshold=0.60`（S2：实测非 @ RAG top1 p50=0.652 压在中位数上；**该值只是 Gate 调用量的音量阀，不做"该不该回"的判断**）；决策日志每个 entry 的 extra 落盘 `top_rag_score` + `emotion_multiplier`（调阈数据驱动）。
- **G11 poke（2026-09-12 已开）**: `poke.enabled=1`，`cooldown_sec=300`；回戳走 `group_poke` **action 通道**（消息段通道在部分协议端会被静默丢弃，见 `services/protocol_compat.py`）；真机实测通过（`poke_log.jsonl`）。
- **G12 topic_bank**: `topic_bank.enabled` 仍为 0；`topic_bank.json` 建议稿在 data_out/。
- **G13/G17 已开**: `summary.{weekly_enabled,monthly_enabled}=1`（周一 02:10 / 月首 02:15 cron，产物 jsonl + 推 bind_dream 私聊）；`dream.enabled=1`（周一 03:00 cron）；`dream_binding.json` 预绑定 `aiocqhttp:FriendMessage:337934842`。
