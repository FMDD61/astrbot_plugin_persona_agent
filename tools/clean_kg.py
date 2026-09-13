#!/usr/bin/env python3
"""clean_kg — 历史 KG 垃圾清洗（B-014，2026-09-13）。

## 背景：与 RAG 语料清洗同源的问题

`my_conversation_pairs.jsonl` 经历过第二轮清洗（`tools/clean_pairs.py`：
7075 → 4704 对），因为"从每条消息无差别抽取"必然产出大量无信息量条目。
KG 入库此前**没有等价的一道**，实测后果：

| 指标 | 清洗前 |
|---|---|
| distinct topic 实体 | 3194 |
| 实体行 | 12541 |
| `配图` / `识别` / `无法` | 1511 / 1289 / 1210（占实体行 **31.9%**） |
| 只出现 1 次的 topic | 2119 个（66.3%） |
| `talks_about` 边 | 12489，其中污染 **4001（32.0%）** |

代码侧两道门已加（`services/kg_stopwords.py` + `MemoryStore.ingest`），
本工具负责**清历史**。

## 三类清理目标

1. **结构词 topic**：`配图`/`识别`/`无法`/`图片`/`表情` 等 —— 我们自己识图占位符
   被 jieba 切出来的，是系统产物污染长期记忆。**边 + 实体一起删**。
2. **无信息量通用词 topic**：`什么`/`怎么`/`今日`/`感觉` 等（停用词表）。
3. **一次性 topic**：只出现过 1 次的 topic 实体及其边 —— 对应新增的
   "只出现一次不入图"门槛，历史数据要拉齐。**默认不删实体行，只删边**
   （实体行本身不参与图结构，留着无害；`--prune-entities` 才连实体一起删）。

## 安全设计

- **默认 dry-run**：只报告不写。必须 `--apply` 才动手。
- `--apply` 前自动备份 `<db>.bak.<YYYYmmdd_HHMMSS>`（含 `-wal`/`-shm`）。
- 全程单事务；任一步失败整体回滚。
- 不碰 `entities.type='member'` 与 `edges.type='mentions'`（@ 关系是真实信号）。
- `--keep-threshold N` 可调（默认 2，与 `memory.topic_min_occurrences` 对齐）。

用法：
  python3 -m tools.clean_kg --data-dir <plugin_data_dir>            # 只报告
  python3 -m tools.clean_kg --data-dir <dir> --apply                # 执行
  python3 -m tools.clean_kg --db <path> --apply --keep-threshold 3
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from services import kg_stopwords  # noqa: E402


def _backup(db: str) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    bak = f"{db}.bak.{stamp}"
    shutil.copy2(db, bak)
    for suffix in ("-wal", "-shm"):
        src = db + suffix
        if os.path.exists(src):
            shutil.copy2(src, bak + suffix)
    return bak


def collect(conn: sqlite3.Connection, keep_threshold: int) -> dict:
    """统计三类清理目标（纯查询，不改库）。"""
    cur = conn.cursor()
    # ① 结构词 topic
    structural = sorted(kg_stopwords.SYSTEM_STRUCTURAL)
    qs = ",".join("?" * len(structural))
    cur.execute(
        f"SELECT alias, COUNT(*) FROM entities WHERE type='topic' AND alias IN ({qs}) "
        f"GROUP BY alias ORDER BY COUNT(*) DESC",
        structural,
    )
    structural_rows = cur.fetchall()
    # ② 通用词 topic
    generic = sorted(kg_stopwords.GENERIC_WORDS)
    qg = ",".join("?" * len(generic))
    cur.execute(
        f"SELECT alias, COUNT(*) FROM entities WHERE type='topic' AND alias IN ({qg}) "
        f"GROUP BY alias ORDER BY COUNT(*) DESC",
        generic,
    )
    generic_rows = cur.fetchall()
    # ③ 一次性 topic（出现次数 < keep_threshold）
    cur.execute(
        "SELECT alias, COUNT(*) c FROM entities WHERE type='topic' "
        "GROUP BY alias HAVING c < ? ORDER BY c DESC, alias",
        (keep_threshold,),
    )
    rare_rows = cur.fetchall()

    def edge_count(aliases: list[str], etype: str = "talks_about") -> int:
        if not aliases:
            return 0
        total = 0
        # 分批，避免 SQLite 变量上限
        for i in range(0, len(aliases), 400):
            chunk = aliases[i : i + 400]
            q = ",".join("?" * len(chunk))
            total += cur.execute(
                f"SELECT COUNT(*) FROM edges WHERE type=? AND to_alias IN ({q})",
                [etype, *chunk],
            ).fetchone()[0]
        return total

    structural_aliases = [r[0] for r in structural_rows]
    generic_aliases = [r[0] for r in generic_rows]
    rare_aliases = [r[0] for r in rare_rows]
    # 去重后的"要删边的 topic 全集"
    all_bad = list(dict.fromkeys(structural_aliases + generic_aliases + rare_aliases))

    return {
        "structural_topics": structural_rows,
        "generic_topics": generic_rows,
        "rare_topics": rare_rows,
        "structural_edges": edge_count(structural_aliases),
        "generic_edges": edge_count(generic_aliases),
        "rare_edges": edge_count(rare_aliases),
        "all_bad_topics": all_bad,
        "all_bad_edges": edge_count(all_bad),
        "entities_total": cur.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
        "edges_total": cur.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
        "topics_total": cur.execute(
            "SELECT COUNT(DISTINCT alias) FROM entities WHERE type='topic'"
        ).fetchone()[0],
        "mentions_total": cur.execute(
            "SELECT COUNT(*) FROM edges WHERE type='mentions'"
        ).fetchone()[0],
    }


def apply_cleanup(
    conn: sqlite3.Connection, plan: dict, prune_entities: bool, keep_threshold: int
) -> dict:
    """执行清理（单事务）。返回受影响行数。"""
    cur = conn.cursor()
    bad = plan["all_bad_topics"]
    removed_edges = 0
    removed_entities = 0
    try:
        cur.execute("BEGIN")
        for i in range(0, len(bad), 400):
            chunk = bad[i : i + 400]
            q = ",".join("?" * len(chunk))
            # 只删 talks_about —— mentions（@ 关系）不碰
            cur.execute(
                f"DELETE FROM edges WHERE type='talks_about' AND to_alias IN ({q})", chunk
            )
            removed_edges += cur.rowcount
        if prune_entities:
            for i in range(0, len(bad), 400):
                chunk = bad[i : i + 400]
                q = ",".join("?" * len(chunk))
                cur.execute(
                    f"DELETE FROM entities WHERE type='topic' AND alias IN ({q})", chunk
                )
                removed_entities += cur.rowcount
        # 清理孤立实体：不再被任何边引用的 topic（可选，仅在 prune 时）
        if prune_entities:
            cur.execute(
                "DELETE FROM entities WHERE type='topic' AND alias NOT IN "
                "(SELECT to_alias FROM edges WHERE type='talks_about')"
            )
            removed_entities += cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"removed_edges": removed_edges, "removed_entities": removed_entities}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="KG 历史垃圾清洗（B-014）")
    ap.add_argument("--db", help="memory_store.db 路径")
    ap.add_argument("--data-dir", help="插件数据目录（自动拼 memory_store.db）")
    ap.add_argument("--keep-threshold", type=int, default=2,
                    help="保留阈值：出现次数 < N 的 topic 视为噪声（默认 2）")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    ap.add_argument("--prune-entities", action="store_true",
                    help="连实体行一起删（默认只删边，保留实体行无害）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)

    db = args.db or (os.path.join(args.data_dir, "memory_store.db") if args.data_dir else "")
    if not db or not os.path.exists(db):
        print(f"error: db not found: {db!r}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(db)
    try:
        plan = collect(conn, max(1, args.keep_threshold))
        if args.json:
            print(json.dumps({
                "db": db, "mode": "apply" if args.apply else "dry-run",
                "keep_threshold": args.keep_threshold,
                "entities_total": plan["entities_total"],
                "edges_total": plan["edges_total"],
                "topics_total": plan["topics_total"],
                "structural_topics": plan["structural_topics"][:15],
                "generic_topics": plan["generic_topics"][:15],
                "rare_topics_count": len(plan["rare_topics"]),
                "structural_edges": plan["structural_edges"],
                "generic_edges": plan["generic_edges"],
                "rare_edges": plan["rare_edges"],
                "bad_edges_total": plan["all_bad_edges"],
                "mentions_total": plan["mentions_total"],
            }, ensure_ascii=False, indent=1))
        else:
            print(f"db: {db}")
            print(f"阈值: topic 出现 < {args.keep_threshold} 次视为噪声")
            print(f"当前: entities={plan['entities_total']} edges={plan['edges_total']} "
                  f"distinct_topics={plan['topics_total']} mentions={plan['mentions_total']}")
            print()
            print(f"① 结构词 topic（系统占位符产物）: {len(plan['structural_topics'])} 个，"
                  f"关联边 {plan['structural_edges']}")
            for a, c in plan["structural_topics"][:10]:
                print(f"     {c:5d}  {a}")
            print()
            print(f"② 通用词 topic: {len(plan['generic_topics'])} 个，关联边 {plan['generic_edges']}")
            for a, c in plan["generic_topics"][:10]:
                print(f"     {c:5d}  {a}")
            print()
            print(f"③ 一次性 topic（<{args.keep_threshold} 次）: {len(plan['rare_topics'])} 个，"
                  f"关联边 {plan['rare_edges']}")
            print()
            print(f"合计待删：topic {len(plan['all_bad_topics'])} 个 / "
                  f"talks_about 边 {plan['all_bad_edges']} "
                  f"({plan['all_bad_edges']/max(1,plan['edges_total']):.1%} of edges)")

        if not args.apply:
            # --json 模式 stdout 必须只有 JSON（否则调用方解析会炸）
            if not args.json:
                print("\n[dry-run] 未改动任何数据。加 --apply 执行（会自动备份）。")
            return 0

        bak = _backup(db)
        print(f"\n备份: {bak}")
        res = apply_cleanup(conn, plan, args.prune_entities, args.keep_threshold)
        after_entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        after_edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        print(f"已清理: 删除边 {res['removed_edges']} 条 / 实体 {res['removed_entities']} 行")
        print(f"清理后: entities={after_entities} edges={after_edges}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
