## [v0.4.0] - 2026-08-24
- 版本号与 metadata 对齐（v0.1.0→v0.4.0）；文档全面校正（AGENTS/OPERATIONS/README/IMPLEMENTATION_PLAN/DEPLOYMENT_GUIDE）；本会话 G1-G18 闭环明细见根目录 TODO.md

# Changelog

本文档记录 `astrbot_plugin_persona_agent` 的所有功能变动。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/).

---

## [Unreleased]

### Changed (2026-09-20, 提示词 v2 · 批次一（前置四件之 C7/C10）：人格装配接线)
> 来源：`docs/specs/prompt_v2_handoff.md`（任务清单）+ `docs/specs/prompt_v2_rp.md` §2/§10
> + `BUGS.md` B-037。设计文档在仓库外（根目录非 git 仓库）。
> **只做前置四件里的 C7/C10**；C16/C17 见下一条。

- **🔴 人格文件 75% 从未进 prompt —— 接线（B-037 / C7 / C10）**：`StyleProfile.system_prompt()`
  用硬编码 7 元组读 `system_prompt_fragments.json`，2026-07-26 的 v0.4 整份重写后**键元组没跟着改**
  → 22 键里只有 6 键（910 / 6145 字符）进过 prompt，`expression_dna`/`social_behavior`/
  `mental_models` 等 14 键（4629 字符）**被静默丢弃**（其中 `relations` 还被 `isinstance(v,str)`
  守卫二次吞掉）。修法：新增 `services/persona.py`（**声明式段注册表** + 三个装配视图）
  与生成物 `services/persona_sections.py`（文案真身在 `docs/specs/` 草案，`tools/gen_persona_sections.py` 生成，
  单测 `test_persona_source_sync` 钉死"生成物 == 草案"）。
  - **段与视图**：RP 卡 = §1+作息+§2+§3+§4+§6+§7+§8（≈1470 字符，旧 910）；
    Gate 冻结头部 = §1+§2+§4+GATE 决策段（`gate_system_prompt()`，C22 的一半）；
    总结类 system = §1+§2（`summary_system_prompt()`，C32 的一半）。
  - **可编辑**：`<data_dir>/persona/sN.md` **有内容即覆盖**内置默认，删文件即回到默认；
    ⚠️ 空文件**不算**覆盖（想临时禁用某段请删文件）。每次调用现读，**不做 mtime 缓存** ——
    实测内核粗时钟下两次同尺寸写入的 `st_mtime_ns` **完全相同**，任何 mtime 指纹都会漏检；
    启动时把默认物化到该目录（**只在缺失时写**），并同步写一份 `README.md` 说明段→文件对应关系。
  - **回退开关**：`persona.sections_mode = legacy` 回到旧键元组装配（改不动时的退路）。
  - **降级必须可见**（本项目反复栽在这里）：启动日志与自检逐条打印
    `段清单 used/missing/file_override`；旧人格文件里**已被取代但仍留着的键**显式告警列出
    （B-037 的另一半："不再读取"这件事必须说出来）；`missing`/整体回退会写进 trace `persona_degraded`。
  - **§3 记忆段**：头部为恒定文案，正文由 `<data_dir>/memory_digest.json` 渲染；**空层整段不出现**，
    全空时 §3 整段省略（不留空壳标题）。**注意：这是渲染器，选取侧规则（各层不重叠）属 C4，尚未实现。**
  - 测试 670 → **696 全绿**（新增 `test_persona_sections` 25 例 + `test_persona_source_sync` 1 例；
    旧夹具照抄过时键元组、与实现同源漂移的两处已改成写 `persona/*.md`）。
  - ⚠️ **已知取舍（首日副作用）**：人格文本一变，`sync_system_prompt` 判 `update` →
    **旧 910 字人格仍留在 session[0]**，新全文以「［设定更新］…以此为准」追加在尾部，
    两者共存到次日 02:05 轮转。语义由"以此为准"兜住，但旧块仍占前缀一天。

### Changed (2026-09-20, 提示词 v2 · 批次一（前置四件之 C16/C17）：KG/RAG 展示下架 + 引用改打标制)

- **🔴 KG/RAG 块的展示从 RP 与 Gate 双双删除（D42 / C16）**：实测该块 **100% 出现**、
  得分中位 **0.40**、**94% 在 0.6 以下**，内容是**裸回复无情境**（B-039），
  且在**示范已被禁掉的行为**（反复出现「今日老婆」——D35′ 明令不用群内 bot 指令）。
  修法：RP 不再拼 `kg_content` 且**跳过 `kg_provider.query()`**（省一次 BM25+entity 融合）；
  Gate 不再收 `rag_hits`。**硬闸的 `top_rag_score` 一个字没动** ——
  参与量杠杆仍由 `rag.score_threshold` 控制，只是判断依据不再给模型看。
  - **回退开关** `rag.display_enabled`（默认 **0** = 不展示；置 1 回到旧行为）——
    这段时间同时在重写整个提示词，没有开关就无法归因（§13.8③）。
  - **降级必须可见**：`trace["kg_tail"]` **保留**，展示关闭时写「（已禁用：D42 …）」；
    另加 `trace["kg_display"]` 记开关实际取值（§13.8③ 点名要求）。
  - **连带修掉一条悬空判据**（§13.8①）：`GATE_SYSTEM_PROMPT` 的 A 判定第 4 条原写
    「参考风格片段：…能不能自然接上」——删掉注入后它没有输入了，改为
    「参考**对话历史里我自己的发言**」（Gate 本来就有全量 session，含 assistant 条目）。
  - 测试 +6 例（`TestKgRagDisplayRemovedC16`：默认不查 KG / 开关打开仍查 /
    硬闸分数不受影响 / Gate 收不到风格片段且开关打开时收得到 / schema 默认值）。
- **🔴 引用改「打标制」（D37/D40/D43 / C17）**：旧制要模型**自己数** `[r:-N]` 是第几条
  —— 编号错位是 B-001 那一族缺陷的根。新制：硬闸放行的那条在 PHI 里就是
  `【现在要回应的】…`，模型只写 **`[r]`（不带 N）**，引用 id 由代码给出。
  - `PipelineInput.message_id`（新字段）＋ 挂起落盘槽兜底 → 记下「通过硬闸的那一条」；
  - `[r]` → 直接用该 id；**异常路径拿不到 id → 静默退化为不引用**（设计③），
    但 trace 记 `quote_mode=tagged` / `quote_tagged_id` / `quote_target_missing`，
    让「没引用」与「拿不到 id」在日志上可区分；
  - 标记**无论是否解析出目标都必须剥离**（B-012 教训），并顺带补上 `RE_REPLY_MARKER`
    漏掉的**裸 `[r]`**（此前只认 `[r:`，裸标记会原样发进群）；
  - **保留旧编号制**：`[r:-N]` 仍可解析 —— `persona.sections_mode=legacy` 回退时
    旧文案教的正是它，两条路都要能走。
  - 测试 +7 例（`TestTaggedQuoteC17`）。

### Changed (2026-09-20, 提示词 v2 · 批次二b：C31 摘要 digest+body / C33 dream 四项)
> 完成 handoff §7 的批次②（C20/C21/C32 见更早的条目，C31/C33 见本条）。
> **不改线上即时行为**：摘要与梦都在睡眠窗（02:05–03:00）跑。

- **C31 摘要改 `digest + body` 两字段**（`summary_v2.md` §2/§3/§7）：
  - **格式约定死**（frontmatter + 正文），代码在 `---` 围栏处切开（`split_digest_body`）。
    依据是 `vision.py` S6 的实测：自由文本可用率 **31.2%**，约定固定形状后 **100%**。
  - **两条线不交叉**：`digest`（一句话）**只进 §3 长期记忆**；`body`（正文）**只喂上一层**
    （周读日记 body、月读周 body、年读月 body）。**正文永不进 RP 上下文** ——
    每天往上下文塞 200 字散文，与「把输出压回 7.9 字」的方向相反。
  - **四个 PHI 换定稿文案**（日记/周/月/年），**一律包成末尾的 `system` 块**；
    **原料留在 `user`**（`build_prompt` 收窄为纯原料）。整条改成 system 会让请求里
    **没有任何非 system 消息**，部分 provider 不接受 —— 所以必须拆两半。
  - **旧数据迁移桥**：旧记录只有 `summary` → 读侧按 `body = body or summary` 回落
    （`list_diaries` / `list_summaries` / `dream.gather_diaries` 三处一致）。
    不回落的话迁移首日 dream/周报原料**全空**，而表现是「没有日记」——正是静默失效。
    回落条数进 `stats["legacy_summary_diaries"]`。
  - 格式没遵守时**正文照收**（只记 `digest_missing`）：宁可少一句摘要，不要丢掉整篇。
- **C33 dream 四项 + 删锚点阶段**（`dream_v2.md` §5③/§8/§9/§10）：
  - **⑤ 删除锚点阶段**（用户 2026-09-19：「锚点直接删去…未在设计内，会对 LLM 的注意力
    产生不确定的引导行为」）。该设计出自**子代理推理**、从未被验证；三次真实运行里
    两次锚点为空（等于顺带跑了对照组），**没锚点那次也成立**。删后**每周只剩 1 次调用**。
    连带作废 **③**（「锚点空返回要留痕」的存在前提就是阶段①）。
  - **②** 取料 `summary` → **`body`**（digest 不进梦）。
  - **④** system 加 **§4 世界段**（`build_dream_system` + `StyleProfile.world_section()`）——
    梦唯一的设定来源；身份靠日记的第一人称隐含继承（保留朦胧感）。
  - **①** `dreams.jsonl` 注释更正：「供 LLM 消费」是错的 —— 梦**只推送给人**，无 LLM 读者。
  - 删除清单：`ANCHOR_SYSTEM` / `parse_anchors` / `build_anchor_prompt` / `DreamResult.anchors` /
    `anchor_fn` / `main._dream_anchor_llm()` / 配置 `dream.anchor_temperature` + `anchor_max_tokens`
    / 三条锚点用例。
  - 新增 `test_no_anchor_symbols_remain_in_module`：模块里不得再留锚点阶段的任何符号。
- **连带补漏（自查发现）**：`services/familiarity.py:167` 的关系提案提示词仍在读 `d['summary']` ——
  C31 之后新记录没有这个键 → 模型看到的会是**空日记**，而表现只是「提案质量变差」，不会报错。
  已改为 `body or summary`，并补一条回归用例。
- 测试 731 → **746 全绿**（新增 C33 回归 7 例 + C31 回归 8 例 + 提案提示词 1 例，改写 3 例旧断言）。

### Fixed (2026-09-20, 独立核验第 6 轮：C4 的 1 阻塞 + 6 非阻塞 —— **上线前必修**)
> 审查方对 `610ac84` 的冻结快照核验。主路径时序、各层不重叠（**含跨年**）、9 种坏数据组合、
> 端到端落盘→读侧→进 §3 全部实测通过；但抓到一个**会让 §3 停在昨天**的阻塞项。

