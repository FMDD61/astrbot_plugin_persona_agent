# astrbot_plugin_persona_agent

AstrBot 插件 — 在 QQ 群内模仿指定用户（QQ `234567`）的发言风格。

基于三层 AI 记忆架构：检索层（KGProvider：dense/BGE + BM25/FTS5 + 实体融合）→ 存储层（MemoryStore：SQLite ADD-only 实体关系图 + FTS5 BM25）→ 进化层（DreamJob：周 cron 漂移检测）。

## 架构

```
群消息 → 前置（识图/睡眠/冲突检测）→ PersonaPipeline.run()（A7 共享主链路）
   ├─ RAG 检索（风格源历史）→ Emotion（三维）→ interjection 硬闸（规则）
   ├─ GateLLM 决策层+安全阀（可选，gate.enabled=1；所有 REPLY/含@/TOPIC 都过；
  │      单 prompt 双任务 {reply, conflict, reason}；conflict=true 强制不发言；0.2 温度+off 思考）
   ├─ KG 风格指引 + session 全量上下文（prefix caching）+ 固定示例
   ├─ LLM 生成 → postprocess → SendIntent（含 [r:-N] 引用/表情意图）
   └─ 全链路 trace → logs/<group_id>/trace_log.jsonl（trace_view 可读）
```

### 三层记忆

| 层 | 触发 | 职责 |
|----|------|------|
| 检索层 `KGProvider` | 每次 LLM 调用 | "现在该怎么说话？" → 图遍历 + 向量检索 → 注入提示 |
| 存储层 `MemoryStore` | 每条消息 | 实体/关系抽取 → SQLite ADD-only 图写入 + FTS5 |
| 进化层 `DreamJob` | 每周 cron | 回放近期对话 → 更新权重 → 漂移报告 (只建议不改) |

### 双层 LLM（A7）

| 层 | 模型 | 职责 |
|----|------|------|
| 决策层+安全阀 `GateService` | 独立中性 LLM（provider 同源 dsv4f） | "这句要不要回" + conflict 判定（{reply, conflict, reason}）；conflict=true 强制不发言（防煽风点火）；0.2 温度 + off 思考（不发该参数）；同群节流；失败保守静默 |
| 扮演层 `PersonaPipeline` → LLM | 主 RP 模型 | 纯粹表演（上下文不掺工具/决策噪音）；reasoning off（不发参数）+ 温度分档；max_tokens 512 |

## 快速开始

### 前置

- AstrBot v4.25+
- NapCatQQ (OneBot v11)
- opencode-go 网关 LLM API（OpenAI 兼容；deepseek-v4-flash，1M 上下文；视觉用 deepseek-v4-flash-vision-exp）
- Python 3.12

### 部署

```bash
# 部署机（Debian，2026-08-21 已上线 root NapCat + AstrBot）
cd /opt/AstrBot/data/plugins/
git clone https://github.com/FMDD61/astrbot_plugin_persona_agent.git
cd astrbot_plugin_persona_agent
pip install -r requirements.txt

# 确认数据目录独立
python3 -c "import json; c=json.load(open('_conf_schema.json')); print(c['data_dir']['default'])"
# → /opt/AstrBot/data/plugin_data/astrbot_plugin_persona_agent

# BGE 模型必须预下载（插件 local_files_only=True + HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE 双保险，不会联网自动下载）
export HF_ENDPOINT=https://hf-mirror.com
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-base-zh-v1.5')"

# AstrBot WebUI 启用插件 → 测试群 @ 机器人
```

> NapCat 必须以 root/独立用户运行（`/opt/QQ/qq --no-sandbox -q QQ号`），
> 与桌面 QQ 同目录双开会触发风控。详见 OPERATIONS.md / DEPLOYMENT_GUIDE.md。

### 配置

WebUI: `http://<IP>:6185` → Astr 插件 → astrbot_plugin_persona_agent

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `reply_on_at` | 1 | @ 回复开关 |
| `active_interjection` | 0 | 主动插话开关 (0=仅@回复) |
| `test_mode` | 0 | 测试模式 (1=只在 test_group_id 生效) |
| `test_group_id` | 123456788 | 测试群号 |
| `target_group_id` | 123456789 | 生产群号 |
| `data_dir` | `/opt/AstrBot/data/...` | 运行时数据目录 (git pull 不覆盖) |

