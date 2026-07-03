# astrbot_plugin_persona_agent

AstrBot 插件 — 在 QQ 群内模仿指定用户（QQ `337934842`）的发言风格。

基于三层 AI 记忆架构：检索层（知识图谱注入）→ 存储层（Postgres 图表）→ 进化层（周 cron 漂移检测）。

## 架构

```
session (全量上下文，prefix caching)
  └─ KGProvider.query() → 结构化风格指引 → 注入 LLM contexts
       └─ MemoryStore (PG 图 + pgvector)
            └─ DreamJob (周 cron, 关系漂移检测)
```

### 三层记忆

| 层 | 触发 | 职责 |
|----|------|------|
| 检索层 `KGProvider` | 每次 LLM 调用 | "现在该怎么说话？" → 图遍历 + 向量检索 → 注入提示 |
| 存储层 `MemoryStore` | 每条消息 | 实体/关系抽取 → PG 图写入 |
| 进化层 `DreamJob` | 每周 cron | 回放近期对话 → 更新权重 → 漂移报告 (只建议不改) |

## 快速开始

### 前置

- AstrBot v4.25+
- NapCatQQ (OneBot v11)
- 火山引擎 LLM API
- Python 3.12

### 部署

```bash
# R7000 / 台式机
cd /opt/AstrBot/data/plugins/
git clone https://github.com/FMDD61/astrbot_plugin_persona_agent.git
cd astrbot_plugin_persona_agent
pip install -r requirements.txt

# 确认数据目录独立
python3 -c "import json; c=json.load(open('_conf_schema.json')); print(c['data_dir']['default'])"
# → /opt/AstrBot/data/plugin_data/astrbot_plugin_persona_agent

# AstrBot WebUI 启用插件 → 测试群 @ 机器人
```

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

**v0.2-dev** — session 复用已部署，KG 三层架构设计中。

| Issue | 状态 |
|-------|------|
| #1 消息上下文反馈循环 | ✅ 已关闭 |
| #3 多人 @ Bot burst 洪水 | ✅ 已关闭 |
| #5 测试群路由 | ✅ 已关闭 |
| #2 RAG → AI 记忆层 | 🔧 设计中 |
| #4 口癖过度使用 + 风格不一致 | ⏳ 待处理 |
| #6 Git 同步工作流 | ⏳ 待处理 |

## 文件结构

```
astrbot_plugin_persona_agent/
├── main.py                  # 插件入口, on_group_message handler
├── metadata.yaml            # AstrBot 插件元数据
├── _conf_schema.json        # 配置 schema (含 data_dir/test_mode)
├── requirements.txt
├── services/
│   ├── session_manager.py   # 每群持久 session (name 字段区分参与者)
│   ├── kg_provider.py        # KGProvider 抽象 + RagKGProvider v1
│   ├── emotion.py            # EmotionProvider 抽象 + DefaultEmotionProvider
│   ├── interjection.py       # 决策引擎 (AT/RAG/COLD 三级)
│   ├── style_profile.py      # 风格文件热加载 + alias 映射
│   ├── rag_service.py        # ChromaDB 向量检索 (将被 MemoryStore 替代)
│   ├── context_buffer.py     # 滑动窗口 buffer (仅用于 interjection 决策)
│   └── json_store.py         # 原子写 JSON/JSONL
├── tools/
│   ├── build_dataset.py      # 离线: merge.json → 对话对
│   ├── verify_dataset.py     # 离线: 对话对验证
│   ├── analyze_style.py      # 离线: 风格画像提取 (不覆盖已有文件)
│   ├── rebuild_chroma.py     # 离线: 对话对 → ChromaDB 索引
│   └── smoke_rag.py          # 离线: RAG 冒烟测试
└── data_out/                 # (gitignored) 离线产物 + 风格文件
```

## 操作手册

- `DEPLOYMENT_GUIDE.md` — 完整部署流程 (git clone + 数据构建)
- `OPERATIONS.md` — 冷启动/关机/screen 重连/测试模式
- `IMPLEMENTATION_PLAN.md` — 18 节详细实施计划
- `CHANGELOG.md` — 版本历史