- **🔴 B-1（阻塞）：轮转有两条路径，只有 cron 那条接了 C4 —— 而且兜底路径会把 cron 的组装整段跳过。**
  `main.py` 里换日界的地方有**两处**：02:05 的 `_daily_rotation_job` 与消息处理里的兜底轮转。
  当时只改了前者；而后者一旦先换掉日界（**02:00–02:05 之间来一条消息**就会），
  cron 再跑时 `rotate_if_day_changed` 返回 `None` → `if old_msgs:` 为假 → **组装被跳过**。
  **净结果：新一天的 §3 是昨天的「这几天」** —— 陈旧比空更糟（空看得见，陈旧看起来完全正常）。
  **更常见的触发**：进程在 02:05 不在运行（重启/部署/崩溃）→ 当天 cron 没跑 → 第一条消息走兜底 → 同一条链。
  **09-21 轮转时部署正好命中这一类。**
  修法（四条一起）：
  1. 两条路径**共用同一个协程** `_rotate_followup()`（日记 → 组装）—— 这段逻辑曾被复制成两份，
     只有一份接了组装；**重复实现必然漂移**，这条与 B-037/B-030 同族；
  2. 组装**移出 `if old_msgs:`**，无条件执行（幂等纯读+写）；
  3. `initialize()` 末尾补一次组装 —— 直接覆盖「进程在 02:05 不在运行」这一大类；
  4. **静态闸的判据也改了**：第一版只按函数名取 `_daily_rotation_job` 一个函数，所以 B-1 从它眼皮下溜过；
     现在扫**全文件**，要求**每个** `rotate_if_day_changed` 调用点都通向组装，并禁止任何
     `create_task(self._generate_diary`（fire-and-forget）。
- **N-1**：`write_memory_digest` 现在**内容未变则不落盘** —— 否则刷新时间戳也会触发 `sync_system_prompt`
  的 `update`，白造一个「［设定更新］…新版全文」追加块（审查方实测：中途改 digest 当天就会以追加块形式生效）。
- **N-3**：`period`/`day` 非法时**不进 §3**（实测此前会原样漏成 `坏-W w坏-W` 这种行），
  跳过的条数进 `stats.skipped_bad_period`。
- **N-5**：读侧记下 `generated_at`，自检新增一行「§3 历史群聊摘要：N 行，组装于 …」，
  **早于今天就告警** —— 陈旧与新鲜在正文里长得一模一样，没有这一行发现不了。
- **N-6**：更正上一节的测试计数（实测 **769**，我写的 766 是在补静态闸之前数的）。
  *（这是本会话第三次「记录与实测不符」—— 前两次是 C21 与闸门描述。已在流程上自罚：数字一律以最终 `Ran N tests` 为准。）*
- **N-2/N-4 登记不修**：睡眠窗内仍有 vision 调用（夜里发图会调识图）；`memory_digest.json` 是单文件无群后缀
  （当前单目标群，无影响）—— 分别归批次三与"多群支持"。
- 测试 769 → **773 全绿**（实测 `Ran 773 tests`；新增 N-1/N-3/N-5 回归 3 例 + 闸门重写 2 例）。

### Added (2026-09-20, 无人值守部署工具 `tools/deploy/` —— 09-21 轮转前上线用)
> 用户 2026-09-20：「插件部署操作保证台式机上能自动定时执行」。
> 台式机口径（`DESKTOP_STATE.md`）：AstrBot 跑在**宿主机的 screen 会话 `astrbot`** 里，
> 插件代码在 `/opt/AstrBot/data/plugins/astrbot_plugin_persona_agent`。

- **`tools/deploy/deploy_persona_plugin.sh`** —— 一次跑完：
  `git fetch`（120s 超时 + `GIT_TERMINAL_PROMPT=0`，凭据坏掉也不会挂死在 cron 里）
  → 工作树必须干净 → **快进合并**（非快进即中止）→ **跑测试**（不过就 `git reset --hard` 回滚）
  → **只重启 AstrBot**（`screen -S astrbot -X quit` → 等它真退出（最多 60s，**不强杀**）
  → `screen -dmS astrbot bash start-astrbot.sh`）→ 核对启动日志（`[persona] mode=` / 示例来源 / 组装 / cron）。
  **绝不碰 docker / QQ 容器**；失败一定给出可读的中文原因，并写 `~/.deploy-persona-plugin.status`。
  子命令：`--check-only`（只看不动作）、`--force-restart`（盘上代码已新，只差激活）、
  `--no-restart`、`--skip-tests`、`--allow-dirty`、`--install-cron '<cron 表达式>' [--one-shot]`、
  `--remove-cron`。`--one-shot` = 成功跑完**自动摘掉自己那条 cron**（失败保留，便于次日排查）。
- **`tools/deploy/selftest.sh`** —— 离线自测台（**32 断言全过**）：真 git（裸远端 + 克隆）+
  桩 `screen`/`python3`/`crontab`，跑 9 个场景：工作树脏 → 拒绝；`--check-only` → 不动；
  正常部署 → 拉取+测试+重启+核对日志；已是最新 → **不重启**；`--force-restart` → 重启；
  **测试失败 → 回滚且不重启**；非快进 → 中止；cron 安装幂等/摘除；`--one-shot` 自摘。
  写它的理由：这个脚本会在**凌晨无人值守**时决定「要不要重启线上 bot」，判错一次就是一次事故。
  *（自测台自身也踩了两个坑并修掉：桩脚本的 heredoc 转义被吃掉、`screen -S x -X quit` 的 `quit` 是第 4 个参数 ——
  两个都只在跑起来才暴露。）*
- 口径变更：**部署脚本进插件仓库**（而不是项目根 `ops/desktop/`）——
  根目录不是 git 仓库，台式机拿不到；`ops/desktop/README.md` 留操作步骤与本仓库路径的指引。
- 测试：插件 Python 套件不受影响（**778 全绿**）；另加 shell 的 32 断言自测台。

### Fixed (2026-09-20, 独立核验第 7 轮：闸门加固 —— **复核结论：可上线** ✅)
> 审查方对 `71c47c2` 的冻结快照复核：B-1 的**三条触发链全部堵住**（含我特别问的
> 「cron 拿到 `old_msgs=None` 也仍会组装」）；`await` 真 awaited；超时后仍组装；
> N-1/N-3/N-5 都按预期生效；`2026-9-5` 被拒是**有意且正确**（生产端永远写零填充）。
> 它给出的结论是 **「可以按 09-21 轮转上线」**。

- **闸门加固（它用变异测试抓到的覆盖缺口）**：M1（把 cron 改回 `create_task(日记)`）→ 闸门报红 ✅；
  但 **M2（把 `await _rotate_followup(...)` 塞进 `if old_msgs:` 之内 —— 正是 B-1 的原始形态）漏检** ❌。
  根因：判据只查「函数文本里出现过组装调用」，**不管它在不在 `if old_msgs:` 里面**，
  而 B-1 的要害恰恰是「被门控」。
  已补 `gated_rotation_calls()`：对每个测试 `*_msgs` 的 `if`，要求调用在它**之外**，
  或该 `if` 有 `else` 且 `else` 里也通向组装（不带 `else` 那支会误报当前唯一合法的兜底形态）。
  并带**双向自证**：M2 形态必须报红、两种合法形态必须放行、真实 `main.py` 必须干净。
- **它登记的两条非阻塞**：闸门缺口（本轮已补）；链①/② 的**良性竞态**（兜底是 `create_task`，
  cron 的组装可能早于那条日记落盘 —— 但兜底任务随后会再组装一次，**自愈**，登记备查不修）。
- 测试 773 → **778 全绿**（实测；新增闸门自证 2 例）。

### 09-21 首日的日志预期（写进部署文档，别把正常当故障）

| 现象 | 是否正常 | 解释 |
|---|---|---|
| `recent_days` 只有 1 行（09-20） | ✅ 正常 | C31 之后才有 digest；09-20 02:05 那篇日记是旧格式 → 计入 skipped_no_digest |
| `recent_weeks` = 1 行（2026-W38） | ✅ 正常 | 周一 02:10 的周记 cron |
| `recent_months` / `older` = 空 | ✅ 正常 | 存量月报/年报是 C31 之前的记录（无 digest） |
| 日志出现「跳过无 digest 的 M 条」 | ✅ 正常 | 上面两条的留痕；10-01 / 2027-01-01 后自然补齐 |
| 自检出现「⚠️ §3 是**旧的**（组装于 …，不是今天）」 | ❌ 要查 | 当天轮转的日记/组装那两步没跑成 —— B-1 堵的就是这一类 |

### Changed (2026-09-20, C4：§3 历史群聊摘要的组装与轮转时序 —— **09-21 轮转前上线**)
> 用户定的部署点：**09-21 会话轮转时**，届时「总结 → 组装 → 开新一天」的顺次同时改变。
> 部署方式：**只 git pull + 重启 AstrBot**（不碰容器 / 不碰 QQ 进程）。

- **C4 组装管线（新）**：`services/summary.py::build_digest_layers()` / `write_memory_digest()`
  —— 组装 §3 的四层并**原子落盘** `<data_dir>/memory_digest.json`（tmp + `os.replace`：
  这是冻结头部的原料，半截文件会让 §3 静默消失）。
  - **各层不重叠**（`summary_v2.md` §4.2）：日记只报**最近一篇周记结束日之后**的那些天、
    周记只报**最近一篇月记覆盖月之后**的周、月记只报**最近一篇年记覆盖年之后**的月、
    年记报更早的年份。越久远越粗 → 遗忘痕迹成立；总量恒定（约 20 行）。
  - 上限 7 / 4 / 11 / 2；**没有内容的层整段不出现**（不留空标题）。
  - **无 digest 视作空**（用户定案）：旧记录（只有 `summary` 的 100~200 字正文）**不进 §3** ——
    那是正文，塞进长期记忆与「把输出压回 7.9 字」的方向相反。跳过的条数进 `stats.skipped_no_digest`。
  - ⚠️ 踩到并修掉一个坑：日记用 `day`、周/月/年用 `period` —— 字段名写死一个会让日记层**永远为空**
    （表现是「它记性不太好」，不报错）。已按 `period_key` 参数区分。
- **🔴 轮转时序（C23-b 的落地）**：`_daily_rotation_job` 里日记从 `asyncio.create_task(...)`
  （**发出去就不管**）改成 **`await asyncio.wait_for(..., timeout=300)`**，之后立刻
  `_rebuild_memory_digest()`。旧顺序下，新一天的 §3 可能在日记落盘**之前**就被组装 →
  **长期记忆里永远缺最近一天/一周/一月/一年**。给 5 分钟上界是为了：日记 LLM 卡死时
  不能让**整天**的 §3 都组装不出来。
