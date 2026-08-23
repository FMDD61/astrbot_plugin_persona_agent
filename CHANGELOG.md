# Changelog

本文档记录 `astrbot_plugin_persona_agent` 的所有功能变动。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/).

---

## [Unreleased]

### Changed/Fixed (G15 实测修正)
- 视觉模型定稿（2026-08-24 实测）：默认 `deepseek-v4-flash-vision-exp` + `reasoning_effort=low` + `max_tokens=512`（思考不挤占输出；与本地 dsh settings.yaml 的视觉模型声明一致）。附录：mimo-v2.5 在 text-first 载荷下也能返回图像描述，但网关/dsh 均未声明其 image 输入，弃用
- 视觉模型默认改为 `deepseek-v4-flash-vision-exp`（实测 mimo-v2.5 视觉返回空 content；flash-vision-exp 可准确描述）
- 描述失败时注入「（配图：无法识别）」诚实占位，杜绝主 LLM 对未见图凭空猜“芳乃”类幻觉
- 事件循环防泄漏加固：生成锁分支 / LLM 失败 / 空回复三条早退路径补 `stop_event()`，避免 AstrBot 核心兜底回复将错误文本广播到群
- 说明：此前「LLM 响应错误」外泄为 AstrBot 核心默认 agent 行为（非目标群未处理消息 + 图片多模态调用失败），非插件代码泄漏；生产切换（test_mode=0）后目标群由插件接管可避免，或在 AstrBot WebUI 配置 `provider_ltm_settings.image_caption_provider_id` 指定视觉模型
### Added
- **睡眠窗即时开关（2026-08-24）**: `/persona_wake`（特权，测试期唤醒）/ `/persona_sleep`（恢复），内存态即时生效免重启；`_is_sleeping()` 改为每消息热读配置（WebUI 改 sleep.* 亦即时生效）
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
- **member_relations 风格源标注**: 焦糖条目增加 `is_style_source: true`（不影响别名块渲染，系统提示词哈希实测不变，缓存不失效）
- **换行折叠（postprocess 强制执行 prompt 禁令）**: 回复中的多段换行按标点感知合并为单行（`x\n\ny` → `x，y`），修复 2026-08-23 实测 6/6 回复带换行的系统性违规
- **member_relations.json 补风格源条目**: `337934842 → 焦糖/焦糖玛奇朵`（人工格式追加，携 `.bak.20260822` 备份；修正此前机器人臆造他人别名「老狗/内桑」称呼的根因）
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
