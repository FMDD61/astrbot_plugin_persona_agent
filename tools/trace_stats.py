#!/usr/bin/env python3
"""trace_stats —— 把统计口径固化成代码（D3，S17）。

## 为什么需要它

2026-09-15 那一轮为调查 B1/B2/Q1，我**手写了 5 个一次性脚本**。每个都要：
重新摸索 trace 字段名、重新踩"日志被宿主轮转为 `.jsonl.1`"这个坑（不读 `.1`
会把 59MB 历史误判成"日志被清空"）、重新定义口径。

后果：结论**无法复现、无法跨时间比较**（调参前后 / 改动前后比不了）。

本工具把口径**写进代码**，这样"这次和上次"可比。

## 口径定义（此处即权威，不必再口头解释）

漏斗（每条 trace = 一条进群消息的处理）：

| 环节 | 判据 |
|---|---|
| 入站 | trace 条数 |
| 硬闸放行 | `hard_gate.action == "reply"` |
| 过 Gate | `gate.reply == true` |
| 生成尝试 | `generation_attempted`（旧行退回 `raw_generation`/`final_text`） |
| 生成未完成 | 存在 `llm_error`（空生成 / 异常 / 连 LLM 都没调成 —— 第三种**不算**生成尝试） |
| 生成成功 | 存在 `raw_generation`（= 旧口径的"生成"） |

比率：

- **Gate 拒绝率** = 1 − 过Gate / 硬闸放行
- **解析失败率** = `gate.reason` 含 `parse failed` / 硬闸放行
  ⚠️ **必须与"真实拒绝"分开统计** —— 解析失败不是"模型判断拒绝"，而是
  **Gate 根本没生效**（S10 事故里它占拒绝的 54%，混在一起会得出完全错误的结论）
- **点名率** = 生成文本含任一成员别名的比例（需要 `--data-dir` 载入成员表）

缓存（需 usage）：

- **加权命中率** = Σcached / Σ(cached+other) —— 反映**成本**
- **p50 命中率** —— 反映**典型体验**
  两个会讲不同的故事（实测 09-13：总量 74.2% 而 p50 95.9%）

## 用法

    python3 -m tools.trace_stats --data-dir <plugin_data> --group 100000001
    python3 -m tools.trace_stats --data-dir ... --group ... --since 2026-09-15
    python3 -m tools.trace_stats --data-dir ... --json      # 机器可读
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def load_jsonl_with_rotations(path: Path) -> list[dict]:
    """读 jsonl，**连同 `.1` 轮转文件**（宿主会轮转；实测 `.1` 有 59MB）。

    坏行跳过、文件缺失返回空 —— 绝不抛。
    """
    out: list[dict] = []
    for p in (Path(str(path) + ".1"), Path(path)):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict):
                out.append(r)
    return out


def _rate(n: int, d: int) -> float:
    return (n / d) if d else 0.0


def load_alias_map(data_dir: str | Path) -> dict[str, str]:
    """``uin → alias``（点名率口径用）。"""
    try:
        p = Path(data_dir) / "member_relations.json"
        ms = json.loads(p.read_text(encoding="utf-8")).get("members") or []
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}
    out: dict[str, str] = {}
    for m in ms:
        if not isinstance(m, dict):
            continue
        a = str(m.get("alias") or "")
        if not a:
            continue
        if m.get("uin"):
            out[str(m["uin"])] = a
        for n in (m.get("other_names") or []):
            if len(str(n)) >= 2:
                out[str(n)] = a
    return out


def compute_stats(rows: list[dict], *, alias_map: dict | None = None) -> dict:
    """按上表口径算统计。**绝不抛**（观测工具不该拖垮分析）。"""
    st: dict = {"inbound": len(rows), "hard_pass": 0, "gate_pass": 0,
                "generated": 0, "gen_attempted": 0, "gen_failed": 0,
                "parse_failed": 0, "triggers": {}}
    cached_sum = other_sum = 0
    per_call: list[float] = []
    gen_with_name = 0
    gen_total = 0
    names = set(alias_map.values()) if alias_map else set()

    for r in rows:
        if not isinstance(r, dict):
            continue
        hg = r.get("hard_gate") or {}
        if isinstance(hg, dict) and hg.get("action") == "reply":
            st["hard_pass"] += 1
            tr = str(hg.get("trigger") or "unknown")
            st["triggers"][tr] = st["triggers"].get(tr, 0) + 1
        g = r.get("gate") or {}
        if isinstance(g, dict) and g.get("reply") is True:
            st["gate_pass"] += 1
        if isinstance(g, dict) and "parse failed" in str(g.get("reason") or ""):
            st["parse_failed"] += 1
        text = str(r.get("final_text") or "")
        # 🔴 B-028：生成尝试 / 失败必须与"没走到生成"分开
        #   （旧口径只看 raw_generation → 空生成完全无痕，实测少算 3/86）
        if r.get("generation_attempted") or r.get("raw_generation") or text:
            st["gen_attempted"] += 1
        if r.get("llm_error"):
            st["gen_failed"] += 1
        if r.get("raw_generation") or text:
            st["generated"] += 1
            if names:
                gen_total += 1
                if any(n and n in text for n in names):
                    gen_with_name += 1
        u = r.get("usage") or {}
        if isinstance(u, dict):
            c, o = u.get("input_cached"), u.get("input_other")
            if isinstance(c, int) and isinstance(o, int):
                cached_sum += c
                other_sum += o
                tot = c + o
                if tot > 0:
                    per_call.append(c / tot)

    st["gate_reject_rate"] = 1.0 - _rate(st["gate_pass"], st["hard_pass"]) \
        if st["hard_pass"] else 0.0
    # 🔴 真实拒绝要**剔除 parse failed** —— 后者是 Gate 没生效，不是判断拒绝
    real_reject = max(0, st["hard_pass"] - st["gate_pass"] - st["parse_failed"])
    st["gate_reject_real"] = real_reject
    st["parse_failed_rate"] = _rate(st["parse_failed"], st["hard_pass"])
    if names:
        st["gen_with_name"] = gen_with_name
        st["name_mention_rate"] = _rate(gen_with_name, gen_total)
    if cached_sum + other_sum > 0:
        st["cache_hit_total"] = cached_sum / (cached_sum + other_sum)
        st["cache_cached_tokens"] = cached_sum
        st["cache_full_price_tokens"] = other_sum
        if per_call:
            st["cache_hit_p50"] = statistics.median(per_call)
    return st


def _fmt(st: dict) -> str:
    L = [
        "=== 漏斗 ===",
        f"  入站        {st['inbound']:>7}",
        f"  硬闸放行    {st['hard_pass']:>7}  ({_rate(st['hard_pass'], st['inbound']):.1%} of 入站)",
        f"  过 Gate     {st['gate_pass']:>7}  ({_rate(st['gate_pass'], st['hard_pass']):.1%} of 放行)",
        f"  生成尝试    {st['gen_attempted']:>7}",
        f"  生成未完成  {st['gen_failed']:>7}  (空生成/异常/未调用 LLM；后两者不算尝试)",
        f"  生成成功    {st['generated']:>7}",
    ]
    if st["hard_pass"]:
        L += [
            "",
            "=== Gate 质量（关键：解析失败 ≠ 判断拒绝）===",
            f"  拒绝率（合计）  {st['gate_reject_rate']:.1%}",
            f"  ├ 解析失败      {st['parse_failed']:>6}  ({st['parse_failed_rate']:.1%} of 放行)"
            + ("   ← Gate 未生效" if st["parse_failed"] else ""),
            f"  └ 真实拒绝      {st['gate_reject_real']:>6}",
        ]
    if st["triggers"]:
        L += ["", "=== 硬闸 trigger 分布 ==="]
        for k, v in sorted(st["triggers"].items(), key=lambda x: -x[1]):
            L.append(f"  {k:<14} {v:>6}")
    if "cache_hit_total" in st:
        L += [
            "",
            "=== 缓存（总量看成本，p50 看体验）===",
            f"  加权命中率  {st['cache_hit_total']:.1%}"
            f"   （cached {st['cache_cached_tokens']:,} / 全价 {st['cache_full_price_tokens']:,}）",
        ]
        if "cache_hit_p50" in st:
            L.append(f"  p50 命中率  {st['cache_hit_p50']:.1%}")
    if "name_mention_rate" in st:
        L += ["", "=== 输出 ===",
              f"  点名率      {st['name_mention_rate']:.1%}"
              f"  ({st['gen_with_name']}/{st['generated']})"]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="trace 统计（口径固化）")
    ap.add_argument("--data-dir", required=True, help="插件数据目录")
    ap.add_argument("--group", default="", help="群号（留空则用日志目录里最大的）")
    ap.add_argument("--since", default="", help="只算 ISO 日期 >= 此值（如 2026-09-15）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    d = Path(args.data_dir).expanduser().resolve()
    gid = args.group
    if not gid:
        cands = [p.name for p in (d / "logs").glob("*") if p.is_dir()] if (d / "logs").is_dir() else []
        if not cands:
            print(f"{d}/logs 下没有群目录", file=sys.stderr)
            return 1
        gid = sorted(cands)[-1]
    rows = load_jsonl_with_rotations(d / "logs" / gid / "trace_log.jsonl")
    if args.since:
        rows = [r for r in rows if str(r.get("ts") or "") >= args.since]
    st = compute_stats(rows, alias_map=load_alias_map(d))
    st["group_id"] = gid
    st["since"] = args.since or "(all)"
    if args.json:
        print(json.dumps(st, ensure_ascii=False, indent=2))
    else:
        print(f"群 {gid} | since {st['since']} | {len(rows)} 条 trace\n")
        print(_fmt(st))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