- **周/月/年记写完也重算 §3**：它们会改变金字塔的分层边界（例：写了周记，被它覆盖的那几天
  就该从「这几天」里退场）。
- 静态闸 `TestRotationOrderC4`（AST 查顺序，因为 `main.py` 不被测试导入）：
  ① 日记必须被 `await`（不许回到 fire-and-forget）；② 组装的行号必须晚于总结；
  ③ 周期总结必须重算 §3。
- 测试 758 → **766 全绿**（digest 组装 8 例 + 时序静态闸 3 例）。

### Fixed (2026-09-20, 独立核验第 5 轮：7 条非阻塞 + 草案更新后重生成)
> 审查方对 `b54184a` / `ce400dd` 的冻结快照核验（0 阻塞 + 7 非阻塞）。
> 它同时**实测确认**：C5 三条硬约束全过、C1/C2 **旧句零回路**（七种输入路径 × 6 个旧串 = 0 命中）、
> 两道新闸在隔离环境（父级无 `docs`）下真跑且有效、`examples.enabled=0` 静默。

- **🔴 N-1（与之前修过的 N7 同根因）**：`services/examples.py` 的热重载**只比 `mtime_ns`** ——
  内核粗时钟下同一刻度内的任何改写 `st_mtime_ns` 完全相同 → **返回旧块**（新示例不可见、不报错）。
  这正是我们为 persona 段文件修掉的同一根因，同仓两种相反约定必然被下一个人踩。
  已按同一口径处理：**去掉 mtime 缓存，每次现读**（文件只有二十来条）。
- **N-3/N-4**（把注释升级成闸门）：示例块指纹新增 **`rendered_sha256_12`**（钉住**渲染后文本** ——
  此前只哈希 `ENTRIES` 结构，改 `_render()` 不触发任何闸）与 **`emote_count`**（钉住 `[emote:]` 密度）。
- **N-7**：新增断言把 schema 的 `examples.max_entries` 默认值与代码 `MAX_ENTRIES` **机器绑定**
  （此前只是「目前恰好一致」）。
- **N-2**：`block == ""` 的 docstring 补充 `max_entries=0` 这一情形。
- **N-5**：`tools/ab_test_examples.py` 打印 `state.source`/条数 —— 否则跑 A/B 的人看不出 ON 臂
  加载的是数据目录文件还是内置默认（文件缺失时口径不同）。**顺带修掉一个潜在失效**：
  此前文件缺失时 ON 臂拿到空串 → **ON ≡ OFF**，A/B 等于没做。
- **N-6 登记**：`口癖丙` 仍在 `text_style.KOUPI_LIST`（口癖**后处理封顶**名单，永不匹配、无害）。
  提示词侧已干净（草案 §7 与示例块都无它）；归 **C8** 一起处理，加注释以免被误当成 C1/C2 残留。
- **🔄 草案被使用者更新后重新生成**：`docs/specs/rp_examples_draft_v1.md` 于 16:34 被改（多条示例补上了
  `[emote:]`）→ `test_examples_source_sync` **如期报红**（这道闸第一次真正拦住漂移），
  重跑生成器后：**`[emote:]` 5 条 → 9 条**。
  ⚠️ 草案 §C 的覆盖矩阵仍写「8 条」，**实际 9 条**（文档待作者校正）；指纹里已钉住 9。
- 测试 755 → **758 全绿**。

### Changed (2026-09-20, 用户裁决落地：C5 图谱 NOTE 渲染 / C1+C2 示例块（新 20 条）)
> 用户当日三条裁决：① `notes` 人工维护、要渲染；② §4 角色表**暂不做**（他要先图转文）；
> ③ 示例块**只保留新 20 条**，部署用替换法，**任何情况下不回退旧句**。

- **C5 图谱 NOTE 渲染**：`relations_lines()` 现在把 `member_relations.json` 的 `notes` 渲染到成员行尾
  （`  1234: 别名 (也常被叫作: X)  [熟人] — <备注>`）。为什么必须做：**D28 把「对谁这么说」整块
  推给了群关系图谱 NOTE，不渲染 = §6 的那半句没有载体**。三条约束：
  - 备注是**人写的散文**，可能含换行 → **压成一行**（一成员一行是 `relations_lines()` 的契约，
    而 S10 的增量按**整行文本**比对，多行会撑坏块结构）；
  - 历史哨兵 `notes == "bot"` **不得当备注渲染**（`is_bot_member` 已按 kind/notes 双读过滤，
    渲染侧再挡一次，防将来 kind 迁移后哨兵泄漏进提示词）；
  - 备注目前**全是空的**（用户原打算让 LLM 从日志生成，后来决定人工写），所以本项**当下不改变
    线上提示词**，只是把槽位接通 —— 他写一条，下一次增量就会以「变化行」追加到会话尾部。
- **C1+C2 示例块重做**：新 20 条成为**内置默认**（`services/examples_default.py`，由
  `tools/gen_examples_default.py` 从 `docs/specs/rp_examples_draft_v1.md` 生成）：
  - **不锁死 12**：`examples.max_entries` 默认 12 → **20**（代码 `MAX_ENTRIES`、schema、离线台三处同源）；
  - **头部换文案**：`示例对话（就是这种语感，照着说）：`；删掉旧的「规则A/规则B」
    （规则B 已证伪；规则A 是错误示例 —— 它教的那条本身就是反面教材）；
  - **`[emote:]` 教学去掉劝退措辞**（「选不中就不发」「没有合适的表情时不要硬写」）——
    选图是 RAG 命中已向量化的表情包描述，属**机制行为**，不由模型把关（C2/D20）；
  - **任何情况下不回退旧示例句**：旧句（`成员甲`/`成员甲`/规则A/规则B/`口癖丙`）在这份代码里
    **一个字都不存在**；数据目录文件缺失/损坏/无可用条目 → 回落的就是新 20 条。
    新增 `ExamplesState.source`（file / bundled）并在**来源变化时打一行日志** ——
    部署时忘了替换文件，那是唯一能看见的地方；
  - 两道防漂移闸：`test_examples_source_sync`（生成物 == 草案，无 docs 时 skip）+
    **内容指纹** `tests/examples_frozen.json`（台式机唯一有效的闸）。
  - ⚠️ **草案自相矛盾，已按实际文本落地**：`rp_examples_draft_v1.md` §C 的覆盖矩阵声称
    「**8 条**带 `[emote:]`」，但 §B 的 20 行文本里只有 **5 条**。生成器**只报告不设限**
    （文案以 §B 实际文本为准，不替作者改数），已向使用者报备。
- **§4 角色表（C29）：本轮不做**。用户 2026-09-20：角色辨认依赖发色/瞳色/脸型/饰品/服装，
  而发色瞳色易混、脸型难描述、同一角色多套服装 → 他要**先收集图片再图转文**，
  已框定候选（作品名：角色名/游戏角色B/游戏角色C；蔚蓝档案：游戏角色A；鸣潮：游戏角色D（尤其 Q 版）；
  魔女的夜宴：绫地宁宁）。落点就是可编辑的 `persona/s4_world.md`，他随时可以自己加。
- 测试 746 → **755 全绿**（NOTE 渲染 4 例 + 示例块 6 例 + 生成物一致性 3 例，改写 4 例旧断言）。

### Fixed (2026-09-20, 独立核验第 4 轮：C31/C33 的 4 条非阻塞)
> 审查方对 `d343b50` / `7f2a46b` 的**冻结快照**核验（0 阻塞 + 4 非阻塞）。
> 它同时更正了自己上一轮的两条测量（测试数 746 是 `1a583e2` 的；familiarity 在 `7f2a46b` 时确实没改）。

- **N-4（最重要，防的正是刚发生过的漏改）**：`tests/test_familiarity.py` 的两条夹具
  **仍在喂旧键 `summary`** —— 夹具与实现同源、互证「正确」，于是 745 条全绿却漏掉了
  `familiarity.py` 里一处裸读 `summary` 的真缺陷（生产会变成**空日记**，不报错）。
  这正是 `BUGS.md` B-037 盲区②的**原样复发**。夹具改成喂 `body`，并在类 docstring 里
  写死这条教训：**字段改名这类改动，风险不在实现而在夹具。**
- **N-3**：`body_missing` 两条写侧不对称 —— 周期侧有、日记侧没有。而没有 body 的记录
  会被读侧**完全丢弃**（`list_diaries` 要求 body 非空）→ 这一天对周报/做梦不可见，
  且**无任何日志**。日记侧补齐 `body_missing` / `body_from_digest`，并在两者皆空时告警。
- **N-1**：`world_block` 构造时快照 → 与其它段「每次现读」不一致（改了 `s4_world.md`
  要重启才对梦生效）。改为 `world_block_fn` **惰性取用**（取不到回落构造时快照）。
- **N-2**：`test_budgets_are_generous` 的 docstring 仍在引用已删除的锚点阶段。
- 测试 746 → **746 全绿**（夹具与断言改写，无新增用例）。

### Fixed (2026-09-20, 独立核验第 3 轮：静态闸判据换 AST + N-4 范围声明)
> 审查方核对 `acba8e5`（0 阻塞 + 4 非阻塞）。C21 这条线已关闭（三条验收 + CHANGELOG 更正到位）。

- **🔴 我的静态闸判据太松，被变异测试证伪**：`TestInstrumentationHasReaders` 第一版对整文件
  文本做**子串**匹配 → **注释与 docstring 里的字段名也算"有人读"**；审查方删掉两处真读、
  只留注释，闸门**仍然绿**。判据松了等于没有闸门 —— 而这正是它要防的那类失效。
  修法：换成 **AST 判据**（`reads_of()`：属性 load / `getattr(x,"f")` / `d["k"]` / `d.get("k")`），
  `ast` 天然不含注释。自证用例也重写成**双向**：合成模块"只在注释/docstring 里提字段" → 必须判无读者；
  再加一处真读 → 必须认出来（旧的自证是同义反复：断言一个全仓不存在的字符串找不到读者，恒真）。
  我在隔离副本上复跑了审查方的变异：**已精确报红**那一对字段。
- **N-4 范围声明**：Gate 头部的留痕出口目前只在 `initialize()` 的启动自检里跑一次，
  而 `gate_system_prompt()` 是**逐轮现读**段文件的 —— 当前够用靠的是"空文件回落内置默认"
  这条**无人守护**的不变量。已在代码注释里写明覆盖范围与失效条件，
  并要求 **C22 接线时把出口挪到 Gate 调用点写 per-turn trace** `trace["gate_head_fallback"]`。

### Fixed (2026-09-20, 独立核验第 2 轮：一处**记录不实**的更正 + R1–R8)
> 审查方核对 `0618029` + `0055db9`（0 阻塞 + 8 非阻塞）。**先记一条本项目的自伤**：
> 上一条 CHANGELOG 把 **C21 记为「已完成」，但代码里一个字都没改** ——
> 我读了死代码、计划了删除，然后被别的事打断，**没有执行就写进了变更记录**。
> 审查方用 `git show` 逐个提交核对后发现（`_build_alias_block()` 仍在原处）。
> **教训：变更记录必须由 `git show` 复核，不能凭计划写。**