> 完整配置见 `_conf_schema.json`：`sleep.*`（睡眠窗 02–07）、`diary.*`、`examples.*`、`vision.*`、`emotion.*`、`housekeeping.*`、`privileged_qq`、`llm.temperature`（温度分档）、`summary.*`（周/月摘要，G13）、`poke.*`、`topic_bank.*`、`dream.*`、`gate.*`（A7 GateLLM 决策层+conflict 安全阀，enabled=0 默认关 / temperature=0.2 / reasoning_effort=off）、`trace.*`（A7 全链路 trace，enabled=1 默认开）、`rag.enabled`（RAG 总开关，A7③）、`vision.cache_persist*`（识图持久缓存，A7③）、`llm.reasoning_effort`（RP 思考 off=不发送该参数；网关只认 low/medium/high/max）、`llm.max_tokens`（默认 512，需容纳 ~200 思考 token）、`emotion.temperature/reasoning_effort`（0.2/off）。

## 当前状态

**v0.5.0（2026-09-08）** — **生产接管**（`test_mode=0`，目标群 `123456789`）。含：v3 按日会话+02:00轮换、睡眠窗 02–07、每日日记、识图（flash-vision-exp，持久哈希缓存）、情绪引擎 v1、示例注入（规则A/B）、离线 A/B 通道、**主动插话（`active_interjection=1`，阈值 0.65）**、**DreamJob（`dream.enabled=1`）**、**周/月摘要（`summary.*=1`）**、G11/G12 代码就绪。**A7（至 2026-09-10）**：GateLLM 决策层+conflict 安全阀（含 @/TOPIC 覆盖）、PersonaPipeline 共享主链路、全链路 trace（trace_view 查看）、群隔离、日志按群分目录、RAG 动态开关（rag.enabled）、Vision 持久 LRU 缓存、LLM 参数矩阵、**离线测试台 replay_scene（extract/run）**、**RAG 语料清洗 + 重建（4704 对）**、**hourly_budget 宽松化（daily 24→2400）**。关键修复（2026-09-10）：`reasoning_effort` 不再发 `none`（网关 400 → 空回复静默，A7 上线会全哑）+ `max_tokens` 256→512。`gate.enabled=0` 未开待手动验证。测试 **171 例全绿**。

> A7 计划书：`docs/specs/chatbox-rp-tool-dual-channel.md`（工作区根）

| Issue | 状态 |
|-------|------|
| #1 消息上下文反馈循环 | ✅ 已关闭 |
| #3 多人 @ Bot burst 洪水 | ✅ 已关闭 |
| #5 测试群路由 | ✅ 已关闭 |
| #2 RAG → AI 记忆层 | ✅ SQLite MemoryStore + KGProvider 已落地（PG 方案为远期规划） |
| #4 口癖过度使用 + 风格不一致 | ✅ 已关闭（postprocess + system_prompt 调优） |
| #6 Git 同步工作流 | ✅ 已关闭（git pull 流程） |
| 主动回复卡死（BGE 联网校验） | ✅ 已修复（`local_files_only=True`，commit e3f1dc5；双保险 setdefault HF_HUB_OFFLINE，d218654） |
| #7 smoke_rag --real 卡死（BGE 联网校验残留） | ✅ 已修复（d218654，faulthandler 栈定位） |

## 文件结构

