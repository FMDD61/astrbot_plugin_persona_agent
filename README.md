# astrbot_plugin_persona_agent

AstrBot 插件 — 在 QQ 群内模仿指定用户（QQ `337934842`）的发言风格。

基于三层 AI 记忆架构：检索层（KGProvider：dense/BGE + BM25/FTS5 + 实体融合）→ 存储层（MemoryStore：SQLite ADD-only 实体关系图 + FTS5 BM25）→ 进化层（DreamJob：周 cron 漂移检测）。

## 架构

```
session (全量上下文，prefix caching)
  └─ KGProvider.query() → 结构化风格指引 → 注入 LLM contexts
       └─ MemoryStore (SQLite ADD-only + FTS5 BM25)
            └─ DreamJob (周 cron, 关系漂移检测)
```

### 三层记忆

| 层 | 触发 | 职责 |
|----|------|------|
| 检索层 `KGProvider` | 每次 LLM 调用 | "现在该怎么说话？" → 图遍历 + 向量检索 → 注入提示 |
| 存储层 `MemoryStore` | 每条消息 | 实体/关系抽取 → SQLite ADD-only 图写入 + FTS5 |
| 进化层 `DreamJob` | 每周 cron | 回放近期对话 → 更新权重 → 漂移报告 (只建议不改) |

## 快速开始

### 前置

- AstrBot v4.25+
- NapCatQQ (OneBot v11)
- 火山引擎 LLM API
- Python 3.12

### 部署

```bash
# 台式机（Debian，2026-08-21 已上线 root NapCat + AstrBot）
cd /opt/AstrBot/data/plugins/
git clone https://github.com/FMDD61/astrbot_plugin_persona_agent.git
cd astrbot_plugin_persona_agent
pip install -r requirements.txt

# 确认数据目录独立
python3 -c "import json; c=json.load(open('_conf_schema.json')); print(c['data_dir']['default'])"
# → /opt/AstrBot/data/plugin_data/astrbot_plugin_persona_agent

# BGE 模型必须预下载（插件 local_files_only=True，不会联网自动下载）
export HF_ENDPOINT=https://hf-mirror.com
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-base-zh-v1.5')"

# AstrBot WebUI 启用插件 → 测试群 @ 机器人
```

> NapCat 必须以 root/独立用户运行（`/root/Napcat/opt/QQ/qq --no-sandbox -q QQ号`），
> 与桌面 QQ 同目录双开会触发风控。详见 OPERATIONS.md / DEPLOYMENT_GUIDE.md。

### 配置

WebUI: `http://<IP>:6185` → Astr 插件 → astrbot_plugin_persona_agent

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `reply_on_at` | 1 | @ 回复开关 |
| `active_interjection` | 0 | 主动插话开关 (0=仅@回复) |
| `test_mode` | 0 | 测试模式 (1=只在 test_group_id 生效) |
| `test_group_id` | 1047699954 | 测试群号 |
| `target_group_id` | 881438753 | 生产群号 |
| `data_dir` | `/opt/AstrBot/data/...` | 运行时数据目录 (git pull 不覆盖) |

## 当前状态

**v0.4-nuwa-interview** — 已部署运行（`test_mode=1`，仅测试群 `1047699954` 生效）。

| Issue | 状态 |
|-------|------|
| #1 消息上下文反馈循环 | ✅ 已关闭 |
| #3 多人 @ Bot burst 洪水 | ✅ 已关闭 |
| #5 测试群路由 | ✅ 已关闭 |
| #2 RAG → AI 记忆层 | ✅ SQLite MemoryStore + KGProvider 已落地（PG 方案为远期规划） |
| #4 口癖过度使用 + 风格不一致 | ✅ 已关闭（postprocess + system_prompt 调优） |
| #6 Git 同步工作流 | ✅ 已关闭（git pull 流程） |
| 主动回复卡死（BGE 联网校验） | ✅ 已修复（`local_files_only=True`，commit e3f1dc5） |

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
│   ├── emotion.py            # LLMEmotionProvider v2 (5 维, 30s 缓存, 3s 超时)
│   ├── interjection.py       # 决策引擎 (AT/RAG/COLD 三级)
│   ├── style_profile.py      # 风格文件热加载 + alias 映射
│   ├── rag_service.py        # ChromaDB 向量检索 (local_files_only 离线 BGE)
│   ├── memory_store.py       # SQLite ADD-only 实体关系图 + FTS5 BM25
│   ├── conflict_detector.py  # 三阶段冲突检测
│   ├── dream_job.py          # 周 cron 记忆巩固 + 漂移报告
│   └── context_buffer.py     # 滑动窗口 buffer (仅用于 interjection 决策)
├── tools/
│   ├── build_dataset.py      # 离线: merge.json → 对话对
│   ├── verify_dataset.py     # 离线: 对话对验证
│   ├── analyze_style.py      # 离线: 风格画像提取 (不覆盖已有文件)
│   ├── rebuild_chroma.py     # 离线: 对话对 → ChromaDB 索引
│   └── smoke_rag.py          # 离线: RAG 冒烟测试
└── data_out/                 # (gitignored) 离线产物 + 风格文件
```

## 操作手册

- `DEPLOYMENT_GUIDE.md` — 完整部署流程 (git clone + 数据构建)（位于工作区根）
- `OPERATIONS.md` — 冷启动/关机/screen 重连/测试模式（位于工作区根）
- `IMPLEMENTATION_PLAN.md` — 18 节详细实施计划（位于工作区根）
- `CHANGELOG.md` — 版本历史
- `docs/archive/` — 历史设计/装机/GPU 文档归档（位于工作区根）
