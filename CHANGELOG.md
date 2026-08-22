# Changelog

本文档记录 `astrbot_plugin_persona_agent` 的所有功能变动。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/).

---

## [Unreleased]

### Added
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
