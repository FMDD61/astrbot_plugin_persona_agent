## [v0.4.0] - 2026-08-24
- 版本号与 metadata 对齐（v0.1.0→v0.4.0）；文档全面校正（AGENTS/OPERATIONS/README/IMPLEMENTATION_PLAN/DEPLOYMENT_GUIDE）；本会话 G1-G18 闭环明细见根目录 TODO.md

# Changelog

本文档记录 `astrbot_plugin_persona_agent` 的所有功能变动。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/).

---

## [Unreleased]

### Fixed (2026-09-13, R2 重构 S0：静默失效可见化 —— 四个"装成正常"的缺陷)
> 背景：对生产 trace（2262 条）/ runtime.log / SnowLuma 归档日志取证后发现同一类病：
> **结构化 LLM 调用失败后被 except 吞掉，降级值与"模型正常输出"在日志上完全不可区分**。
> 四个缺陷都因此隐藏了 3–17 天。本批次统一把降级做成**可统计、可直读**的字段。

- **🔴 识图从未成功过一次（656/656 条退化为「（配图：无法识别）」）**：`vision.model` 默认值 `deepseek-v4-flash-vision-exp` **缺网关命名空间前缀**，实测一律 `HTTP 400 unsupported_model`；`describe_bytes` 的 `except Exception: return None` 把它静默成"无描述"。实测同一张 PNG：`deepseek/deepseek-v4.1-flash` 3.8s ✅ / `deepseek/deepseek-v4-flash-vision-exp` 4.7s ✅ / `Qwen/Qwen3.8-Flash` 4.6s ✅ / `xiaomi/mimo-v2.5` 22.5s（过慢）/ 无前缀名一律 400 ❌。默认值改为 `deepseek/deepseek-v4.1-flash`。连带：`image_desc_cache.json` 从未生成（无成功描述可写），且 `memory_store.db` 把 `（配图：无法识别）` 拆成 `配图`/`识别`/`无法` 三个话题入库 —— 识图修好后此污染源自消
- **🔴 情绪引擎从未生效过（2262 条 trace 全是 `willingness=1.0, mood="", sticker=""`）**：`emotion.timeout_sec=3` 而**模型自身耗时 4.1–8.0s**（思考 token 227–738）→ 每次 `wait_for` 超时 → 静默回退中性。修复：① `LLMEmotionProvider` 新增 `last_error` + `stats{ok,cached,timeout,error,parse_fail}`，pipeline 落 `trace.emotion_degraded`；② `_parse` 不再 `except → neutral`（那是把"格式不合"伪装成"中性"，改为抛出并由 query 分类计数）；③ 容错剥离 ```json 围栏与前后解释文字；④ 默认超时 3→**30s**、`reasoning_effort` off→**low**
- **🔴 `gate.enabled=1` 会变成"全体不发言"**：GateLLM 用**同样的 3s 超时** + 同一模型 → 必然超时 → 保守静默。因 `@` 不受 gate 影响（且当前 enabled=0）未暴露，一开就是非 @ 消息**全部静默**。修复：`GateService.last_error` + `stats` + `trace.gate_degraded`；默认超时 3→**30s**
- **🔴 日记靠"上一次回复用过的 provider"才跑得起来**：`_generate_diary` 要求 `self._last_provider_id`，而它**只在真的生成过回复后**才赋值 → 02:05 cron 若当天无人 @ 过就永远跳过（实测 `diary skipped: no provider id known yet`）。修复：新增 `_resolve_provider_id()` 单一收口（配置值 → 本进程已知 → AstrBot 当前会话 provider），RP/Emotion/Gate/Vision/Diary/Summary **六处统一**；启动 `_warm_provider_id()` 预热
- **cache probe 从未覆盖线上主回复链路**：`_log_llm_probe` 要求 `event.get_group_id()`，而 pipeline 走 `event=None` 的 standalone 回调 → 探针只有 topic 路径写入（105 行全停在 2026-08-27），**缓存命中率长期无数据**。修复：接受显式 `group_id`（由 `umo` 解出群号）
- **`llm.max_tokens` 是死配置**：schema 有、离线测试台用，**线上生成路径从未读取**。已接上透传
- **日记 `day` 偏移一天**：归档 09-12 的会话，`day` 却取轮换后的 `day_key()`（09-13）。改为按同一口径回推 24h
- **provider id 写错会静默哑掉（新增加固）**：`_resolve_provider_id()` 现在**先校验配置值存在性** —— 写成不存在的 id 时 `llm_generate` 抛 `ProviderNotFoundError`、被 except 吞成空回复（又一条"看起来正常其实全哑"）。校验失败 → 告警 + 回退到会话 provider；拿不到 `provider_manager` 时返回"未知"照用配置值（不让校验本身成为故障源）。三级回退逻辑抽为纯函数 `llm_params.resolve_provider_id()`，可离线单测（+8 例）

### Changed (2026-09-13, S2 回复链构造：输入打包重划 + Gate 职责 + 缓存序修正)
> 动工前的实测把 spec §4.2 的一个前提推翻了，故按证据重做。

- **🔴 推翻 spec §4.2 的"把决策窗口作为追加用户输入"**：实测真实决策窗口
  （96 条/1h，deque 200 + 1h 窗口）的文本有 **59% 已在 session 里**
  （662/901 字符）—— session 本就是完整有序的对话记录（bot 自己的回复、
  当前消息、窗口文本全在里面）。再注入一遍是**重复付费 + 同一内容看两遍**。
  **改为"不加输入、只重新划界"**：把「当前这一轮」显式抬出来。
- **`【现在要回应的】`块**（pipeline `turn_block` 回调 + `main._build_turn_block`）：
  一条 system 消息集中给出 该回哪句 / 对谁 / 图片表情 / 当下时间心情，替代原先
  散落的「当前说话人」「volatile_line」两条独立消息 —— 注意力集中在一处。
- **识图描述「直投式」注入**（用户要求：融合 dsh read_image 的直投性质、
  去掉 agent 式的聚焦提问）：新增 `text_style.split_media_annotations()` 把
  `（配图：…）`/`（表情：…）` 从正文**拆出来**，改以 `［图片］…`/`［表情］…`
  行注入 —— 语义等价于模型自己看图，而不是别人转述的一句话。
  失败（`无法识别`）渲染成 `［图片］看不清内容（识图失败）`，不丢弃：RP 不该脑补。
- **🔴 本条"推迟入会话"（`defer_session_append` / `flush_session_append`）**：
  原先 main 在收到消息时就写 session，导致 LLM 上下文与引用编号基都包含
  **当前这条** → 与「现在要回应的」块**说两遍**。现在 main 只挂起，pipeline 在
  **硬闸决策后、上下文构建后、引用快照前、任何 early return 前**落盘。
  三条约束各有理由（不重复 / 编号基与所见一致 / 静默睡眠也要记录）。
  早退路径用幂等 `_ensure_session_append`（每轮只落一次）。
  附带改进：`_generating` 早退（同群正在生成）时本条留到下一轮一起落盘，
  不再直接丢弃。
- **🔴 缓存序修正：恒定示例块移到 session 之前**：它原先拼在 session 之后，
  而 session 每轮都在变长（实测一天 1482 条）—— 排在增长段之后的恒定内容
  **永远落在缓存失效区，等于每轮白付**。移到 session 前即进入稳定前缀。
- **Gate prompt 增强**：把"不算冲突"的判据写具体（玩笑互怼/嘴炮约战/观点争论
  **要明确判为非冲突**；判据是"有没有真实的恶意与伤害意图"，不是语气强不强）。
  依据：对抗性压测 6 例 —— 真冲突 3/3 命中、正常交流 2/2 放行，**误报 1 例**
  （"你个菜鸡/solo 啊/输了叫爸爸"被判成冲突）。安全方向正确（漏报 0 是关键），
  误报是可接受的保守代价，故只做提示词澄清、不放松阈值。
- **配置**：`rag.score_threshold` 0.65 → **0.60**、`interjection.min_gap_sec`
  25 → **20**。实测非 @ 消息 RAG top1 分布 p50=0.652/p75=0.687/p90=0.714 —— 旧值
  正好压在中位数上。**这不是"该不该回"的判断，而是 Gate 调用量的音量阀**
  （Gate 单次 4.8s：无过滤 2778 候选/天 → 3.7h；≥0.65 → 1.9h）。
- **Gate 真机首次验证**（此前 `enabled=0`，2262 条 trace 零 gate 记录）：
  8 个真实候选 7 个成功解析（唯一失败是空返回 → 思考吃光 512 max_tokens，
  与 2026-09-10 修的同一类）；延迟 3.1–7.3s（均 4.8s）。
- **轮转前落盘挂起条目**（`flush_all_pending_appends`）：02:05 cron 与每消息兜底
  轮转都先 flush —— 否则上一轮挂起的消息会被写进**新一天**的会话，出现在错误的
  日期归档里
- 测试 287 → **304 全绿**（新增 S2 turn block 9 例、推迟落盘 7 例、缓存序 1 例）

### Fixed (2026-09-13, B-014 KG 入库质量门 + B-015 识图诊断 + 流式缓存)
> 用户指出：**原 RAG 就是因为质量问题做了第二轮清洗（7075 → 4704 对）**，而 KG 入库
> 从来没有等价的一道。实测确认，并补上。

- **🔴 B-014 KG 入库质量门（对齐 RAG 二轮清洗思路）**：`memory_store.db` 实测
  **distinct topic 实体 3194 个**、**66.3% 只出现过一次**，其中
  `配图`(1511)/`识别`(1289)/`无法`(1210) 占实体行 **31.9%** —— 那是**我们自己的
  识图占位符** `（配图：无法识别）` 被 jieba 切出来的，**系统产物污染了自己的长期记忆**。
  两道门 + 一个清洗工具：
  - **门槛① 图片描述不入 topic**：含 `（配图：` 的消息跳过关键词抽取（描述是"眼前信息"，
    不是长期话题；且长描述进 jieba 必然持续产噪）。speaker/@ 实体照常记录
  - **门槛② 只出现一次不入图**：topic 首次出现只记实体行（待观察），第二次才建
    `talks_about` 边 → 一次性噪声不进图结构。`memory.topic_min_occurrences` 可配（1 = 关闭）
  - **`services/kg_stopwords.py`**：结构词（系统占位符产物）+ 无信息量通用词过滤。
    **刻意保留** `晚安/早睡/签到/戒色/老婆` 这类群内真实行为/文化词 —— 原则是
    **宁可漏删，不可误删**，真正的噪声交给门槛②
  - **`tools/clean_kg.py`**：历史清洗（默认 dry-run，`--apply` 自动备份 + 单事务）。
    三类目标：结构词 topic / 通用词 topic / 一次性 topic。**只删 `talks_about`，
    绝不碰 `mentions`（@ 关系）与 member 实体**
- **🔴 图片描述持久缓存从未落盘（流式化）**：`_touch_persist` 只标脏，而 `_flush_persist`
  的唯一调用点是 `main.terminate()` —— "惰性 flush"那一半**从未实现**。实测跑了 7 次
  成功识图，`image_desc_cache.json` 至今不存在，A7③ 的"跨重启复用"完全没生效。
  改为**计数 + 时间双阈值流式落盘**（默认每 5 条或 30s，先到者触发）。
  ⚠️ 实现时踩到一个**自伤死锁**：`describe_bytes` 在 `with self._lock` 内调
  `_touch_persist`，而新的 flush 又取同一把非重入锁 → **整个测试套件挂死 600s**。
  修法：拆出 `_flush_persist_locked()`（要求调用方已持锁）供锁内路径复用
- **B-015 识图失败诊断**：`无法识别` 是**一个结果、四种成因**（取不到图 / 模型空返回 /
  异常 / 超时）。`resolve_image_bytes(..., diag)` 逐级记录失败原因；
  `describe_bytes/describe_image` 加 `last_error` + `stats{ok,cache_hit,empty,timeout,error}`；
  全部失败时 WARNING 打出诊断 JSON（hash/mime/bytes/失败层）
- `MemoryStore` 取词函数**可注入**（`keyword_fn`）—— 门槛逻辑的测试不再依赖环境装没装 jieba
  （本机就没有，原测试会空转）
- 测试 257 → **287 全绿**（新增 `test_memory_quality.py` 23 例）

### Fixed (2026-09-13, R2 重构 S1：B-001 引用错位 + B-002 空条目静默)
- **🔴 B-001 `[r:-N]` 引用目标错位（线上事故 11:45，群 881438753）**：LLM 按**它当时看到的上下文**编号，旧实现 `ContextBuffer.quote_target()` 却在生成结束后对**实时** buffer 求值 —— 生成窗口（5–8s）内每进 1 条消息编号整体偏移 1 位（该群 100–400 条/时 → 错位概率约 1/3–1/2）。修复：新增 `QuoteIndex`（**不可变引用快照**）+ `SessionManager.quote_snapshot()`，在**建上下文之后、发起生成之前**冻结编号基，生成结束后对它求值；`message_id`/`sender_uin` 随条目入 session（内部键 `_mid`/`_uin`，所有对外出口剥离，**绝不进 LLM 请求**）。附带消除次要错位：编号基与 `get_contexts()` **同源同过滤**（旧实现 session 含 assistant 条目而 buffer 不含，且 buffer 含被媒体过滤掉的纯图消息）。新增审计字段 `trace.quote_n/quote_id/quote_resolved/quote_basis/quote_target_alias/quote_target_uin/quote_target_missing` —— 此前 trace **没有** quote 字段，11:45 那次只能靠人工比对 SnowLuma 日志
- **⚠️ 顺带修掉一个标记泄漏**：旧代码只在 `quote_id` 非空时剥离 `[r:-N]`，解析失败时会把标记**原样发进群**（线上实际发生过）。现在无论是否解析出目标都剥离
- **🔴 B-002 空 `content` → LLM 永久 400 → 静默不回复**：唯一收口点 `SessionManager` 过滤空白 content（`get_messages`/`recent`/`quote_entries` 同过滤），正常会话零行为变化；恢复期丢弃计数入 `empty_dropped_on_load()`，运行期 `trace.session_empty_dropped`

### Changed (2026-09-13, 前缀缓存重排 —— system prompt 恒定化)
- **时间句与心情挪出 system prompt**：`system_prompt()` 原先在**末尾**拼「现在本地时间 HH 时」（每小时变）以及调用方追加的 `current_mood`（有值时 30s 变）。system prompt 是请求的**第一个 token 位置**，它一变其后整段会话前缀（可达上千条）**全部 miss**。新增 `StyleProfile.volatile_line(local_hour, mood)`，改由 `main._generate_reply` 插到**上下文末尾**（speaker 行之后、KG 尾注之前）→ 只破坏尾部，稳定前缀（人设 + 会话 + 示例块）得以复用。`tools/replay_scene.py` 同步（防线上/离线漂移）
- **`reasoning_effort` 语义澄清**：网关**不认 `off`** —— 填 off 只是"不发送该参数"，模型仍按**默认档**思考（实测 651–738 思考 token / 4–8s），**并不能真的关掉思考**。全栈默认改 `low`（思考降至 136–227 token）；`vision.reasoning_effort` 另有硬约束：vision 系列**没有默认档**，传 off 直接 HTTP 400（只接受 low/medium/high/xhigh/max）
- **工具意图标记预防性收口**：`text_style.postprocess` 增加 `[emote:…]`/`[poke:…]` 剥离规则。当前提示词尚未教这两个协议，但**没有剥离规则就教协议 = 标记原样进群**，故先兜住
- `_conf_schema.json` 同步：`vision.model`、`emotion.{timeout_sec,reasoning_effort}`、`gate.{timeout_sec,reasoning_effort}`、`llm.reasoning_effort` 默认值与 hint 全部改为实测口径（含 400 错误原因）
- 测试 225 → **257 全绿**（新增：QuoteIndex 冻结不变量 5、session 元数据/自愈 9、pipeline 引用快照隔离 4、emotion 降级可分辨 3、style 缓存恒定 3、provider 回退 8）

### Fixed (2026-09-10, 协议端迁移准备：poke 死代码 + LLBot 出站通道)
- **🔴 poke 处理器是死代码（G11 开启也不会生效）**：`on_other` 挂了 `EventMessageType.OTHER_MESSAGE` 过滤器，但 AstrBot **v4.27.4** 的 `_convert_handle_notice_event()` 会把**带 group_id 的通知**归为 `GROUP_MESSAGE`（`OTHER_MESSAGE` 在该路径上不可达）→ 群戳从未进入该处理器，且无任何报错。更隐蔽的是：群戳实际落到 `on_group_message`，`message_str` 为空 → 被媒体过滤器 `event.stop_event()` 吞掉；而 `StarRequestSubStage` 的派发循环是 `for handler in activated_handlers: if event.is_stopped(): break`、handler 顺序 = 装饰器注册顺序（`on_group_message` 在前）→ **单改过滤器也救不回来**。修复：① `on_group_message` 对 `post_type != "message"` 的通知**让路**（不 stop_event）；② 处理函数改名 `on_notice`，过滤器改 `EventMessageType.ALL`，判定下沉到插件侧。依据：AstrBot v4.27.4 源码实测（`_convert_handle_notice_event` / `get_message_type` / `EventMessageTypeFilter` / `StarRequestSubStage` / `PipelineScheduler._process_stages`）
- **回戳通道修正（LLBot v8 上必定失效）**：原实现 `yield event.chain_result([Comp.Poke(id=poker)])` 走 `send_group_msg` + `{"type":"poke"}` 消息段，而 LLBot v8 的出站转换表（`src/onebot11/transform/message/outgoing.ts`）**没有 poke 分支也没有 default** → **静默丢弃**；NapCat 的 poke 段转换器同样是 `async () => undefined` 空实现桩。修复：改为 action 优先 `group_poke`（LLBot `action/llbot/group/GroupPoke.ts` / NapCat 同名），失败再回退消息段
- **通知事件不再被内置 LLM 兜底作答**：认领「戳向本机器人」的通知后一律 `event.stop_event()`（私聊通知会置 `is_at_or_wake_command=True`，不拦会触发 AstrBot 内置 LLM 对空消息作答）；⚠️ 段通道**不能**先 stop_event 再 yield —— `PipelineScheduler` 在 `async for _ in agen` 里**先判 `is_stopped()` 再递归执行后续阶段**，先停会阻断发送。戳一戳撤回（`sub_type=poke_recall`）明确忽略

### Added (2026-09-10, 协议端迁移准备)
- **`services/protocol_compat.py`**：协议归一化层（纯 stdlib，无 astrbot 依赖）。① `normalize_poke(raw)` 吸收各实现形状变体（`notice_type=notify/poke`、缺 `notice_type`、`poke_recall`），拒绝非 poke 通知与无效/为 0 的 QQ 号（LLBot `target_id` 默认 0 = 未设置）；② `ProtocolCapabilities` + `poke_channels()` 把「走哪条通道」变成可测数据（LLBot/NapCat 均无 poke 段 → action 优先；未知协议端 action + 段回退）
- **`tools/llbot_config.py`**：从 AstrBot `cmd_config.json` 推导 LLBot v8 的 OneBot11 反向 WS 配置（`ws://<host>:<port>/ws` + `messageFormat=array`），支持打印（token 掩码）/ `--out` 片段导出（0600）/ `--merge-into` 就地 upsert（备份 + 原子写 + 保留其它 connect 条目）/ `--check` 一致性校验。BOM 兼容读取；token 绝不回显
- **`_conf_schema.json` 新增 `poke.protocol`**（默认空 = 自动）：适配器名恒为 `aiocqhttp`，无法自动识别协议端，需人工声明 llbot/napcat 以跳过注定被丢弃的段通道
- 测试 171 → **225 全绿**（`test_protocol_compat.py` 23 例 + `test_llbot_config.py` 31 例）
- 迁移计划书 `docs/specs/llbot-migration-plan.md`；调研报告 `data_out/llbot_onebot11_compat_research.md`、`data_out/llbot_plugin_platform_audit.md`