```
astrbot_plugin_persona_agent/
├── main.py                  # 插件入口, on_group_message handler
├── metadata.yaml            # AstrBot 插件元数据
├── _conf_schema.json        # 配置 schema (含 data_dir/test_mode)
├── requirements.txt
├── services/
│   ├── session_manager.py   # 每群持久 session (name 字段区分参与者)
│   ├── kg_provider.py        # MultiSignalKGProvider (dense+BGE + BM25/FTS5 + entity)
│   ├── emotion.py            # LLMEmotionProvider v1 (3 维: 意愿/情绪/表情, 30s 缓存, 3s 超时)
│   ├── interjection.py       # 规则硬闸 (AT/RAG/COLD 三级; 用量按群隔离 + usages/<gid>.json)
│   ├── llm_params.py         # A7④ reasoning_effort 映射 (off→None=不发送该参数; 网关拒 none/off 会 400)
│   ├── gate.py               # A7 GateLLM 决策层+安全阀 ({reply, conflict, reason}; @/TOPIC 全覆盖; 0.2+off; is_at 缓存键)
│   ├── pipeline.py           # A7 共享主链路 (RAG→情绪→硬闸→Gate→KG→生成→SendIntent+trace)
│   ├── style_profile.py      # 风格文件热加载 + alias 映射
│   ├── rag_service.py        # ChromaDB 向量检索 (local_files_only 离线 BGE)
│   ├── memory_store.py       # SQLite ADD-only 实体关系图 + FTS5 BM25
│   ├── conflict_detector.py  # 旧冲突检测 (仅 gate.enabled=0 兜底)
│   ├── dream_job.py          # 周 cron 记忆巩固 + 漂移报告
│   ├── context_buffer.py     # 滑动窗口 buffer (仅用于 interjection 决策)
│   ├── examples.py           # G14 静态示例注入 (mtime_ns 热重载 + 规则A/B)
│   ├── vision.py             # G15 识图 (flash-vision-exp, 三源解析, 诚实占位)
│   ├── poke.py               # G11 戳一戳 (同人冷却/小时配额/未知成员不回戳/严肃抑制/poke_log)
│   ├── topic_bank.py         # G12 冷场话题 (§10 评分/热加载/topic_sent 按群归档)
│   ├── summary.py            # G13 周/月摘要 (日日记聚合 + 原文抽样防失真 + bind_dream 推送)
│   ├── json_store.py         # 原子 JSON/JSONL 读写 (自动建子目录: usages/, logs/<gid>/)
│   └── text_style.py         # 纯文本清洗/后处理 (占位符剥离/引用标记/口癖/换行)
├── tools/
│   ├── build_dataset.py      # 离线: merge.json → 对话对
│   ├── verify_dataset.py     # 离线: 对话对验证
│   ├── analyze_style.py      # 离线: 风格画像提取 (不覆盖已有文件)
│   ├── rebuild_chroma.py     # 离线: 对话对 → ChromaDB 索引
│   ├── smoke_rag.py          # 离线: RAG 冒烟测试 (--real 真实库验证)
│   ├── select_examples.py    # G14 候选池筛选 (规则+分桶+LLM打分)
│   ├── ab_test_examples.py   # 离线 A/B 生成 harness (同源提示词/网关)
│   ├── ab_judge_style.py     # 风格 judge (正反清单 1-5 分)
│   ├── sync_config.py        # A2 配置-schema 同步 (只补缺省/保留现有值/BOM 兼容/备份)
│   ├── trace_view.py         # A7 trace 渲染工具 (层级可读报告, dsh 内查看)
│   ├── replay_scene.py       # A7④ 离线测试台 (extract 场景抽取 / run 真 services 重放 → dsh 日志)
│   ├── find_style_windows.py # A7④ 滑动窗口找风格源最活跃时段 (O(n) + 优先队列 top-N)
│   └── clean_pairs.py        # A7④ RAG 语料清洗 (去图片占位 reply / 标记化 ctx 图行)
└── data_out/                 # (gitignored) 离线产物 + 风格文件

运行时数据目录 (data_dir, git pull 不覆盖):
├── usages/<group_id>.json    # interjection 用量状态 (按群, 原子写 + 热重载)
├── logs/<group_id>/          # trace/decision/gate/cache_probe/diary 日志 (按群分目录)
├── session_<group>_<day>.json# 按日会话
└── topic_bank.json / topic_sent.json / member_relations.json / ...

## 操作手册

- `DEPLOYMENT_GUIDE.md` — 完整部署流程 (git clone + 数据构建)（位于工作区根）
- `OPERATIONS.md` — 冷启动/关机/screen 重连/测试模式（位于工作区根）
- `IMPLEMENTATION_PLAN.md` — 18 节详细实施计划（位于工作区根）
- `CHANGELOG.md` — 版本历史
- `docs/archive/` — 历史设计/装机/GPU 文档归档（位于工作区根）
