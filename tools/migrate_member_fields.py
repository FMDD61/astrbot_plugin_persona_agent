#!/usr/bin/env python3
"""把 `notes=="bot"` 标记迁到 `kind` 字段，把 `notes` 让给"群友描述"（S12）。

## 为什么需要

`notes` 字段目前被当作**标记**使用（值 `"bot"`），全场只有 2 人用它，且唯一用途是
把这两个 **bot 账号**排除出关系图谱：

    if m.get("notes") == "bot": continue

但我们要把 `notes` 让给"对每个群友的简短描述"（LLM 生成 + 人工可改），
所以标记必须搬到新字段 `kind`。

## ⚠️ 迁移的安全要点

**成员表里有 2 个 bot 账号混在真人中间**（成员子、成员丑 —— 它们是 bot，不是群友）。
若迁移时漏掉兼容读取，它们会被**当成真人暴露给 LLM**（关系图谱、熟悉度、描述全都会带上它们）。

所以本工具：
  1. 写 `kind="bot"`，同时**保留** `notes="bot"`（双写，让新旧代码都能识别）
  2. **不做** `notes` 清空 —— 清空要等所有读取点都切到 `kind` 之后再单独做
  3. dry-run 默认；`--apply` 才写；写前自动备份

用法：
    python3 -m tools.migrate_member_fields --data-dir <dir>              # 看看要改什么
    python3 -m tools.migrate_member_fields --data-dir <dir> --apply      # 实际写
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

REL_FILE = "member_relations.json"


def plan(members: list[dict]) -> list[dict]:
    """算出需要迁移的条目（纯函数，可单测）。"""
    out: list[dict] = []
    for m in members:
        notes = str(m.get("notes") or "").strip()
        if notes == "bot" and str(m.get("kind") or "") != "bot":
            out.append({"uin": str(m.get("uin") or ""),
                        "alias": str(m.get("alias") or ""),
                        "action": "notes:bot → kind:bot（notes 保留双写）"})
    return out


def apply_migration(members: list[dict]) -> tuple[list[dict], int]:
    """原地写入 `kind`；返回 (members, 改动数)。**不动 `notes`**（双写期）。"""
    n = 0
    for m in members:
        if str(m.get("notes") or "").strip() == "bot" \
                and str(m.get("kind") or "") != "bot":
            m["kind"] = "bot"
            n += 1
    return members, n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="notes→kind 字段迁移（S12）")
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--apply", action="store_true", help="实际写入（默认 dry-run）")
    args = ap.parse_args(argv)

    p = Path(args.data_dir).expanduser().resolve() / REL_FILE
    if not p.exists():
        print(f"找不到 {p}", file=sys.stderr)
        return 1
    raw = json.loads(p.read_text(encoding="utf-8"))
    members = raw.get("members") or []
    todo = plan(members)
    print(f"成员 {len(members)} 人 | 待迁移 {len(todo)} 条")
    for t in todo:
        print(f"  {t['uin']:>12}  {t['alias'][:16]:18s} {t['action']}")
    if not todo:
        print("无需迁移")
        return 0
    if not args.apply:
        print("\n[dry-run] 未写入。加 --apply 执行。")
        return 0

    bak = p.with_suffix(p.suffix + f".bak.{time.strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(p, bak)
    members, n = apply_migration(members)
    raw["members"] = members
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    print(f"\n✅ 已迁移 {n} 条 | 备份 {bak.name}")
    print("⚠️ `notes` 保留双写 —— 等所有读取点切到 `kind` 后再单独清空。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