### Fixed (2026-09-10, A7④ 复盘：致命 reasoning bug + RAG 语料 + 预算)
- **🔴 `reasoning_effort` 致命 bug（A7 上线会让 bot 全哑）**：旧实现 off→`"none"`，实测 commandcode 网关（OpenAI 兼容 `/chat/completions`）**拒绝任何非标准取值**（none/off/minimal/disabled/false 全部 HTTP 400）→ `_generate_reply` 的 try/except 吞掉 → 返回空 → 静默。修复：`reasoning_value()` 对 off/未知返回 **None（=不发送该参数）**，仅 low/medium/high/max 透传（佐证：dsh 自身配置 `reasoningEfforts: {False: None}` 即此语义）；main.py RP/Emotion/Gate 三处在 None 时跳过 kwarg；`tools/replay_scene._chat` 同步
- **`max_tokens` 256 → 512**：v4-flash 下思考开销（实测 141-256 reasoning tokens）会吃光预算 → content 为空（重放实测 14 次 empty generation，修复后 **0 次**）；`_conf_schema` 默认值与 hint 同步
- **RAG 语料清洗（相关性差的主因）**：`build_dataset` 从未过滤图片占位——merge 把图片消息渲染成 `[图片: xxx.jpg]` 当文本提取，**36% 的 reply 是纯图占位**、62% 的 context 含图行，导致 RAG 命中"看不出相关性"。新增 `tools/clean_pairs.py`（删纯图 reply 对 + context 图行→`[图]` 标记 + 长度过滤）；7075 → **4704 对**，chroma 已重建（部署机 48 分钟）
- **hourly_budget 宽松化（用户实测校准）**：`analyze_style` 的 `daily_budget` 是 v0.1 随手拍的 24（注释 "conservative"），从未按真实活跃度校准 → 低谷小时预算 <1（17 点 0.51 / 20 点 0.0）而每次 reply 消耗整数 1 → **结构性每小时 ≤1 条、低峰静音**。改为可配 `--daily-budget`（默认 **2400**，= 用户 2026 实测单群日均 200+ × 12 倍裕度），预算降级为"宽松保险丝"，真实节奏交给决策门槛；两台机数据已从 raw count 精算同步（17 点 0.51→**50.54**）。重放实测 reply 3 → 57（预算不再压制，min_gap 25s 接管节奏）
- **重放 recency 虚高（评测口径）**：重放虚拟时钟推到场景时刻（2025-08）而语料为 2025 全年 → recency 满分 1.0，总分虚高（0.67 vs 真实时钟 0.50）。**已知口径差异**，评测时以 dense 分为准或标注时钟；阈值定 0.65（新语料下 p75≈0.644）