- **C21 真正落地（B-041）**：删除 S9 拆分后残留的死代码 `StyleProfile._build_alias_block()`
  （全仓零调用点）；两处不同文案的图谱表头**只剩生效的那一份**（`relations_block()`）。
  删除用脚本做边界校验（锚点存在 / 区间内只有一个 `def` / 行数减少 / 关键符号未丢），
  不是手工框选。
- **R1（留痕回归）**：`kg_display` / `kg_tail` 改为**无条件**写 trace —— 此前
  「display=1 且 kg_provider=None」（离线/未接线）时两个键都不写，比改造前还少。
- **R2（A/B 臂不可复现）**：离线测试台 `tools/replay_scene.py` 现在与 main 同源读
  `rag.display_enabled` —— 此前它不传 `kg_display` → 恒为关，而 schema 明写这个开关
  「用于 A/B 与归因」，那一臂根本跑不出来。
- **R3**：给 `kg_display` 的构造默认值补了用例（此前把它改成 `True` 的变异**无人发现**）。
- **R4（留痕无出口）**：`last_summary_fallback` / `last_gate_fallback` 曾经「设了没人读」，
  docstring 承诺的「日志里看得见」没兑现。现在：总结侧新增统一出口
  `main._summary_system_prompt()`（日记与周/月/年记都走它，回退时 warning）；
  Gate 侧在**启动自检**里取一次头部并报回退（C22 接线前它没有别的消费点）。
  另加静态闸 `TestInstrumentationHasReaders`：用 **AST** 找真读（属性 load / `getattr` / 字典键），
  **没有真读的降级字段判红**。⚠️ 第一版判据是子串匹配，审查方变异测试证伪（删掉真读、
  只留注释，闸门仍绿）→ 换成 AST 后同一变异**精确报红**（已双向自证）。
- **R5**：残留的「文件存在即覆盖」措辞（docstring / 注释 / 段文件 README 的「mtime 热重载」）
  全部改成与代码一致的说法。
- **R7**：删掉收下即弃的 `persona_manifest(used=…)` 参数与两个死属性。
- **R8**：物化时**部分文件**写失败（目录可写、单文件写不进）此前完全静默 → 现在留痕。
- 测试 726 → **731 全绿**。

### Fixed (2026-09-20, 独立核验第 1 轮：C7/C10 的 2 阻塞 + 11 非阻塞全收)
> 审查方对 `ebb8f1e` 的**冻结快照**逐行核验（报告 `scratch/prompt_v2_recon/REVIEW_round1_C7C10.md`）。
> 两条阻塞都不是功能 bug，而是**仪表盘说谎**：会让运维得出错误结论。

- **🔴 trace 每轮误报降级（B1）**：`manifest()` 用 `used` 反推 `missing`，把「**按设计省略**」
  （§3 在没有记忆正文时整段不出现，§4.4）与「**段丢失**」混成一类 → 从部署当天到 C4 落地之间，
  **100% 的轮次**都带 `persona_degraded`，而自检的排查指引（"检查 persona/*.md 是否被清空"）
  指向的文件其实是满的。**恒亮的降级信号 = 没有信号** —— 与「降级必须可见」的初衷相反。
  修法：`manifest()` 拆出 `omitted`（按设计省略）/ `missing`（真丢失），消费侧只看后者；
  自检对 `omitted` 打 info、对 `missing` 打 warning。
- **🔴 回退开关的仪表盘说谎（B2）**：启动日志把 `mode=v2` **写死在 f-string 里**，
  且 `persona_manifest()` 在 legacy 下仍返回 v2 段清单 → 切到 `sections_mode=legacy` 时，
  日志会报「v2 全套已生效」（而实际生效的是旧装配）→ 会得出「回退没生效」的错误结论。
  `legacy` 是本批承诺的**唯一归因退路**，读错就等于没有退路。修法：日志取 `_pm['mode']`；
  legacy 返回 legacy 视图（`used=[]` + `note` + 真实 `assembled_chars`）。
- **11 条非阻塞同批收掉**：`overridden` 改「真被改过」而非「文件存在」（首启物化后它曾恒为 8 段，
  信号饱和）；README/CHANGELOG 的「存在即覆盖」改成「**有内容**即覆盖」（空文件不覆盖，以代码为准）；
  `gate_system_prompt()` 空装配时退回裁判 system（**绝不返回空串** —— S13 实测空 system 让解析失败率
  0%→40~60%）并留痕；`summary_system_prompt()` 回升全量人格时留痕（否则静默取消 C32）；
  `memory_digest.json` 写坏与「还没生成」分开留痕；**去掉 mtime 缓存**（实测内核粗时钟下
  两次同尺寸写入的 `st_mtime_ns` 完全相同 → 任何 mtime 指纹都漏检；8 个小文件每次现读）；
  清单只报 `assembled_chars`（装配后真实长度），不再让启动日志与自检对不上；
  删死过滤；trace 与自检**同源**（`system_prompt()` 把算好的 texts 传给 manifest）；
  物化失败留痕。
- **补一条不依赖 docs/specs 的内容指纹闸**（`TestGeneratedContentFrozen`）：
  `test_persona_source_sync` 在台式机（无设计文档目录）会 skip，生产机因此没有「生成物==草案」
  的保障；改成对每段内容冻结 sha256 前 12 位 —— 手改生成物或重跑生成器都会红，
  强制把改动显式更新进来。
- 测试 709 → **726 全绿**（新增 `TestReviewRound1Fixes` 15 例 + 指纹闸 2 例）。

### Fixed (2026-09-17, V1 部署验证批次：B-022～B-031 —— 九修 + 一处文档对齐，全部清账)
> 来源：对生产机的**只读**验证（报告 `docs/measurements/verify_20260917.md`，缺陷登记 `BUGS.md`）。
> 验证窗口 2026-09-16 13:38 → 09-17 19:34（当前进程生命周期）；部署 `05981a2` 与开发机 HEAD 的
> 全部 `.py` / `_conf_schema.json` **逐字节相同**、620 测试全绿 —— 即"代码即计划"，但行为层
> 仍挖出 9 条缺陷。共性：**"设计写了、实现没做到"**与**"记录不到"**两类，与项目反复栽的
> 静默失效同族。每条**先写失败测试**（TDD）再改；测试 620 → **652 全绿**
> （随后独立核验又补 6 例 → **658**，见下方跟进小节）。

- **🔴 周/月报读的是废弃的根目录日记 → 关系提升提案永久失效（B-024，`525b015`）**：
  `summary.py::collect` 读 `<data>/daily_diary.jsonl`（停在 2026-08-29），而日记自 `e566116`
  （09-08）起写在 `logs/<gid>/`。生产实证：09-14 的 W37 窗口内已有 09-11/09-13 两条日记，
  记录却是 `n_diaries=0`；连带 `_propose_relations` 的 `not diaries` 守卫恒真 →
  **S12 整套熟悉度/关系提升提案从不产出**（`familiarity_proposals.json` 至今不存在）。
  修法：新增 `read_diaries()` 双路径（口径同 `dream.py::gather_diaries`：现行优先、同日取较新）。
  **真实生产数据复验：`n_diaries` 0 → 2**
- **🔴 关系图谱头部块未冻结 → 吃掉 32.6% 的全价 token（B-022，`d25d64d`）**：
  `_assemble_base` 每轮现算活块且位于 session **之前** → 图谱随新成员入列增长（实测一天 8~11 次）
  就把它后面 4–9 万 token 的前缀缓存全部作废。实测 13 次变更事件共 777,441 全价 token
  （`common_prefix_chars` 恒为 793(gate)/1703(rp)，断点恰在"群友识别"块）；
  独立核验按同一判据筛出 14 行/795,790（33.3%）—— 多出的一行是 02:55 的日轮换冷启动，
  与图谱变更无关，两口径已对齐。
  S10 的设计（"旧块留在前缀里不动 + 增量追加到尾部"）此前只实现了后半句。
  修法：`SessionManager.freeze_relations_block()` 首次冻结、之后恒返回冻结值
  （随 session 落盘、跨重启不变；轮转即重置）；顺带修掉"同一变更被 LLM 看到两遍"。
  端到端复验（管线 + 真 `SessionManager`，活块连涨两轮）：前缀里的图谱块三轮**逐字节相同**、
  活块新内容未进前缀、相邻两轮 messages 公共前缀 4/4。
  ⚠️ **已知取舍**：`relations_delta()` 不上报**删除**，故人工删行的效果会延迟到下一次轮转（≤24h）
- **🔴 `/admin status` 必崩（B-023，`5ea0214`）**：`snapshot()` 有**两个形状**的返回值，
  无 group_id 分支不含 `hourly_used`/`current_hour`，而 `_admin_status` 正的正是它
  → `KeyError: 'hourly_used'` → 管理员只收到 `:( 在调用插件…时出现异常`（09-15 起存在，
  命令改名后跟着活下来，620 测试无一覆盖）。修法：服务层把键契约钉死（无参分支 = 多群峰值）+
  main 改用 `snapshot(self.target_group_id)`（语义本就该是目标群）
- **轮转归档丢会话状态（B-025，`39d265a`）**：`rotate_if_day_changed()` 的归档 payload
  只写 5 键，漏了 `_save()` 早已写入的 `system_prompt`/`sys_blocks`/`next_block_id`
  → 归档日 session 的导出/离线复核里**人格整条消失**（实测同一导出工具：归档文件命中人格 0 次、
  在写文件 1 次）。必须在 `sess.clear()` **之前**取走这些值
- **可变块标记被恢复路径剥掉 → 每次重启静默丢块（B-031，`39d265a`）**：
  `load_all()` 用 `_public()` 清洗条目，而它按"下划线开头即剥"处理 →
  `__sys_block__` 标记被销毁，内容留在 `sys_blocks` 里却无人引用 ——
  **所有［设定更新］块会在下一次重启时无声消失**。（随 B-025 的往返测试发现）
- **`[r:-N]` 编号基漏掉可变块占位 → 整体偏移一位、静默引用错人（B-026，`e4f4db2`）**：
  `get_messages()` 把可变块物化成 system 消息（LLM 看得见），`quote_entries()` 却整条跳过
  → 编号基比 LLM 所见少一条（实测 4 vs 2）。修法：块在编号基里占**空三元组**位（不可引用但
  编号不塌陷，与既有设计一致）。⚠️ 生产上 2/5 引用解析失败经核实**不是缺陷** ——
  模型把 `[r:-1]` 指到了尾部的关系增量块，该槽位不可引用 → 保守放弃引用（有测试锁定）；
  残余属提示词层，转 STAGE3
