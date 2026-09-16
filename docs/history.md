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