### Added (2026-09-10, A7④ 工具与选样)
- **`tools/find_style_windows.py`**：滑动窗口 + 最小堆找风格源最活跃的 1h 窗口 top-N（O(n) 双指针 + 前缀和统计文字/图占比；避免人工盲找全年 86 万条）。产出 5 个候选窗口（w1 08-13 16:43 最佳：55 条纯文字、群 344 条活跃）
- **`tools/replay_scene.py` extract 第二级支持 `--merge` 重建**：draft 行只有显示名（风格源昵称是不可见字符、无昵称者显示数字 QQ），run 靠 name 反查 member_relations 只能覆盖 3/30 人 → 发言人识别失真。给 `--from-draft` 加 `--merge`：以 draft 首末行时间戳为界流式重扫 merge 重建场景（带 `sender.uin/name/message_id`）；无 `--merge` 保持原文本解析
- **首个正式场景**：`data_out/scene_20250813_w1.json`（357 条，16:40:57–17:44:41 UTC+8，含 13% 群 bot 消息——真实场景本就有，不剔除）
- 测试 169 → **171 全绿**（`test_llm_params` 重写为 None 哨兵语义 + extract merge 重建 1 例）

### Added (2026-09-09, A7 批次⑥：离线测试台 replay_scene — A7④)
- **tools/replay_scene.py 双子命令**：
  - `extract`（开发机，两级选样）：第一级 ijson 流式抽 merge.json 时间窗（UTC+8，`--start/--end`）→ 小文件（`[2025-10-07 20:03:12][小红]: 内容`，多行消息压 `⏎`，风格源即普通群友无特殊标记）；第二级 `--from-draft + --range "N..M"` 按行号截取 → 场景 JSON（`{scene_id, group_id, messages[{ts epoch, uin, name, text}], meta}`）
  - `run`（部署机，真 services 重放）：读场景 + 生产插件数据目录 + cmd_config → import services/* 自组构造（不 import main.py）→ 按场景时间线每条消息喂 PersonaPipeline（硬闸/Gate 决定回不回，回了才生成，与线上同决策）→ dsh 风格日志（`# replay:` 头 + `━━━ [#n] 时间 别名: 原文 ━━━` + 【决策】reply/silent/topic + RAG top + 回复）
- **凭据/模型跟随（不硬编码 provider id）**：读 AstrBot cmd_config `provider_sources` 第一个 enable chat provider 的 api_base/key（key 只内存，绝不落盘/打印）；model = CLI `--model` > env `PERSONA_TEST_MODEL`；插件 config 读 `data/config/`（`--plugin-config` 覆盖）
- **隔离红线**：重放 session/usages/logs 全写 `--tmp`（默认 out 同目录 `replay_tmp_<scene_id>`）；只读生产风格/chroma；`memory_store.db`（±wal/shm）复制到 tmp 副本再打开（重放中 ingest 自积累、生产零写）
- **services 时钟注入（A7④ 重放语义）**：pipeline/gate/emotion 加可选 `now_utc_fn`（缺省 time.time，main 不传行为不变）——重放虚拟时钟推到消息时刻，硬闸预算/冷却、Gate 节流、情绪缓存、RAG recency 按"当时"决策
- CLI 开关：`--rag-off` / `--no-gate` / `--gate-enabled`（A/B 对照）；RAG/示例开关同 config
- 补 `tests/test_replay_scene.py`（14 例：extract 时间窗/草稿行/range 截取/场景 JSON；run fake services：逐条走 pipeline/硬闸与 gate silent 不生成/回复续 session/生产零写/日志渲染）；测试 155 → **169 全绿**
- 设计文档：`docs/specs/a7-step4-replay-scene.md`（已同步落地细节与冒烟结果）

### Added (2026-09-08, A7 批次⑤：LLM 参数矩阵 + Gate 安全阀化 — A7④)
- **LLM 参数矩阵**: `_conf_schema` 加 `llm.reasoning_effort`(默认 off)、`gate.temperature`(0.2)+`gate.reasoning_effort`(off)、`emotion.temperature`(0.2)+`emotion.reasoning_effort`(off)；`services/llm_params.py` `reasoning_value()` 容错映射（off→none，未知→none）；RP/Gate/Emotion 三处 `llm_generate` 透传 temperature+reasoning_effort（采样参数不进 prompt，不破坏前缀缓存）；Diary/Summary 跟随 RP（同 system_prompt）
- **Gate 安全阀化（conflict 并入）**: Gate 单 prompt 双任务（参与度 reply + 安全 conflict），输出 `{reply, conflict, reason}`；conflict=true → 强制不发言（防煽风点火）；**@ 也过 Gate**（@ 提示通常应回但冲突除外；缓存键含 is_at 不串）；**TOPIC 也过 Gate**（刚吵完的冷场不主动惹事）；Gate 节流窗口内冲突结果复用
- **conflict_detector 降级为兜底**: `gate.enabled=1` 时旧 detector（keyword+burst+verify）不跑（Gate 语义判定每次候选都查，无 keyword 漏检）；`gate.enabled=0` 时保留旧 detector（避免关 Gate 连带失去冲突保护）
- **通知旁路工具化**: Gate conflict → main 从 session recent 取上下文 → `_notify_admin`（30min 冷却防刷屏，仅通知侧不影响发言闸）
- 补测试：gate conflict 5 例（解析/强制 false/旧格式兼容/@ 缓存键）+ pipeline topic-gate 2 例 + @ gate 语义改造；测试 147 → **155 全绿**
- 设计文档：`docs/specs/a7-step4-llm-params-gate-conflict.md`

### Added (2026-09-07, A7 批次④：RAG 动态开关 + 单次检索 + 图片持久哈希 — A7③)
- **`rag.enabled` 总开关**: `_conf_schema.json` `rag` 块新增顶层 `enabled`（默认 1）。=0 时 pipeline 完全不查向量库（决策无 RAG 分数 → rag_hit 不触发、Gate 无参考片段、trace 记 `rag_disabled`），用于 A/B 验证 RAG 价值。`/reload_persona_config` 重建 pipeline 即时生效（抽 `_build_pipeline()`）
- **KG 退化分支（B1）**: `rag.enabled=0` 时 KG 不注入历史话术示例（memory_store 只存实体碎片无完整发言，BM25 无法顶替 dense 候选池——实现中发现原方案 B 数据不可行），保留关系块（互动次数/关系等级/共同话题，edges 表不依赖 BGE）；无关系块则返回 None。`MultiSignalKGProvider` 加 `dense_enabled` 开关
- **单次检索复用**: pipeline 一次查全量（`top_n_final=k` 拿回 k 条），截 top3 供决策/Gate，**全量传 KGProvider**（`query(ctx, external_dense_hits=...)`）→ KG 不自查 rag（每轮一次 BGE 编码+Chroma 查询，原两次）；不传 external 时 KG fallback 自查（向后兼容）
- **trace 记全量 RAG 命中**: trace["rag"] 记录全部命中原文+score（"LLM 看到什么 trace 记什么"，评估 RAG 价值；规模可控，RAG 确认保留后可改回截断）
- **Vision 持久 LRU 哈希缓存**: VisionService 加持久层 `data_dir/image_desc_cache.json`（sha256→{desc,last_ts,hits}，跨重启复用图片描述）；LRU 淘汰最久未命中（命中刷新 last_ts+hits，高频表情不被早进缓存误杀）；上限 `vision.cache_persist_max`（默认 2000）；落盘节流标脏 + terminate flush；`snapshot()` 暴露 size/max/evicted/hits 分布（观测 2000 是否够）；`vision.cache_persist=0` 关闭仅内存 TTL
- 补测试：vision LRU 4 例 + kg 退化 4 例 + pipeline rag 3 例；测试 132 → **143 全绿**
- 设计文档：`docs/specs/a7-step3-rag-switch-single-query-image-hash.md`

### Added (2026-09-07, A7 批次③：日志分群 + topic_bank 隔离 + trace_view — 2b)
- **日志按群分目录**: decision/gate/trace/llm_cache_probe/daily_diary 全部迁移到 `logs/<group_id>/` 子目录（JsonStore append_jsonl 自动建子目录）；旧根目录文件保留兼容；housekeeping 轮转扩展覆盖 `logs/*/*.jsonl`
- **gate_log 恢复独立落盘**: 2a 重构曾把 gate 决策并入 decision_log.extra，2b 恢复 `logs/<group>/gate_log.jsonl` 独立记录（含 decision_action/trigger/fallback/cached）
- **TopicBank 群隔离**: 已发送归档 `topic_sent.json` 按 group_id 键控（pick/mark_sent 带 group_id；话题池仍全群共享；旧无 group_id 记录归 legacy 不参与排除——本仓从未启用无迁移负担）
- **tools/trace_view.py**: trace_log.jsonl 渲染工具（`python -m tools.trace_view --data-dir <dir> [--group] [--limit]`），输出层级可读报告（入站/硬闸/GateLLM/RAG 命中原文/情绪/KG/session/生成/最终），dsh 内直接查看
- 补 topic_bank 群隔离 2 测试；测试 130 → **132 全绿**

### Added (2026-09-07, A7 批次②：pipeline 抽取 2a — 共享主链路 + trace 落盘)
- **PersonaPipeline 共享主链路**: 新增 `services/pipeline.py` — 把 RAG→emotion→硬闸→GateLLM→KG→contexts→生成→postprocess→quote 抽成纯数据编排（构造注入 services + generate 回调 + 可选 topic_handler），返回 `SendIntent`（action/text/quote_id/sticker_prompt + 预留 emote/poke 工具字段 + 全链路 trace dict）。**线上 main 与离线测试台共用同一份代码**，防逻辑漂移
- **main.py on_group_message 瘦身**: 主链路改调 pipeline；前置副作用（睡眠/记忆/冲突/新人）留 main；发送按 SendIntent 翻译（含 [r:-N] quote 链）；`_send_topic` 冷场路径保留（从 trace.hard_gate 重建轻量 Decision）；`_generate_reply` 支持 standalone（speaker_uin/umo 覆盖，event=None 可离线用）
- **trace_log.jsonl 落盘**: 每轮处理全链路记录（input/rag命中原文/hard_gate/gate/emotion/kg_tail/temperature/raw_generation/final_text），调用方（main）落盘，`trace.enabled=1` 默认开（评估 RAG 价值的数据依据）；decision_log 保持兼容输出
- **修复**: quote 提取必须在 postprocess 前（真实 postprocess 会剥 [r:-N]，2026-09-07 bug + 回归测试）；锁语义修正（topic/silent 分支在 finally 释放后处理，避免 _send_topic 嵌套释放）
- 补 `tests/test_pipeline.py`（12 例）；测试 118 → **130 全绿**；`_conf_schema.json` 新增 `trace` 块

### Added (2026-09-07, A7 批次①：GateLLM 决策层 + 群隔离改造)
- **A7 GateLLM 决策层（双层 LLM 上层闸）**: 新增 `services/gate.py` — 独立非角色 LLM 判断「这句要不要回」；仅规则硬闸内、非 @ 消息才调用（@ 必回不 gate）；中性分析师视角，输入最近 N 条 + RAG 风格片段，输出 `{reply, reason}`；同群决策节流窗口 `decide_cooldown_sec`（复用缓存）；失败/超时/坏 JSON → 保守静默降级、绝不抛出。`gate.enabled=0` 默认关（手动开），`_conf_schema.json` 新增 `gate` 块（timeout_sec/decide_cooldown_sec/recent_n/max_rag_hits）。main.py `on_group_message` 接线 + 每次决策落 `gate_log.jsonl`（含 fallback/cached/trigger）
- **群隔离改造（A7 review 结论）**: `InterjectionManager` 用量状态（last_reply_ts / current_hour / hourly_used / at-cooldown）从全局单例改为**按 group_id 隔离**，持久化到 `usages/<group_id>.json`（JsonStore 原子写 + mtime 热重载 + `/reload_persona_config` 生效）；`JsonStore` 通用支持子目录自动创建。poke 保持跨群共享（QQ 拍一拍按目标用户限，现状正确，不改）
- 补 `tests/test_interjection.py`（12 例：群隔离/持久化/重载/回归语义）；`tests/test_gate.py`（18 例）；测试 88 → **118 全绿**

### Added (2026-08-25, A1-A3 批次；生产切换 + 配置同步 + 第三批功能)
- **A1 生产切换（test_mode=0）**: 目标群 123456789 接管，测试群不再由插件处理；ready 日志验证 + cache_probe 全部落在生产群；02:00 后核心兜底「LLM 响应错误」广播 0 次（含图片消息路径）
- **A2 配置同步工具**: 新增 `tools/sync_config.py` — 按 `_conf_schema.json` 默认值只补缺失键、保留现有值（红线 #3）；UTF-8 BOM 兼容读写；写入前自动备份 `.bak.<ts>`；原子写（.tmp → rename）；默认 check 模式，`--write` 生效；12 例单测。桌面配置实测 in-sync（22 顶层键全含，added=0）
- **G11 Poke 戳一戳响应**: `services/poke.py` + `on_other` 接线 — OneBot notify/poke 解码；仅目标群 + target=bot 才考虑；同人 300s 冷却（`poke.cooldown_sec` 可配）、全局小时配额 4、未知关系成员默认不回戳、`conflict_keywords.json` 命中抑制（mtime 热重载，严肃上下文不回戳）、`poke_log.jsonl` 留痕；`poke.enabled=0` 全静默（Day4 按 DEPLOYMENT_GUIDE 开启）
- **G12 TopicBank 主动话题**: `services/topic_bank.py` + ACTION_TOPIC 接线 — IMPLEMENTATION_PLAN §10 评分（0.45·silence + 0.25·priority + 0.20·context_hints + 0.10·freshness）；`topic_bank.json` mtime_ns 热加载；发送后归档 `topic_sent.json`（追加、原子写）；无可发话题绝对沉默；冷场触发走 `llm.temperature.cold_start=1.1` LLM 改写 + `context.send_message` 主动发送（group UMO）；决策日志 extra.topic_id/sent；建议稿 `data_out/topic_bank.json`（8 条，人工可改，历史坏例置 enabled=false 即规避）；`topic_bank.enabled=0` 不触发（Day3 开启）
- **G13 周/月摘要金字塔**: `services/summary.py` + cron（周一 02:10 / 月首 02:15 CST）— 聚合日日记 `daily_diary.jsonl`，每个归档日从 session_<group>_<day>.json 抽样 ≤6 条 user 原文防失真；输出 `weekly_summary.jsonl` / `monthly_summary.jsonl`（UTF-8 原子追加）并通过 dream_binding 私聊推送（未绑定仅落盘）；`summary.{weekly_enabled,monthly_enabled,max_sample_messages,max_summary_chars}` 可配（默认关，本次部署已开）
- **G16 插话质量门**: 60 条生产消息真实 RAG 实播（chroma+BGE）：p50=0.436 / p75=0.504 / p90=0.573 / p95=0.676 / max=0.698 → 阈值定 0.65（≈5% 触发率）；`active_interjection=1` + `rag.score_threshold=0.65` 已生效（02:42 重启加载）；决策日志全分支落盘 `extra.top_rag_score` + `emotion_multiplier`（阈值可数据驱动调优）
- **G17 dream 启用**: DreamJob 直跑验收（等价 /dream_now 特权路径）：90 天 800 edges / 21 成员分析 / 10 话题趋势 → `style_drift_report.json`（3.7KB）；`dream.enabled=1` + 周一 03:00 cron 已注册；`dream_binding.json` 预绑定 `aiocqhttp:FriendMessage:234567`（等效 /bind_dream，运行时可改）

### Changed
- `_conf_schema.json` 新增 `summary` 节（weekly_enabled/monthly_enabled/max_sample_messages/max_summary_chars，默认 0）
- `/persona_status` 增加 summary 开关行；测试 46 → 88 全绿；`main.py` 初始化新增 PokeService / TopicBank / SummaryService

### Fixed
- 决策日志缺失 RAG 分数：主动插话评估（G16）前 silent 分支不记录 top_rag_score，现每个 decision entry 的 extra 落盘原始分与情绪乘数
- **smoke_rag --real / 独立脚本 BGE 加载卡死根因（2026-08-25 修复，d218654）**: SentenceTransformer(local_files_only=True) 仍会经 model_card.set_base_model → huggingface_hub.model_info() 联网校验；huggingface.co 不可达时 TCP connect 挂起数分钟（faulthandler SIGABRT 栈定位）。修复：rag_service._ensure_backend() setdefault HF_HUB_OFFLINE=1 与 TRANSFORMERS_OFFLINE=1 双保险（不覆盖宿主已导出的值）。验证：smoke_rag --real 连续两次全绿（count=7075，scenarios_failed=0）；与 2026-08-22 看门狗事故同一根因的更深一层防御

### Changed/Fixed (G15 实测修正)
- 视觉模型定稿（2026-08-24 实测）：默认 `deepseek-v4-flash-vision-exp` + `reasoning_effort=low` + `max_tokens=512`（思考不挤占输出；与本地 dsh settings.yaml 的视觉模型声明一致）。附录：mimo-v2.5 在 text-first 载荷下也能返回图像描述，但网关/dsh 均未声明其 image 输入，弃用
- 视觉模型默认改为 `deepseek-v4-flash-vision-exp`（实测 mimo-v2.5 视觉返回空 content；flash-vision-exp 可准确描述）
- 描述失败时注入「（配图：无法识别）」诚实占位，杜绝主 LLM 对未见图凭空猜“芳乃”类幻觉
- 事件循环防泄漏加固：生成锁分支 / LLM 失败 / 空回复三条早退路径补 `stop_event()`，避免 AstrBot 核心兜底回复将错误文本广播到群
- 说明：此前「LLM 响应错误」外泄为 AstrBot 核心默认 agent 行为（非目标群未处理消息 + 图片多模态调用失败），非插件代码泄漏；生产切换（test_mode=0）后目标群由插件接管可避免，或在 AstrBot WebUI 配置 `provider_ltm_settings.image_caption_provider_id` 指定视觉模型
### Fixed
- **AstrBot 占位标记泄漏**：`_clean_message_text`/`_postprocess` 新增剥离 `[图片: 文件名]`、`[表情...]`、`[ComponentType.X]`（实测 02-21 回复中图片文件名被模型复读的根因）
- **指令别名**：`/persona_awake` 与 `/persona_wake` 均可唤醒（此前仅后者注册，实测名字不匹配导致无响应）
### Added
- **睡眠窗即时开关（2026-08-24）**: `/persona_wake`（特权，测试期唤醒）/ `/persona_sleep`（恢复），内存态即时生效免重启；`_is_sleeping()` 改为每消息热读配置（WebUI 改 sleep.* 亦即时生效）
### Fixed
- **G14 A/B 首轮实证精修（2026-08-24）**: 注入块头部加「规则A/B」——钨钼钨钼 仅限肯定/恍然大悟（Phase1 实测泛滥至 4/10 次）；谐音问候仅整词触发（实测“鸡没醒”被误回枣商蚝~）
### Added
- **G14 静态注入落地（2026-08-24）**: `services/examples.py`（纳秒 mtime 热重载，A/B=改文件名零重启）；注入位置=会话与 KG 尾之间（内容恒定，前缀缓存稳定）；`examples.{enabled,max_entries}` 可配；终稿示例 13 条（人工评审 v2 全量并入：极简单发/暴力萌/胡言乱语/无括号动作），文件 `data_out/example_dialogs.json`（旧版已备份 .bak.20260824）；测试 +4（46 全绿）
### Added
- **G15 识图能力（2026-08-23 全量实现）**:
  - `services/vision.py`：图片/gif 表情 → 视觉模型（默认 mimo-v2.5，同网关同 key，懒解析不落密钥）→ ≤120 字中文描述；三源解析（convert_to_file_path 统一处理本地/url/base64 + 手动兜底）；gif/jpeg/png/webp mime 嗅探；30s 同图 hash 缓存；15s 超时与失败降级（不影响回复链路）
  - QQ Face（系统表情）→ 本地 id→名称映射表（零成本）
  - 描述文本并入消息内容进会话（空文本消息因此可被“看见”）；`vision.{enabled,model,timeout_sec,cache_ttl_sec,max_images,desc_max_chars}` 可配
  - 测试 +8 例（face 映射/mime 嗅探/base64 解析/缓存/载荷/超时/空描述）
- **G14 候选池筛选脚本（tools/select_examples.py）**: 7075 对 → 规则过滤 → 8 场景分桶 → 离线排序 → 可选 LLM 人设打分（≥4 保留）→ 输出候选池文件（不触碰 example_dialogs.json，人工抽查后才合并）
- **第二批功能缺口修复（G7-G10, 2026-08-23）**:
  - G7 温度分档：`llm.temperature.{at_reply 0.8, active_interjection 1.0, cold_start 1.1}`（dream 档延后至 dream 具备 LLM 步骤），按 decision.trigger 透传 `llm_generate(**kwargs)`
  - G8 特权 QQ：`privileged_qq`（默认风格源），`/dream_now` 免 dream.enabled 开关
  - G9 新人自动入列：`StyleProfile.add_new_member()` 原子追加（只增不改、昵称空/冲突回退 群友+uin、auto_added/first_seen 标记），未知 uin 异步触发
  - G10 LLM 情绪引擎 v1：`LLMEmotionProvider`（willingness/mood/sticker 三维，30s 同群缓存，3s 超时回退中性，JSON 解析+钳制 0.3-1.5）；`emotion.{enabled,timeout_sec,cache_ttl_sec}` 可配；willingness 调制插话、mood 注入 system prompt 尾、sticker 触发表情包发送
  - 测试 +10 例（情绪解析/钳制/缓存/超时降级、add_new_member 幂等/回退），累计 33 例
- **第一批功能缺口修复（G1-G4, 2026-08-23）**:
  - G1: RAG 查询移出事件循环 —— 决策阶段 `rag.query` 与 KG dense 检索改 `asyncio.to_thread`（BGE 编码不再冻结所有群）
  - G2: 引用回复实现 —— `[r:-N]` 标记解析（`services/text_style.py::extract_quote`，`-1`=最新消息）→ `ContextBuffer.quote_target(n)` 查 message_id → `Comp.Reply` 构造 OneBot reply chain；查不到则降级纯文本
  - G3: 数据保留策略 —— `housekeeping` 配置（session_keep_days=3, jsonl_max_mb=50）：过期按日会话文件清理 + jsonl 轮转（.1/.2），初始化与每日 cron 各跑一次
  - G4: 单元测试落地 —— `tests/`（unittest 零依赖，23 例）：text_style 全链路（引用标记/换行/口癖/AI 味/emoji/长度）、SessionManager 日界/轮换/按日恢复/旧格式兼容/无上限/clear、ContextBuffer quote_target
  - 重构：文本处理纯函数抽取到 `services/text_style.py`（无 astrbot 依赖，可离线测试），main.py 薄委托
- **v3 按日会话 + 02:00 轮换 + 睡眠窗（2026-08-23 设计定稿）**:
  - SessionManager 取消 300 条硬上限（按日轮换天然定界，1M 窗口内自由增长）；`session_<group>_<day>.json` 按日落盘，启动恢复当日窗口
  - 睡眠窗默认 02:00–07:00（可配）：全静默（含 @ 不回复、无插话），消息照常入会话与记忆；轮换时刻 02:00 落在窗内，重建/缓存重构无感
  - 每日日记：02:05 cron（+消息触发兜底）轮换时以**当日会话原文为上下文**生成摘要（前缀与末次聊天请求一致 → 缓存命中），写入 daily_diary.jsonl（原死路径正式接通）
  - 媒体消息过滤：无文本消息（转发/纯图片/表情）不进会话与摘要（识图能力为后续设计项）
  - 会话持久化落盘路径改为按日文件名，`rotate_if_day_changed()` 归档旧日并返回供日记使用
- **会话持久化（v0.2 §4.2 轻量落地）**: `session_manager.py` 每 ~50 条或 ~5 分钟原子落盘 `session_<group>.json`，启动时 `load_all()` 恢复（恢复数记入日志）；`clear()` 同步删文件。重启不再丢上下文，前缀缓存免全量重建
- **RAG 启动预热**: `rag_service.warmup()` 在 `initialize()` 内 `asyncio.to_thread` 预载 BGE + chroma，杜绝首条群消息触发懒初始化阻塞事件循环（2026-08-22 看门狗事故根因）
- **引用标记剥离**: `_postprocess` 增加 `[回复...]`/`[r:-N]` 标记剥离（`_RE_REPLY_MARKER`），消灭 `[回复dog]` 类工件外泄；完整 OneBot reply-chain 引用回复仍属 v0.2 §4.9 未实现项
- **`llm.cache_probe_enabled` 开关**: 探针转正为 config 控制（默认 1），可关停
- **当前说话人动态行（命名识别修复）**: `_generate_reply` 在 KG 前注入 `【当前说话人】QQ xxx，群内别名「yyy」…` 提示（每说话人恒定、不污染前缀缓存），并要求不臆造他人别名；根治 2026-08-23 实测「叫不上名字/误称老狗内桑」类缺口
- **member_relations 风格源标注**: 小明条目增加 `is_style_source: true`（不影响别名块渲染，系统提示词哈希实测不变，缓存不失效）
- **换行折叠（postprocess 强制执行 prompt 禁令）**: 回复中的多段换行按标点感知合并为单行（`x\n\ny` → `x，y`），修复 2026-08-23 实测 6/6 回复带换行的系统性违规
- **member_relations.json 补风格源条目**: `234567 → 小明/小明玛奇朵`（人工格式追加，携 `.bak.20260822` 备份；修正此前机器人臆造他人别名「老狗/内桑」称呼的根因）
- **LLM 缓存观测探针 (2026-08 评测用)**: `_generate_reply` 每次生成追加一行 `llm_cache_probe.jsonl`（session 长度 / sys prompt sha256 前缀 / usage 缓存命中 / raw usage），仅观测用途，失败仅 warning 不影响回复
- **DreamJob (Phase 3)**: 周 cron 记忆巩固与风格漂移报告
  - `_build_member_stats`: 从 MemoryStore 边计算活跃天数/日均互动/连续天数
  - `_data_closeness`: 纯数据驱动亲密度分级 (new/known/close)
  - `_suggest_upgrades`: 自动建议升级 (>=60天→known, >=90天→close)
  - `_suggest_downgrades`: >=90天无互动 → 建议降级(需人工确认)
  - `_detect_topic_trends`: 话题趋势周环比
  - 输出 `style_drift_report.json` (原子写, 不覆盖人工文件)
- **ConflictDetector**: 三阶段冲突检测 (关键词 → burst → LLM 语义确认)
  - `conflict_keywords.json`: 人工可编辑冲突关键词库 (mtime 热重载)
  - `/bind_admin`: 绑定管理员私聊会话, 冲突时推送通知
  - 30min 冷却, 检测到冲突自动 `stop_event`
- **system_prompt_fragments 增强**:
  - `vocabulary` 重写: 口癖与表达习惯分离, 新增群内术语解释
  - 新增 `group_context` 字段: 群介绍 + 群规
  - 新增 `personality` 字段: 人物性格描述
- **`/bind_admin`**: 独立的冲突通知绑定, 与 dream_binding 解耦
  - `_build_member_stats`: 从 MemoryStore 边计算活跃天数/日均互动/连续天数
  - `_data_closeness`: 纯数据驱动亲密度分级 (new/known/close)
  - `_suggest_upgrades`: 自动建议升级 (>=60天→known, >=90天→close)
  - `_suggest_downgrades`: >=90天无互动 → 建议降级(需人工确认)
  - `_detect_topic_trends`: 话题趋势周环比
  - 输出 `style_drift_report.json` (原子写, 不覆盖人工文件)
- **MultiSignalKGProvider (Phase 2)**: 多信号融合检索替代 RagKGProvider
  - dense(BGE向量) + BM25(FTS5关键词) + entity(alias实体匹配) 三路加权融合
  - graph augmentation: 注入说话人与 bot 的互动模式 (closeness tier, 共同话题)
  - 保留 _format 接口: 结构化风格指引注入 contexts 末尾
- **EmotionProvider.query()** 新增 `kg_ctx: Optional[KGContext]` 参数 (Phase 3 兼容)
- **MemoryStore (Phase 1)**: SQLite-backed ADD-only entity + relation graph with FTS5 BM25 search
  - Entity extraction: jieba keywords + @mention detection + alias matching
  - ADD-only edges: never overwrite, append with timestamp for temporal decay
  - `get_relation(from, to)` → interaction count, closeness tier, common topics
  - `get_hot_topics(group)` → trending topics in last 7 days
  - `search_bm25(text)` → FTS5 full-text keyword search
  - fire-and-forget `ingest()` in main.py via `asyncio.to_thread()`
- 三层 AI 记忆架构设计 (检索层 + 存储层 + 进化层)
- DreamJob 周 cron 关系漂移检测 (设计阶段)

### Changed
- `system_prompt_fragments.json`: vocabulary 重写 (口癖与术语分离), 新增 group_context / personality
- `style_profile.py`: `system_prompt()` 增加 group_context / personality key; `_build_alias_block()` 跳过 bot
- `member_relations.json`: 星野 / 苗爷 标记为 bot
- `main.py` `_KOUPI_LIST`: 移除 `汪汪`(人名), 新增 `捏猫猫的`
- `DEPLOYMENT_GUIDE.md`: §8 重写为 git pull 工作流 + DreamJob cron + style 文件同步
- PostgreSQL 图表替代 pure ChromaDB 的存储方案规划

### Fixed
- **requirements.txt**: `chromadb` / `sentence-transformers` 由 `>=` 钉死为 `==0.5.23` / `==3.3.1`
  - 与预构建 RAG 产物（0.5.23 构建）及 AGENTS.md 文档对齐，避免 pip 解析到 chromadb 1.x 导致产物不可读
- **tools/smoke_rag.py**: 修复文档/代码漂移
  - 新增 `--data-dir` 参数（默认开发工作区 `<插件父目录>/data_out`）
  - 新增 `--real`：用真实 chromadb+BGE 后端读取预构建产物做 RAG 查询验证（默认仍为 Fake 后端离线跑）
  - 修复相对导入：支持 `python -m tools.smoke_rag`（repo 根）与 `python -m astrbot_plugin_persona_agent.tools.smoke_rag`（插件父目录）两种运行方式
  - 更新 docstring 用法说明
- **DEPLOYMENT_GUIDE.md**: §4.7 / §4B.5 / §12 冒烟命令与实际工具行为对齐（离线默认 + `--real` 真实验证）
- **rag_service.py 主动回复卡死**：`SentenceTransformer(self._model_name)` 改为 `local_files_only=True`
  - 根因：SentenceTransformer 构造时会联网向 HuggingFace 校验元数据；huggingface.co 不可达时 `http_backoff` 无限重试，同步阻塞 AstrBot 事件循环（看门狗 30s 抓栈 = `_ensure_backend`）
  - 修复：只从本地 HF 缓存加载（模型已预置 781M 缓存），离线加载约 4s；不再联网阻塞
  - 注意：新机器须先预下载 BGE 模型，否则启动首次 RAG 会直接报错而非自动下载

### Docs（2026-08-22 本地文档审计，暂未推送）
- **README.md**: 更新架构/MemoryStore(SQLite)/部署步骤(BGE 离线预下载 + root NapCat)/当前状态到 v0.4
- **DEPLOYMENT_GUIDE.md / OPERATIONS.md / AGENTS.md（工作区根）**: 同步实测部署状态、
  BGE 离线修复、root NapCat 风控红线、归档历史文档到 `docs/archive/`
- **历史文档**: `docs/specs/*`、`GPU_FIX_BACKLOG.md`、`验机装机操作清单.md`、`authorization.md` 移入
  `docs/archive/`；一次性 `新建 文本文档.txt` 删除

---

## [0.2.0] — 2026-07-03

### Added
- **SessionManager**: 每群维护持久 session，消息累计复用。`name` 字段 (OpenAI API 原生) 区分不同参与者，`role=user/assistant` 正确分隔。
- **KGProvider**: 抽象接口 + RagKGProvider v1 (ChromaDB + BGE)。
  LLM 调用时注入结构化风格指引到 contexts 末尾。
- **EmotionProvider**: 抽象接口 + DefaultEmotionProvider (v1 中性)。
  `global_willingness` 调制 interjection 触发概率；`current_mood` 织入 system_prompt；`sticker_prompt` 触发表情包发送。
- **per-group 生成锁** (`_generating[group_id]`):
  生成期间同一群的新消息只记录到 session，不触发新 LLM 调用。
- **500ms 防抖**: 决策通过后等 500ms，取防抖期间累积的完整 session → 一次生成一条回复。
- **test_mode / test_group_id**: `_conf_schema.json` 新增字段。
  `_is_target_group()` 支持 `test_mode=1` 时切到测试群。

### Changed
- **LLM 调用方式重构**:
  - 旧: `contexts=[1条摘要]` + `prompt=user_text` + `system_prompt=persona+examples`
  - 新: `contexts=[全量 session + KG注入]` + `prompt=None` + `system_prompt=persona(固定)`
- system_prompt 固定不变 (prefix caching 全覆盖)。
- **回复从拆段多条改为一条完整发送**: 删除 segment split 循环 + typing delay。
- **interjection.decide()** 新增 `emotion_multiplier` 参数。

### Removed
- segment split 拆段循环 (`for seg in segments:` + `asyncio.sleep` 打字延迟)
- `\n` 拆段相关 system prompt 指令 (tone 字段中两条已删除)
- `import random` (不再需要)

### Fixed
- **#1 消息上下文反馈循环**: session 复用使 LLM 看到自己发言历史 → 不自相似触发。
- **#3 多人 @ Bot burst 洪水**: 生成锁 + 防抖合并多人 @ → 一条回复。
- **#5 测试群路由**: `test_mode` 切换正确识别测试/生产群。

---

## [0.1.0] — 2026-07-02

### Added
- 插件骨架: `main.py`, `metadata.yaml`, `_conf_schema.json`, `requirements.txt`
- **StyleProfile**: 7 个风格文件热加载 (mtime 自动重载) + alias 映射 + system_prompt 构建
- **RagService**: ChromaDB + BGE-base-zh-v1.5，dense + recency + hour_match 混合排序
- **InterjectionManager**: AT/RAG/COLD 三级决策 + hourly budget + cooldown
- **ContextBuffer**: 滑动窗口消息缓冲 (200 条 / 1 小时)
- **JsonStore**: 原子写 JSON/JSONL + mtime 追踪
- 离线工具: `build_dataset`, `verify_dataset`, `analyze_style`, `rebuild_chroma`, `smoke_rag`
- `persona_status` / `reload_persona_config` / `bind_dream` 命令
- @ 回复 + 目标群路由 (`_is_target_group`) + 群白名单
- `_postprocess`: AI 身份剥离 + CJK 空格 → `\n` + 行数/字数截断