- **`cache_stats` 水位取整回退 → 重复计数（B-027，`958ac42`）**：水位存 `round(ts,1)`
  而筛选用原始 ts → 上次最后一行被反复写入（**没新数据也每次 +1**）。生产实证 367 行仅
  363 个唯一 ts，UTC 日汇总被污染（09-14 calls 82→80、hit 0.9155→0.9121）。
  修法：水位改存原始 ts + 按 ts 去重兜底 + `compact()` 保留 `kind`（RP/Gate 分线）。
  **--rebuild 复验：401 行 / 401 唯一 ts / 0 重复**
- **生成失败在 trace 无痕（B-028，`ba8b068`）**：空生成只写进 `SendIntent.silent_reason`，
  探针又在 main 的 except 里提前 return → 86 次 Gate 放行只有 83 次能在 trace 里对上。
  修法：`trace.generation_attempted` + `trace.llm_error`（异常成因经 meta 回传），
  `trace_stats` 漏斗拆成 生成尝试 / 生成失败 / 生成成功
- **🔴 思维链留存从未生效（B-029，`34af53c`）**：代码读 `reasoning_content`，而网关把思维链
  放在非标准字段 **`reasoning`**。AstrBot 的 openai 源确实会赋值 `reasoning_content`
  （`:876-878`），但它只按 `self.reasoning_key`（默认 `"reasoning_content"`，`:399`）取属性
  （`_extract_reasoning_content` `:700-724`）→ 非标准字段名永远取不到。
  实测同一模型同一参数：`reasoning_content=None` 而 `reasoning` 非空（140 字符）、
  `reasoning_tokens` 44（不同调用 27–58 波动）。**（根因表述已按独立核验更正）**
  后果：401/401 次探针 `reasoning_chars=0`、6 个 session 文件 `_reasoning` 全为 0，
  导出恒显示"思维链：无"—— **看起来像模型没思考**，而计费里有 60,482 个 reasoning token。
  修法：新增纯函数 `llm_params.extract_reasoning()`（标准字段 → 网关 `reasoning` →
  `reasoning_details[].text`），main 两处取值改走它；导出改为"未留存"并注明
  **不代表模型没思考**。真实响应端到端复验：0 字符 → 83 字符

### Fixed (2026-09-17, V1 独立核验跟进：B-032～B-035)
> 独立核验子代理对 B-022～B-031 逐条**证伪**，给出 6 个反例 + 5 条回归风险 + 9 处登记册不一致。
> 其中 4 处是真缺陷，本节收口；其余为登记册表述问题（已在 `BUGS.md` 逐条更正）。

- **🔴 冻结块在会话边界仍重复播报（B-032）**：日轮转后新会话首次冻结用的是**当时活的**图谱
  （已含新成员），同一轮增量又按 `known` 追加一次 → 之后每轮 head 与 tail 各讲一遍，
  且尾部那条留一整天。修法：新增 `StyleProfile.filter_already_announced()`，
  头部已写着的行不再追加（"有变化但头部已讲过"时仍推进 `known`，且只在真有差异时写盘）。
  同时修一个**静默回归点**：`relations_block_state.json` 被删/重置时，
  头部冻结块与 `known` 之间的差异对 LLM 永久不可见 → 新增
  `_align_frozen_relations_block()` 重冻对齐并 warning 留痕
- **生成失败成因误标（B-033）**：`provider 为空（根本没调 LLM）` 与
  `错误响应被抑制` 两条路径都记成 `empty completion` → 各自写自己的成因，
  `empty completion` 只留给真正的空生成
- **无参 `snapshot()` 两数跨群混搭（B-034）**：`current_hour` 与 `hourly_used` 各自取 max
  → 实测返回 hour=20（G1）配 used=9.0（另一陈旧群）。改为取"用量最大那个群"**成对**返回
- **守卫测试无牙（B-035）**：74bd848 新增的用例在修复回退后**仍然全绿**（两行都算新行，
  去重没被触发）→ 补真反例用例（stats 已有同槽行 + 水位回退 + 同槽真实新调用），
  并用"临时退回 ts-only 去重"验证**确实变红**
- 测试 652 → **658 全绿**
- 说明：原 B-028「探针再落一条 failed=true」**刻意不做** —— 探针行会被 cache_stats 当成一次调用
  （cached=0/other=0 → cold=1），会污染命中率统计；失败留痕由 trace 承担（职责分开）

### Fixed (2026-09-17, V1 独立核验跟进·第二轮：B-036 + 三处加固)
> 核验者对 c776d6f 做**窄范围复核**：4 条收口全部有效、无新反例，但给出 4 条加固建议。
> 本轮全部落地，测试 658 → **667 全绿**。

- **脏 session 文件可让整个恢复流程抛错（B-036）**：`load_all` 的
  `int(k)`（`sys_blocks` 键）与 `int(next_block_id)` 不在 per-file try 内 →
  一份写坏的文件让**所有群**的会话都恢复不了（B-031 之后该路径更常被走到）。
  修法：逐键容错 + 非 dict 兜底 + `next_block_id` 解析失败回退 0
- **`filter_already_announced` 判据由"子串"改为"整行精确匹配"**：核验者构造出唯一误杀面
  （某成员别名里字面内嵌另一成员整行 → 子串判据会把真实新行当"已播报"丢掉）。
  改为按行切开做集合比对；新增用例锁住该构造（退回子串判据 → 用例变红）
- **关系增量决策下沉为纯函数** `style_profile.plan_relations_delta()` →
  `RelationsDeltaPlan(text, known, added, changed, first_run)`。核验者指出
  "B-032 的 main 侧接线没有任何测试（main.py 不被测试导入，只能靠 AST 抽取才验到）"
  —— 下沉后四条出口全部可离线单测（+5 例）
- **"没调 LLM"不再算一次生成尝试**：`provider 为空` 路径同时写 `meta["llm_not_called"]`，
  pipeline 记 `generation_attempted=False` + `generation_skipped=<成因>`
  （成因仍可见，只是 `trace_stats` 的"生成尝试"不再虚高）
- **无参 `snapshot()` 优先"当小时内"的群**：核验者指出本方法不 roll hour →
  陈旧群的大用量会胜出（实测返回 hour=3 的陈旧值）。现在只在 `current_hour == 当前小时`
  的群里挑，没有才回退全局最大

### Fixed (2026-09-17, V1 独立核验跟进·第三轮：三处最后加固)
> 核验者对 `7be6ba3` 复核确认**五条全部成立**（含 7/7 差分验证「纯函数与旧内联逻辑行为等价」、
> `load_all` 容错没跳过好数据、snapshot 三种情形仍同源）。它另指出 3 处，全部已修；
> 测试 667 → **670 全绿**。

- **格式漂移漏判**：整行严格比对在「块里行尾/行首多空白」时认不出「已播报」→
  该行被再播报一次。判据加 **strip 兜底**（不引入误杀面；别名内嵌整行那条用例仍绿）。
  新增用例验牙（去掉 strip → 用例变红）
- **main 接线仍无测试**：核验者把 `main.py` 整个回退，四个模块 155 项**全绿** ——
  正是项目注释里「踩过四次的接线漏了」。现在把**落盘/对齐决策也下沉进**
  `plan_relations_delta`：`RelationsDeltaPlan` 带 `state`（None = 不写盘）与 `need_align`，
  main 只剩「照 plan 执行」的平铺动作；四条出口的 state 形状与 `now` 可注入性逐条被锁住
- **冻结值落盘延迟**（核验回归风险 ②）：原本只在下一次 `_save`（≤50 条 / 300 s）落盘，
  窗口内重启会按**活块**重冻、白破一次前缀缓存。`freeze_relations_block()` 命中时清零
  `_last_save` → 下一次 `_maybe_save`（通常就是紧随其后的首条消息）立刻写盘。新增用例验牙
- 标签修正：`trace_stats` 的「生成失败」→「**生成未完成**」（含「连 LLM 都没调成」那一类，
  它不算生成尝试但仍是未完成）

### Changed (2026-09-17, 同批次）
- **装配顺序文档对齐代码（B-030，`159652b`）**：真实顺序是
  `system prompt → 工具语法 → 示例块 → 关系图谱 → session → **本轮块** → KG 尾注`
  （`_finalize` 用 `insert(-1, …)`）；README.md/AGENTS.md 此前把末尾两块写反且漏了工具语法块，
  `docs/services.md` 缺两块 —— 三处一起对齐
- **`InterjectionManager.snapshot()` 键契约统一**：两个分支都返回 `current_hour`/`hourly_used`
  （消除"两形状"footgun）；顺带审计 main 里其它 snapshot 调用点（sticker/session_mgr/poke），
  无同类问题
- **观测口径补齐**：`tools/trace_stats` 漏斗新增"生成尝试/生成失败"两行；
  `tools/cache_stats` 的 `kind` 透传 + `--rebuild` 可一次性修掉历史重复行；
  `export_session_md` 的思维链抬头改为"未留存（不代表模型没思考）"

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

### Changed (2026-09-14, S10 关系图谱改"增量追加"+ Gate 判定指令上移)
> 用户两条设计（2026-09-14）：
> ① "`【现在要判断的这一条】`的系统提示词不短，如果放在最前面，应当更为节省"
> ② 关系图谱应改为"**旧块留在前缀里不动，更新以新块追加到上下文尾部**"：
> `[人格][图谱v1][示例][session][system:图谱更新][session][system:再次更新]…`
> 并明确"**更新那次必然 miss 是可接受的** —— 不然就是更新不 miss，但
> 全量群聊上下文 + LLM 思维链 + RAG 示例文段全部 miss 了"。

- **① `GATE_JUDGE_INSTRUCTION` 从尾部上移到最前**（`services/gate.py`）：
  它约 600 字符（≈450 token），原先拼在**末尾那条 user 消息**里 —— 位置在
  8 万 token 的 session 之后，且后面还跟着逐轮变化的候选消息 → **每次重算**。
  移到最前 + 内容恒定 → 进入稳定前缀，**一次付清**。
  Gate 的 messages 结构变为：`[system 判定指令] + RP 共享前缀 + [user 本轮候选]`。
- **② 关系图谱改增量追加**：`StyleProfile.relations_delta(known)` 按
  `{uin: 行文本}` 比对，产出「新群友」与「关系变化」两类增量；
  pipeline 在**本条落盘之后、引用快照之前**把增量以一条 system 消息追加到
  session 尾部（`main._relations_delta_block` 负责状态与文案）。
  - **行文本变化也算增量** —— 用户明确"我可能人工把 close 调成 known"，
    否则人工调整对 LLM 永远不可见（与"熟悉度人工批准 + 只升不降"的体系冲突）
  - **首次运行返回空**（初始块已在前缀里，不该第一次就灌几百行历史）
  - 状态落 `relations_block_state.json`（原子写）
  - 效果：前缀逐字节不变，代价从"图谱之后的一切（8 万 token）"缩小到"就那一条"
