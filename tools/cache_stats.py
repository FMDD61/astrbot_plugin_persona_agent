#!/usr/bin/env python3
"""cache_stats — 把前缀缓存探针聚合成**长期留存、LLM 可读**的时间序列。

## 为什么需要

`logs/<gid>/llm_cache_probe.jsonl` 是**逐次调用的原始明细**（每次一行，含完整
usage 与上下文长度）。它有两个问题：

1. **无轮转**：同目录的 `trace_log.jsonl` 已经 43 MB、`decision_log.jsonl` 2.7 MB，
   同样只增不减。靠它们做长期趋势分析迟早不可读。
2. **粒度太细**：单次调用的 `input_cached` 波动很大（首轮必然冷启动），
   直接看明细会被噪声淹没，看不出"这周缓存是否变差"。

所以本工具把明细**压缩成两级产物**：

- `cache_stats.jsonl` —— **每次调用一行**的紧凑记录（丢掉 raw_usage 等冗长字段，
  只留可分析的量），供细查
- `cache_daily.jsonl` —— **每天一行**的汇总（按 UTC 日），供趋势与 LLM 阅读

两者都是 JSONL + 原子追加，**只增不改**（人工可读、可被任何工具消费）。

## 关键指标

- `hit_rate` = `input_cached / prompt_tokens` —— 缓存命中率（**核心**）
- `input_other` —— 走全价的那部分。它才是真实成本，`hit_rate` 高不代表 `other` 小
  （实测：hit 99.6% 但 `other=318` token 仍要全价）
- `sys_prompt_hash16` —— system prompt 的稳定性。**它一变，整段前缀缓存全废**，
  所以日汇总里要看 `distinct_sys_hashes`：>1 说明当天提示词被改过
- `cold_start` —— `input_cached == 0` 的调用（新会话首轮/轮转后首轮）

## 用法

    # 增量聚合（默认：只处理上次之后的新行，水位记在 cache_stats.state.json）
    python3 -m tools.cache_stats --data-dir <plugin_data_dir> --group 100000001

    # 全量重算（换 group / 修 bug 后）
    python3 -m tools.cache_stats --data-dir ... --group ... --rebuild

    # 只打印日汇总（不写文件）
    python3 -m tools.cache_stats --data-dir ... --group ... --report
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

STATS_FILE = "cache_stats.jsonl"
DAILY_FILE = "cache_daily.jsonl"
STATE_FILE = "cache_stats.state.json"


# ---------------------------------------------------------------- 读取

def read_probe(path: Path) -> list[dict]:
    """读探针明细（容忍坏行）。"""
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(r, dict):
                    out.append(r)
    except OSError:
        pass
    return out


def read_jsonl(path: Path) -> list[dict]:
    return read_probe(path)


def append_jsonl(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- 压缩

def compact(row: dict) -> dict:
    """把一条探针明细压成紧凑记录（只留可分析的量）。"""
    u = row.get("usage") or {}
    cached = int(u.get("input_cached") or 0)
    other = int(u.get("input_other") or 0)
    total = cached + other
    return {
        "ts": round(float(row.get("ts") or 0.0), 1),
        "day": time.strftime("%Y-%m-%d", time.gmtime(float(row.get("ts") or 0.0))),
        "sess": int(row.get("session_size_before") or 0),
        "ctx_chars": int(row.get("contexts_chars") or 0),
        "sys_len": int(row.get("sys_prompt_len") or 0),
        "sys_hash": str(row.get("sys_prompt_hash16") or ""),
        "hour": int(row.get("local_hour") or 0),
        "cached": cached,
        "other": other,
        "prompt": total,
        "out": int(u.get("output") or 0),
        # hit_rate 基于 prompt_tokens 口径；total==0 时记 0（视作冷启动）
        "hit": round(cached / total, 4) if total else 0.0,
        "cold": 1 if cached == 0 else 0,
    }


def rollup_day(day: str, rows: list[dict]) -> dict:
    """把一天的行汇总成一行（趋势分析用）。"""
    n = len(rows)
    if not n:
        return {}
    cached = [r["cached"] for r in rows]
    other = [r["other"] for r in rows]
    prompt = [r["prompt"] for r in rows]
    hits = [r["hit"] for r in rows]
    ctx = [r["ctx_chars"] for r in rows]
    sess = [r["sess"] for r in rows]

    def avg(xs: list[float]) -> float:
        return round(sum(xs) / len(xs), 2) if xs else 0.0

    def pct(xs: list[float], p: float) -> float:
        s = sorted(xs)
        return round(s[min(len(s) - 1, int(len(s) * p))], 4) if s else 0.0

    return {
        "day": day,
        "calls": n,
        "cold_start": sum(r["cold"] for r in rows),
        # 两种口径都给：总量比（含冷启动拖累）与均值（单次体验）
        "hit_rate_total": round(sum(cached) / sum(prompt), 4) if sum(prompt) else 0.0,
        "hit_rate_avg": round(sum(hits) / n, 4),
        "hit_rate_p50": pct(hits, 0.5),
        "hit_rate_p10": pct(hits, 0.1),
        "other_total": sum(other),
        "cached_total": sum(cached),
        "prompt_total": sum(prompt),
        "other_avg": avg(other),
        "ctx_chars_avg": avg(ctx),
        "ctx_chars_max": max(ctx) if ctx else 0,
        "sess_avg": avg(sess),
        "sess_max": max(sess) if sess else 0,
        # 提示词稳定性：>1 表示当天改过 system prompt（一改整段缓存全废）
        "distinct_sys_hashes": len({r["sys_hash"] for r in rows if r["sys_hash"]}),
    }


def rebuild_daily(rows: list[dict]) -> list[dict]:
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r)
    return [rollup_day(d, by_day[d]) for d in sorted(by_day)]


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="前缀缓存命中聚合（长期留存 + LLM 可读）")
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--group", required=True)
    ap.add_argument("--rebuild", action="store_true", help="全量重算（忽略水位）")
    ap.add_argument("--report", action="store_true", help="只打印日汇总，不写文件")
    ap.add_argument("--limit", type=int, default=14, help="报告显示最近 N 天")
    args = ap.parse_args(argv)

    gdir = Path(args.data_dir).expanduser().resolve() / "logs" / str(args.group)
    probe = gdir / "llm_cache_probe.jsonl"
    stats_path = gdir / STATS_FILE
    daily_path = gdir / DAILY_FILE
    state_path = gdir / STATE_FILE

    if not probe.exists():
        print(f"探针文件不存在: {probe}", file=sys.stderr)
        return 1

    raw = read_probe(probe)
    print(f"探针明细 {len(raw)} 行")

    if args.report:
        existing = read_jsonl(daily_path)
        if not existing:
            print("尚无日汇总（先不带 --report 跑一次）", file=sys.stderr)
            return 1
        _print_report(existing[-args.limit:])
        return 0

    if args.rebuild:
        rows = [compact(r) for r in raw]
        rows = [r for r in rows if r["ts"] > 0]
        # 重算：清空两个产物再写（**仅此命令会清空**）
        for p in (stats_path, daily_path):
            if p.exists():
                p.unlink()
        append_jsonl(stats_path, rows)
        daily = rebuild_daily(rows)
        append_jsonl(daily_path, daily)
        write_json_atomic(state_path, {"last_ts": rows[-1]["ts"] if rows else 0,
                                       "count": len(rows), "rebuilt_at": time.time()})
        print(f"全量重算：{len(rows)} 次调用 → {len(daily)} 天")
        _print_report(daily[-args.limit:])
        return 0

    # 增量：按 last_ts 水位
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
    last = float(state.get("last_ts") or 0.0)
    fresh = [r for r in raw if float(r.get("ts") or 0) > last]
    rows = [r for r in (compact(x) for x in fresh) if r["ts"] > 0]
    if not rows:
        print("无新数据")
        existing = read_jsonl(daily_path)
        if existing:
            _print_report(existing[-args.limit:])
        return 0

    append_jsonl(stats_path, rows)
    # 受影响的日期整体重算（当天可能跨多次增量）
    all_rows = read_jsonl(stats_path)
    touched = {r["day"] for r in rows}
    daily_all = read_jsonl(daily_path)
    keep = [d for d in daily_all if d.get("day") not in touched]
    new_daily = rebuild_daily([r for r in all_rows if r["day"] in touched])
    merged = sorted(keep + new_daily, key=lambda d: d.get("day") or "")
    daily_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = daily_path.with_suffix(daily_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for d in merged:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    os.replace(tmp, daily_path)
    write_json_atomic(state_path, {"last_ts": rows[-1]["ts"], "count":
                                   int(state.get("count") or 0) + len(rows)})
    print(f"增量：+{len(rows)} 次调用，重算 {len(touched)} 天")
    _print_report(merged[-args.limit:])
    return 0


def _print_report(daily: list[dict]) -> None:
    if not daily:
        return
    print()
    print(f"{'日期':<12}{'调用':>5}{'冷启动':>7}{'命中率(总量)':>13}{'命中率(p50)':>12}"
          f"{'全价token':>11}{'上下文均值':>11}{'提示词哈希':>11}")
    for d in daily:
        print(f"{d['day']:<12}{d['calls']:>5}{d['cold_start']:>7}"
              f"{d['hit_rate_total']:>12.1%}{d['hit_rate_p50']:>12.1%}"
              f"{d['other_total']:>11}{d['ctx_chars_avg']:>11.0f}"
              f"{d['distinct_sys_hashes']:>11}")
    print()
    print("读法：")
    print("  命中率(总量) 含冷启动拖累；命中率(p50) 是单次中位 —— 两者差距大 = 冷启动多")
    print("  全价token = input_other 合计，**它才是真实成本**（命中率高不代表它小）")
    print("  提示词哈希 >1 = 当天改过 system prompt（一改整段前缀缓存全废，需警惕）")


if __name__ == "__main__":
    raise SystemExit(main())
