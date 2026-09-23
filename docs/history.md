# 协议端与部署演进史

> 自根 `AGENTS.md` 下沉（2026-09-16）。**部署现状权威说明见根 `DESKTOP_STATE.md`**；
> 协议端研究见根 `docs/protocol/` 与 `docs/riskcontrol/`。

## 协议端演进

```
NapCat（2026-08-25 生产 → 08-30 关闭：o3HookMode=0 两段会话均被结算）
  → LLBot v8（09-10 试点 → 09-12 停用：MAC 协议白名单限制）
  → SnowLuma v1.14.15 + 真实 Linux QQ 3.2.32-52194【现役】
```

历史文档见根 `docs/archive/`；协议端源码级分析见根 `docs/protocol/`。

## 改造批次

| 批次 | 内容 | 提交 |
|---|---|---|
| A1–A3 + A7 | 双层 LLM / 可观测 / 参数矩阵 / 安全阀 / 离线测试台 | b730b24（171 测试）|
| S0–S3(R2) | 四个静默失效缺陷 + 引用错位 + 前缀缓存重排 | 50ddb98 / a8e312e（249 测试）|
| S14–S18 | system prompt 进会话（追加式）/ RP-Gate system 分离 / 精确 token 对账 /
思维链留存 / 质量工具链 D1–D3 | 见 CHANGELOG |
| C1–C33（提示词 v2 批次一/二）| 人格装配（C7/C10）/ KG·RAG 展示下架 + 引用改打标制（C16/C17）/ 图谱 NOTE 与示例块（C5、C1+C2）/ 摘要 digest+body 与 dream 四项（C31/C33）| 见 CHANGELOG（09-20）|
| C 批次三「行为放开」 | emoji/@ 放开（C15）/ 纯贴纸回复（C18）/ 分时段 + PHI 一行（C3）/ Gate 头部独立化（C22）/ `{want, blocked}`（C23）/ **emotion 改代码计算、无 LLM 调用**（C24）/ 废文生图（C25）/ **GIF 一律抽帧拼网格**（C27）| 667d806（863 测试）；上线前审查收尾 ec0cac6 / 3f842ed / 657ee9d |

> 本表只到批次三上线（2026-09-21）。其后的线上观察与审查收尾（B-055/B-056、批次三 死代码/文档清理）
> 见 `CHANGELOG.md` 的 `[Unreleased]` 段 —— 那里才是逐条权威记录。