- **纠正我自己的错误表述**：此前说"更新那一次整个前缀都要重算"不够准确。
  准确说法是：**图谱块之后的所有内容**（示例块 + session 全量）不匹配 → 全价重算。
  改成尾部追加后，这一段恢复逐字节命中。
- 测试 436 → **446 全绿**（新增增量语义 5 例 + pipeline 追加 5 例）

### Fixed (2026-09-14, 🔴 S9 关系图谱从人格块拆出 + 改为"只在尾部追加")
> 用户观察："人格 system_prompt 要进一步拆分，关系图谱单独拆一个块出来，
> 群关系图谱应当做成 dsh 内的 skill catalog 一样的设置，每次变动在尾部加上新的，
> 保证前缀不变。"

- **🔴 实测问题**：`system_prompt()` 末尾拼着 `alias_block`（关系图谱），
  而它随新成员入列**持续增长**（一天 8~11 次、每次约 +23 字符）。由于它在
  请求的**最前面**，每次增长都让**其后全部内容**（session 全量，实测 8 万 token）
  的缓存失效：
  ```
  提示词变更那次: prompt 65560 → cached 3840 (5.9%)  → other **61720** 全价
  正常调用:      prompt 82475 → cached 82176 (99.6%) → other 仅 299
  ```
  98 次调用里 15 次 `other>10000`，**全部发生在提示词变更那一刻**。
- **修法 ①（拆分）**：`StyleProfile.relations_block()` 独立提供关系图谱；
  `system_prompt()` 不再拼它。pipeline 装配顺序改为
  **工具语法 → 示例块 → 关系图谱 → session → KG尾注 → turn_block**
  （按"变化频率从低到高"排）。实测：人格块 **910 字符恒定不变**，
  新成员入列只让关系块 +23。
- **修法 ②（追加语义）**：原块按 `【熟人】/【认识】/【新人】` **三段分组**输出，
  块内顺序与文件顺序不一致 → 中段插入让其后全部位移。改为**严格按文件顺序**输出：
  实测 `member_relations.json` 本身就是追加式的（`[0..109]` 人工策展，
  `[110..184]` 全部 `auto_added` 新成员）→ **新块以旧块为前缀**（已验证）。
  取舍：不再有分段标题，但每行的 `[熟人]/[认识]/[新人]` 标签保留，亲疏信息不丢。
- **澄清一处认知**：`system_prompt_fragments.json` **本就有 `relations` 字段**
  （手写的关系风格，如 `成员甲: "略带调侃的贴贴，会用'成员甲的小名'等别称打趣对方"`）
  —— 真正的关系知识在那里且基本不变；`alias_block` 是平铺的名字→QQ 映射。
- **顺带修正一处我自己的错误陈述**：此前说"Gate 收到的 messages 就是 RP 前缀本身"
  是**错的**。准确说法：Gate 与 RP **共享同一段前缀**，但**尾巴不同** ——
  RP 尾巴是 turn_block，Gate 尾巴是 `【现在要判断的这一条】+@提示+风格片段+
  GATE_JUDGE_INSTRUCTION`（后者**不在** RP 上下文里）。
- 测试 428 → **436 全绿**（新增追加语义不变式 3 例 + 拆分 5 例）

### Added (2026-09-16, S16–S18 对话质量工具链)
> 用户要求："人工看是必须的一环…关键在**提高人工看的效率**" +
> "A/B 测试需要专门设计…支持**控制变量**下的测试"。

- **`tools/export_session_md.py`（D1）**：session JSON → 人工可读 markdown。
  逐条带序号、**思维链用 `<details>` 显式标注（不进 LLM 上下文）**、
  `_mid`/`_uin` 内部元数据可见、`--tail N` 控篇幅、`--extra-from-trace` 附
  KG/RAG（LLM 能看到但不在 session 里）、`--data-dir` 把 `_uin` **回填成别名**
  （旧格式条目原本只有 QQ 号，无法阅读）。
- **思维链留存**：session 条目加内部键 `_reasoning`（用户明确"长度可接受"）。
  🔴 **存储有、发送无** —— 不进 LLM 上下文（Context Rot 信噪比退化 +
  推理链不忠实）。经可变 meta 字典从 `_generate_reply` 回传 pipeline。
- **`tools/trace_stats.py`（D3）**：统计口径固化。本轮为调查手写了 5 个一次性
  脚本，每个都要重新摸字段、重新踩"日志轮转为 `.jsonl.1`"的坑。
  关键口径：**解析失败必须与真实拒绝分开**（parse failed 是 Gate 没生效，
  不是判断拒绝）、**缓存总量 vs p50 分开**（看成本 vs 看体验）。
- **`tools/ab_prompt.py`（D2）**：离线 A/B，**控制变量**（同场景同骨架只变一个
  组件）；装配顺序与生产 pipeline 一致（否则比的是"位置"不是"内容"）；
  报出各臂差异、变多个时 warning；凭据/模型跟随 cmd_config 不硬编码。
- **实测效果（D3 立即可见）**：Gate 解析失败率 **27~31% → 0%**（S14/S15 修复后），
  Gate 通过率 24.2% → 36.6%。
- **实测发现（D2 首次实跑）**：去掉**示例块**后输出里出现了 `成员戊` ——
  而该名字**不在场景里**，却是人格提示词中固定被点名的名字（5 个字段）。
  → 示例块可能在**压制提示词里的"名字引力"**（n=1，待确认）。归档于
  `docs/specs/measurements.md §2d`。

### Removed (2026-09-15, S13 死代码清理)
> 起因：`_admin_binding` 丢失事故后新加的 `self` 属性检查器报告了 3 处
> "只写不读"，逐一查明后清理。

- **删除 `services/dream_job.py`（整个类）**：自 S11 起即为死代码 —— cron 改挂
  `_dream_job_runner`，而 `DreamJob.run` 只写不推、其"关系变更建议"已由
  `services/familiarity.py` 取代。**这也完成了用户要求的"做梦不负责熟悉度"的收尾。**
  删除前已确认全仓无代码引用（仅注释与 cron 名字含 `dream_job`）。
- 删除 `self._dream_job` / `self._decision_log_path` / `self._sleep_enabled`
  三个只赋不用的属性。
  - `_decision_log_path`：日志路径已改走 JsonStore，历史遗留
  - `_sleep_enabled`：`_is_sleeping()` **每次读活配置**，该快照无用
    （顺带确认 `sleep.enabled` 开关**是生效的**，且支持热改）
- `tests/test_self_attributes.py` 的 `KNOWN_WRITE_ONLY` 白名单清空并保留
  —— 留给将来"刻意的只写字段"，同时在注释里如实记录检查器的三处局限
  （括号续行赋值、条件分支内读取、只覆盖一个类）。
- ⚠️ **过程教训（第二次同类失误）**：清理时我又用"索引区间切割"改测试文件，
  **连续两次把 `_class_attrs` 辅助函数一起切掉**。已改为"只精准替换目标行"，
  并在操作前加 `assert` 校验关键符号仍存在。**行号/索引切割是本轮反复出错的手法，
  后续避免使用。**
- 测试 528 全绿（删除死代码不改变任何行为）

### Added (2026-09-15, S12 熟悉度体系 + /admin 收窄 + 年报)
> 用户定稿：`/admin` 统一入口；推送与权限合并；熟悉度**只升不降**、除新成员入列外
> 须人工批准、人工可绕过。

- **`/admin` 收窄**：原 7 个散落命令（persona_status / persona_wake / persona_sleep /
  reload_persona_config / bind_dream / bind_admin / dream_now）全部并入一个入口：
  `status / sleep [小时] / wake / reload / bind / dream / relations`。
- **推送与权限合并**（用户："三者是同一个 QQ…同时绑定私聊推送和管理员权限"）：
  `_admin_binding()`（admin_binding 优先、回退 dream_binding）；
  `_is_privileged()` = `privileged_qq`（bootstrap）**或**绑定会话。
  好处：不会出现"能收到推送但没权限操作"。
- **`services/familiarity.py` 熟悉度体系**：
  - `parse_proposals`：四道过滤（uin 存在 / to 合法 / **必须升级** / 同级丢弃）；
    `from` **取服务端真实值不采信模型**；垃圾输入返回空表绝不猜
  - `build_proposal_prompt`：**必含旧版关系图谱**（否则会提议已是 close 的人）
  - 候选集 = **日记里出现的人**（不做量化筛选 —— 用户"做成定量太死板"）
  - `ProposalStore.replace()`：**新批覆盖旧批**（不拼接）⇒ 单批内序号稳定，
    `/admin relations apply N` **不需要"序号→稳定 id"翻译层**（用户指出）
  - `decide()`：幂等；**apply 时二次校验**（防提案生成后人工改了等级导致降级）；
    人工已升过 → 报"无需重复提升"而非失败
  - `parse_indices()`：支持多序号、**不支持区间**；非法输入分类报错
- **`StyleProfile.set_closeness()`**：写入层第三道"只升不降"守卫；
  `force=True` 给人工路径；原子写、只改 `closeness` 不动其他字段
- **年报**：`yearly_window` = 上一个完整自然年，**读 12 篇月报**。
  🔴 层级选择的关键是**刻度可否整除**：日→月可整除、月→年可整除，故无错位；
  **周与月不可整除** → 周报是**旁支不进主链**（强行让月报读周报会让跨月那一周
  令两个月都"不完整"，且误差无法在月层级修正）。
- **字段迁移**：`notes` 让给"群友描述"，bot 标记迁到 `kind`；
  `is_bot_member()` 双字段兼容（成员表里有 2 个 bot 账号混在真人中间，
  漏判会把它们暴露给 LLM）；`tools/migrate_member_fields.py` dry-run 默认 + 双写。
- 🔴 **修两个我在本次重构中自己引入的 bug**（都是"改旧代码时漏看消费方"）：
  1. **误删 `_apply_live_config` 的定义**（只留调用点）—— 静态检查查不出
     `self.xxx` 缺失，只有真跑 `/admin reload` 才炸
  2. **`_sleep_override` 类型不匹配**：写元组、读字符串比较 → **定时睡眠完全不生效**
     （静默失效无报错）。统一为 `None | "awake" | ("sleep", 到期戳|None)` + 到期自动恢复
- 测试 471 → **524 全绿**

### Added (2026-09-14, S7 主动戳人：`[poke:名字]` → 严格解析 → group_poke)
> 用户拍板：接口用**名字**不用 QQ 号；解析**严格**（"模型用了未收录的昵称时戳不
> 出去"可接受）；候选池仅 `close`；同人冷却**复用被动计时器**。

- **接口设计的关键取捨**：`[poke:QQ号]` 要求模型从 185 人名单背出号码 ——
  比"叫出昵称"难得多，而**戳错人是对外可见的社交事故**。实测模型能准确叫出
  `成员乙`/`成员丙`，故改为 `[poke:名字]`，服务端解析。
- **`StyleProfile.resolve_member_name()` —— 严格优先于宽松**：
  精确匹配 `alias` → 精确匹配 `other_names` → 否则返回 None（**不猜**）。
  🔴 修掉一个既有的静默错配：`resolve_uin_from_name()` 是**首个匹配即返回**，
  实测真实数据里 `成员壬` 同时属于 `成员壬` 与 `成员癸` → 旧函数会静默选第一个，
  而这正是"戳错人"的成因。新函数遇到歧义**拒绝**。
- **`PokeService.decide_proactive()` 硬闸**（任一不过 → 静默跳过戳、正文照发）：
  `proactive_disabled` / `no_target` / `self_poke` / `not_in_pool` / `conflict` /
  `serious_context` / `cooldown`（**与被动共用计时器**）/ `proactive_hourly_cap`
  （与被动**分开计** —— 被动是社交回应、主动是自主行为，混计会互相挤占）。
  **被拒的尝试不推进冷却、不占配额**（只有成功才记账）。
- **`poke_log.jsonl` 增加 `direction`/`raw_name`/`resolved_via`** ——
  事后可核对"模型想戳谁 vs 实际戳了谁"。
- **按 action 通道直发**：主动戳用 `bot.call_action("group_poke", ...)`，
  不走消息段（段通道在协议端被静默丢弃）；**不需要 `stop_event`**
  （那是被动回戳为阻止内置 LLM 兜底才要的）。
- **修两个既有缺陷**：
  - `poke.hourly_cap` 原为**硬编码 4**，WebUI 里调它完全不生效（死配置，与 B-010 同类）
  - `PokeService._roll_hour()` 加 S7 字段时**漏重置主动计数** → 主动配额
    **一天只重置一次**（表现为"只在当天最初几小时能用"）。测试抓到。
- 测试 397 → **417 全绿**（新增 `tests/test_poke_proactive.py` 19 例）

### Fixed (2026-09-14, 🔴 S6 识图：自由文本格式导致 69% 的描述拿不到 + 降采样缺失白烧 7 倍时间)
> 956 次实测标定（串行、0 次 429）。子代理产出
> `tools/calibrate_vision_params.py` + `data_out/vision_calib/`。

- **🔴 失败机理（决定性，191/191 无例外）**：所有 `content` 为空的响应
  `finish_reason` **全是 `length`**，且 `reasoning_tokens ≈ max_tokens`。
  **不是**"模型把答案只写进 reasoning 就收尾"（该假设被证伪：**没有任何一例**
  是 `stop` + 空 content），而是**话没说完就被砍断**。截断处原文可证：
  「…关键词：颓废、趴桌、困倦、摆烂。**回答格式：**可见：白发戴灰蝴蝶结的…」
  —— 模型把预算花在**"输出格式谈判"**上（自由文本 prompt 没说清输出形状）。
- **修法：约定固定 JSON schema**（唯一主导因素）。reasoning 从"顶格"降到
  **p50≈150**，描述可用率 **31.2% → 100%**（n=144，0 截断）。
  53 张历史失败图：现网 **15.1% → 100%**。
- **温度不是因素**：JSON 下 0.0/0.3/0.7 均 100%；取 0.0 求可复现。
- **`reasoning_effort` 必须显式发 `low`**：网关无 off/none 档，只能发 low 或
  完全不发，而**不发更糟**（p95 10.0s/max 26.1s vs low 的 5.9s/5.9s）。
- **`max_tokens` 512 → 2048**：配对实测**无代价**（同 120 张：时延中位 −0.02s、
  completion_tokens 反而 216 vs 247、reasoning 不发散 p50=131）。
- **🔴 `vision.py` 不降采样 = 白烧 7 倍时间**：同一张图 A/B 的 **prompt_tokens
  完全相同**（412 vs 412，网关侧图像 token 数固定），而时延 **4.0s → 29.6s
  均值 / 最坏 127.5s**。原图不带来任何额外视觉信息，只贡献上传时间。
  新增 `services/image_prep.py`（与离线入库工具共用），线上识图改为先降采样；
  实测 13.4MB GIF：**13383KB → 27KB、26s → 4.2s**。
- **稳定性**：现网配置 24 图各跑 2 次有 **7 图结果翻转**（真随机，机制是
  "reasoning 是重尾随机变量撞上 token 上限"）；推荐配置 **72/72 全成功、0 翻转**。
- **动图整图直送**：多帧 GIF 且 ≤1.5MB → 发 `image/gif`（保留动作语义），
  否则回退首帧降采样。实测首帧把"疯狂摇头撞桌"描述成"张嘴"。
- 新增 `services/image_prep.py` + `tests/test_image_prep.py`（20 例）。
  测试 376 → **396 全绿**（系统 python 无 PIL 时 7 例自动跳过）。
- **待办（需你在 WebUI 改）**：`vision.timeout_sec` 15 → **30**
  （推荐配置 p95=6.3s、p99=8.7s、max 26.9s，**>15s 占 1.04%**）。

### Added (2026-09-13, S3③ 出站发贴纸 + 提示词教学 + 开关 + 启动自检)
- **出站发贴纸接通**：`main._send_sticker()` —— `SendIntent.emote` → `StickerService.pick()`
  → `Comp.Image.fromFileSystem(path)`（✅ 读宿主源码确认原生支持本地文件通路，
  不需 base64 兜底）。**与正文分两条消息**发出（"一句话 + 一张表情"形态）。
- **失败一律静默跳过、正文照发**：开关关/库空/service 不可用/低于阈值/候选并列/
  文件缺失/发送异常 —— 每次尝试都落 `logs/<gid>/sticker_log.jsonl`
  （含 `intent`/`top_k` 分数/`via`/`sent`/`reason`），降级必须可见。
- **提示词教学（`tool_syntax_block`）**：把 `[emote:…]`/`[poke:…]` 语法作为
  **恒定 system 消息**注入（内容固定 → 进缓存前缀，一次付清）。
  两个开关各自控制（`sticker.teach` / `poke.teach`，默认 0）——
  **库为空时不该教**（写出来也没图可发，白占 token）。
- **`_conf_schema.json`**：新增 `sticker` 块（enabled/index_path/library_dir/top_k/
  min_score/margin/picker_*/teach）与 `poke` 的 `hourly_cap`（原硬编码 4）、
  `proactive_*`、`teach`。
- **启动自检 `_startup_selfcheck()`** —— 专门拦"配额类参数静默失效"（本轮
  hourly_budget 时区错位就是这么藏了两周）：
  ① 当前小时预算（<1.0 即"结构性静音"，因为每条消耗 1.0）
  ② 全天 ≥20 小时预算 <1.0 → 预算表整体异常（时区/量纲错位）
  ③ 贴纸开关开了但库空 → 静默不发表情；已教语法但库空 → 白教
  ④ 人格提示词体积（>20000 字符告警，它每轮都进缓存前缀）
  **只告警、不改行为**。

### Fixed (2026-09-13, S3② 贴纸阈值标定 —— 拍脑袋的初值会让"任何意图都匹配上")
- **🔴 `min_score=0.45` 比所有负样本都低**：真实库（878 张）标定结果 ——
  正样本（该选的，同主题 tags 组合查询）top1 **p05=0.737 / p50=0.846**；
  负样本（库里无对应图的日常意图，如"把碗洗了""明天开会几点"）
  top1 **max=0.618**。初值 0.45 低于全部负样本 → **每个意图都能"匹配上"某张贴纸**，
  实测「代码review通过了」会被配上 0.52 的无关表情。
  而选不中是**静默跳过** → 表现为"它发的表情总是不对"，无任何报错（B-005/B-006 同形）。
  → 改 **0.68**（落在负样本 max 与正样本 p05 之间偏保守侧）。
- **`margin=0.06` 过大**：实测该指标区分度**弱**（正样本分差 p25=0.019 / p50=0.056）——
  同一个表情常有多张近似图，top2 天然接近。原值 0.06 会把大量**本该选中的**判成
  ambiguous（实测「害羞地脸红」score 0.86 却被拒）。→ 改 **0.02**，只挡"完全并列"。
- 新增 `tools/calibrate_sticker_thresholds.py`：用正/负样本分布标定阈值；
  若噪声与弱匹配重叠，明确报"应先改描述/标签而非调阈值"。
- **贴纸库已建并部署**：959 张（4 张内容重复已跳过）→ **878 张有描述与嵌入**
  （91.6%）；失败 81 张经 8 轮重试仍未返回（文件本身可读，非损坏）。
  嵌入用**关键词段**而非整段描述（短↔短匹配）；BGE 在开发机本地算（0.5 分钟）。

### Fixed (2026-09-13, 🔴 hourly_budget 时区错位 —— 主动插话在活跃时段被完全压制)
> 开 `active_interjection=1` 后实测抓到的静默 bug，**与 B-006 同形**（行为看起来
> 像"性格克制"，实为配额恒为 0）。

- **现象**：trace 里出现 `hourly budget exhausted (1.00/0.34)`。本地 20:33 的
  可用预算只有 **0.34**，而每条回复消耗 **1.0** → **一条都发不出**。
- **根因**：`tools/analyze_style.py` 用 `dt.hour` 统计（**UTC**），文件里也写了
  `"tz_note": "Counts are UTC. The plugin should shift to its local TZ on load."`
  —— 但**下游从来没做这个转换**。于是插件在本地 20:33 读的是 **UTC 20 点**
  （= 本地凌晨 4 点）的预算。实测 `peak_hours` = `[1..16]`（UTC）= 本地 09:00–24:00，
  即**活跃时段的预算被当成深夜配额**，本地 10:00–24:00 主动插话全被压制。
  **@ 回复不受预算限制**（代码有注释），所以这个问题一直没暴露。
- **修法（双管齐下）**：
  1. **生成端**（根因）：`analyze_style` 按 `--tz-offset-hours`（默认 +8）落**本地时**，
     并写 `"tz": "local"` 标记；`tz_note` 改为 "Counts are LOCAL time. Do not shift again."
  2. **读取端**（护栏）：`StyleProfile._hourly_local()` 识别旧文件（含 `tz_note`
     且无 `tz`）并按偏移平移，使 `hourly_budget()` 与 `peak_hours()` **两个消费方
     口径一致**；新文件原样使用
- 顺带修掉一个自己的 bug：`h.get("tz_offset_hours", 8) or 8` 会把**合法的偏移 0**
  当成缺省值静默变成 +8（测试抓到）—— 必须区分"键不存在"与"值为 0"。
- 测试 351 → **357 全绿**（新增时区护栏 6 例，含跨午夜回绕、自定义偏移、peak_hours 一致性）

### Changed (2026-09-13, S4 Gate 共享上下文：让 Gate 与 RP 看到同一个世界)
> 用户判断（2026-09-13）："GATE LLM 最好也要塞进全量的群友关系图谱，并且要知道
> RP LLM 的人格设定，同时判断回不回的消息上文需要尽量长，否则 GATE 本身会降低
> 回复质量"；"会话式的好处在于同样能塞进全量的上下文"。
> **实测把这个判断落到了具体形态**。

- **前提修正（实测）**：`system_prompt` **已含全部 163 人的别名关系块**
  （163 个别名中 157 个命中）—— 所以"关系图谱"本来就在提示词里，且已在 RP
  的缓存前缀中。**Gate 的问题不是缺图谱，而是根本没拿到 RP 的 system prompt**
  （改造前只有 740 字符小 prompt + 15 条窗口，看不到人格与别名）。
- **`PersonaPipeline._assemble_base()` / `_finalize()` / `shared_context()`**：
  抽出 RP 与 Gate **共用**的上下文装配。不变式（已加测试锁住）：
  **`shared_context(group)` 必须是 RP `contexts` 的逐字节前缀**。
- **`GateService` 共享上下文模式**：`decide(..., contexts=[...])` → 前缀原样传入，
  末尾只追加一条判定指令。两个收益：
  ① **质量**：Gate 判断依据与 RP 同级（人格 + 别名网 + 全量 session）
  ② **成本**：网关前缀缓存被 RP/Gate 两次调用复用（否则每次全价重发 ~2.8 万 token）
  实测单次提示词 ~1020 → **~4898 tok**，但其中 4291 tok 是**共享缓存前缀**。
- **`shared_system_prompt`**：Gate 的 system prompt 改为 RP 的人格提示词；
  为空时自动回退旧的 `GATE_SYSTEM_PROMPT`（不因缺配置而失效）。
- **🔴 收紧判定维度（修一个实测抓到的静默误杀风险）**：真机抓到 Gate **自行引入
  未声明的拒答维度** —— 它把「你怎么知道我昨晚只靠郊狼的环就把自己电🐍了」
  判为「内容涉性暗示，不宜回应」。该结论本身无害（那条确实没 @ 机器人），
  但同类判断作用于**该回的消息**时会变成静默误杀。新增 `GATE_JUDGE_INSTRUCTION`：
  明确"只判值不值得接 + 是否冲突"、"**不评判话题本身的内容与尺度**"，
  并重申"玩笑互怼要判非冲突"。
- **`gate_log` 补时间戳**（`ts`/`ts_epoch`）：此前该文件**没有时间戳**，
  导致无法统计决策到达率与缓存命中率随时间的变化 —— 实测排查时只能靠
  外部监视器估算窗口。
- 测试 337 → **345 全绿**（新增 S4 共享前缀不变式 9 例）

### Added (2026-09-13, S3② 贴纸库：离线入库工具 + 选择器)
- **`services/sticker.py`（`StickerService`）**：把 `[emote:意图短语]` 匹配成一张贴纸。
  - **复用已有 BGE**（`embed_via_rag(rag)` 从 `RagService._ensure_backend()` 取），
    **不加载第二份模型** —— 台式机 7.7 GB，AstrBot 常驻 ~2 GB（含 BGE）
  - 索引**预计算 embedding** → 线上每次只嵌入一句短语；余弦用 numpy/纯 python，
    **不引入第二个 Chroma 集合**
  - **默认纯 BGE 选择**（top1 且与 top2 分差 ≥ margin 直接用，省一次 LLM）；
    只有候选接近时才走可选 LLM 精选；仍接近则**保守跳过**（宁可不发，也不发错）
  - 失败原因细分并可观测（S0 的教训）：`empty_intent` / `index_missing` /
    `empty_library` / `no_embed` / `below_threshold` / `ambiguous` /
    `picker_no_choice`，全部附 `top_k` 分数供事后调阈
  - 索引 mtime 热重载（人工文件，改完不该重启）
- **`tools/build_sticker_index.py`**：离线入库
  - **扫描阶段按 sha256 去重**（同图改名不重复入库）
  - **增量且保留人工修正**（红线 #3：工具只建议不覆盖）；`--force` 才重算
  - 视觉描述**并发 2**（4 核机器别打满）；`--review` 打印审核表（描述错了会
    "该发害羞却发了嘲讽"，入库前必须有人看一眼）
  - 无 desc 的条目会醒目标注"基本选不中"
- 测试 314 → **337 全绿**（`test_sticker.py` 23 例）

### Added (2026-09-13, S3① 动作链路协议：意图标记解析)
> 设计文档：`docs/specs/s3-action-channel.md`（2026-09-13 立项，用户定调"先落地代码与协议"）

- **`text_style.extract_tool_intents()`**：解析并剥离 `[emote:意图短语]` / `[poke:QQ号]`。
  规则：一条回复最多一个动作（多个取第一个）；空意图/超长/非法 QQ 视为无效；
  **无论是否解析成功，标记一律剥离**（B-012 的教训：泄漏进群就是乱码）。
- **pipeline 接线**：与 `[r:-N]` 同理，在 **`postprocess` 之前** 提取 ——
  postprocess 会剥离工具标记（S0 预置的防泄漏规则），之后再取就没了。
  结果写入 `SendIntent.emote` / `.poke`（这两个字段自 A7 起预留，**首次真正被赋值**），
  trace 补 `tool_intents`。
- **本阶段零行为变化**：提示词尚未教这两个标记 → 模型不会写 → 线上无差异；
  main 也尚未消费 `emote`/`poke`（发送在 S3③④）。
- 测试 307 → **314 全绿**（含"与 `[r:-N]` 并存"、"无效标记也剥离"、"不依赖 postprocess"）

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
- **🔴 B-001 `[r:-N]` 引用目标错位（线上事故 11:45，群 100000001）**：LLM 按**它当时看到的上下文**编号，旧实现 `ContextBuffer.quote_target()` 却在生成结束后对**实时** buffer 求值 —— 生成窗口（5–8s）内每进 1 条消息编号整体偏移 1 位（该群 100–400 条/时 → 错位概率约 1/3–1/2）。修复：新增 `QuoteIndex`（**不可变引用快照**）+ `SessionManager.quote_snapshot()`，在**建上下文之后、发起生成之前**冻结编号基，生成结束后对它求值；`message_id`/`sender_uin` 随条目入 session（内部键 `_mid`/`_uin`，所有对外出口剥离，**绝不进 LLM 请求**）。附带消除次要错位：编号基与 `get_contexts()` **同源同过滤**（旧实现 session 含 assistant 条目而 buffer 不含，且 buffer 含被媒体过滤掉的纯图消息）。新增审计字段 `trace.quote_n/quote_id/quote_resolved/quote_basis/quote_target_alias/quote_target_uin/quote_target_missing` —— 此前 trace **没有** quote 字段，11:45 那次只能靠人工比对 SnowLuma 日志
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
- 迁移计划书 `docs/specs/llbot-migration-plan.md`；调研报告 `docs/protocol/llbot/llbot_onebot11_compat_research.md`、`docs/protocol/llbot/llbot_plugin_platform_audit.md`

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
- **G12 TopicBank 主动话题**: `services/topic_bank.py` + ACTION_TOPIC 接线 — IMPLEMENTATION_PLAN §10 评分（0.45·silence + 0.25·priority + 0.20·context_hints + 0.10·freshness）；`topic_bank.json` mtime_ns 热加载；发送后归档 `topic_sent.json`（追加、原子写）；无可发话题绝对沉默；冷场触发走 `llm.temperature.cold_start=1.1` LLM 改写 + `context.send_message` 主动发送（group UMO）；决策日志 extra.topic_id/sent；建议稿 `生产插件数据目录（**生产是唯一真身**）`（8 条，人工可改，历史坏例置 enabled=false 即规避）；`topic_bank.enabled=0` 不触发（Day3 开启）
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
- 描述失败时注入「（配图：无法识别）」诚实占位，杜绝主 LLM 对未见图凭空猜“角色”类幻觉
- 事件循环防泄漏加固：生成锁分支 / LLM 失败 / 空回复三条早退路径补 `stop_event()`，避免 AstrBot 核心兜底回复将错误文本广播到群
- 说明：此前「LLM 响应错误」外泄为 AstrBot 核心默认 agent 行为（非目标群未处理消息 + 图片多模态调用失败），非插件代码泄漏；生产切换（test_mode=0）后目标群由插件接管可避免，或在 AstrBot WebUI 配置 `provider_ltm_settings.image_caption_provider_id` 指定视觉模型
### Fixed
- **AstrBot 占位标记泄漏**：`_clean_message_text`/`_postprocess` 新增剥离 `[图片: 文件名]`、`[表情...]`、`[ComponentType.X]`（实测 02-21 回复中图片文件名被模型复读的根因）
- **指令别名**：`/persona_awake` 与 `/persona_wake` 均可唤醒（此前仅后者注册，实测名字不匹配导致无响应）
### Added
- **睡眠窗即时开关（2026-08-24）**: `/persona_wake`（特权，测试期唤醒）/ `/persona_sleep`（恢复），内存态即时生效免重启；`_is_sleeping()` 改为每消息热读配置（WebUI 改 sleep.* 亦即时生效）
### Fixed
- **G14 A/B 首轮实证精修（2026-08-24）**: 注入块头部加「规则A/B」——口癖甲 仅限肯定/恍然大悟（Phase1 实测泛滥至 4/10 次）；谐音问候仅整词触发（实测“鸡没醒”被误回早上好~）
### Added
- **G14 静态注入落地（2026-08-24）**: `services/examples.py`（纳秒 mtime 热重载，A/B=改文件名零重启）；注入位置=会话与 KG 尾之间（内容恒定，前缀缓存稳定）；`examples.{enabled,max_entries}` 可配；终稿示例 13 条（人工评审 v2 全量并入：极简单发/暴力萌/胡言乱语/无括号动作），文件 `生产插件数据目录（**生产是唯一真身**）`（旧版已备份 .bak.20260824）；测试 +4（46 全绿）
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
- **当前说话人动态行（命名识别修复）**: `_generate_reply` 在 KG 前注入 `【当前说话人】QQ xxx，群内别名「yyy」…` 提示（每说话人恒定、不污染前缀缓存），并要求不臆造他人别名；根治 2026-08-23 实测「叫不上名字/误称成员戌成员戊」类缺口
- **member_relations 风格源标注**: 小明条目增加 `is_style_source: true`（不影响别名块渲染，系统提示词哈希实测不变，缓存不失效）
- **换行折叠（postprocess 强制执行 prompt 禁令）**: 回复中的多段换行按标点感知合并为单行（`x\n\ny` → `x，y`），修复 2026-08-23 实测 6/6 回复带换行的系统性违规
- **member_relations.json 补风格源条目**: `234567 → 小明/小明玛奇朵`（人工格式追加，携 `.bak.20260822` 备份；修正此前机器人臆造他人别名「成员戌/成员戊」称呼的根因）
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
- `member_relations.json`: 成员子 / 成员丑 标记为 bot
- `main.py` `_KOUPI_LIST`: 移除 `成员A`(人名), 新增 `口癖乙`
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
