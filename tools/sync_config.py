"""sync_config — merge a live plugin config file with _conf_schema.json defaults.

A2 工具：把 /opt/AstrBot/data/config/astrbot_plugin_persona_agent_config.json
与仓库 _conf_schema.json 对齐 —— **只补缺失键，绝不改动已有值**（红线 #3）。

行为契约：
- 读取支持 UTF-8 BOM（AstrBot WebUI 写出的文件带 BOM），写入保持 utf-8-sig
  以兼容 AstrBot 自身的读者（实测 json.load(utf-8) 会因 BOM 报错）。
- 嵌套 object 键按 schema 的 `items` 结构递归补齐。
- 已有键值类型与 default 不一致时：保留原值并 warning（不自动纠正）。
- 未知键（schema 中已删除的旧键）：保留不动。
- --write 生效前自动备份 .bak.<YYYYmmddHHMMSS>；原子写（.tmp → rename）。
- 默认 check 模式（dry-run）：只报告，不改文件。

用法：
    python -m tools.sync_config --config <config.json> [--schema _conf_schema.json] [--write] [--json]
退出码：0 = 无需变更（或已成功写入）；1 = check 模式发现缺失键；2 = 错误。
"""
from __future__ import annotations

import argparse
import copy
import datetime
import json
import sys
from pathlib import Path
from typing import Any, Optional


def collect_schema_children(node: Any) -> dict:
    """Return the child map for a schema node (handles the {type, items} wrapper)."""
    if isinstance(node, dict):
        items = node.get("items")
        if isinstance(items, dict):
            return items
    return node if isinstance(node, dict) else {}


def merge_defaults(cfg: dict, schema: dict, warnings: list) -> list:
    """Fill missing keys recursively from schema defaults. Returns added paths."""
    added: list[str] = []

    def walk(cfg_node: dict, schema_node: dict, prefix: str) -> None:
        for key, child in collect_schema_children(schema_node).items():
            path = f"{prefix}{key}"
            if not isinstance(child, dict):
                continue
            if "default" in child:
                if key not in cfg_node:
                    cfg_node[key] = copy.deepcopy(child["default"])
                    added.append(path)
                else:
                    default = child["default"]
                    existing = cfg_node[key]
                    if default is not None and not isinstance(default, bool):
                        # int/float are one numeric family (schema defaults may
                        # write 15 while the live file carries 15.0).
                        if isinstance(default, (int, float)):
                            bad = not isinstance(existing, (int, float)) or isinstance(existing, bool)
                        else:
                            bad = type(existing) is not type(default)
                        if bad:
                            warnings.append(
                                f"{path}: value {existing!r} type {type(existing).__name__} "
                                f"!= schema default type {type(default).__name__} (kept as-is)"
                            )
            elif "items" in child:
                sub = cfg_node.get(key)
                if not isinstance(sub, dict):
                    if sub is not None:
                        warnings.append(f"{path}: expected object, got {type(sub).__name__} (kept as-is)")
                        continue
                    sub = {}
                    cfg_node[key] = sub
                    added.append(path + " (object)")
                walk(sub, child, f"{path}.")

    walk(cfg, schema, "")
    return added


def load_json(path: Path) -> dict:
    return json.loads(path.read_text("utf-8-sig"))


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8-sig",
    )
    tmp.rename(path)


def backup(path: Path) -> Optional[Path]:
    stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    bak = path.with_name(f"{path.name}.bak.{stamp}")
    bak.write_bytes(path.read_bytes())
    return bak


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="live plugin config JSON path")
    parser.add_argument(
        "--schema",
        default=str(Path(__file__).resolve().parents[1] / "_conf_schema.json"),
        help="schema JSON path (default: repo _conf_schema.json)",
    )
    parser.add_argument("--write", action="store_true", help="apply changes (with backup); default is check-only")
    parser.add_argument("--json", action="store_true", help="machine-readable summary")
    args = parser.parse_args(argv)

    cfg_path = Path(args.config)
    schema_path = Path(args.schema)
    try:
        cfg = load_json(cfg_path)
    except Exception as e:
        print(f"error: cannot read config {cfg_path}: {e}", file=sys.stderr)
        return 2
    try:
        schema = load_json(schema_path)
    except Exception as e:
        print(f"error: cannot read schema {schema_path}: {e}", file=sys.stderr)
        return 2

    warnings: list = []
    added = merge_defaults(cfg, schema, warnings)
    changed = bool(added)

    if args.json:
        print(json.dumps({
            "changed": changed,
            "added": added,
            "warnings": warnings,
            "unknown_preserved": True,
            "mode": "write" if args.write else "check",
        }, ensure_ascii=False, indent=2))
    else:
        for p in added:
            print(f"+ {p}")
        for w_ in warnings:
            print(f"! {w_}")
        print(f"summary: added={len(added)} warnings={len(warnings)} "
              f"mode={'write' if args.write else 'check'} "
              f"status={'changed' if changed else 'in-sync'}")

    if not changed:
        return 0
    if not args.write:
        return 1
    bak = backup(cfg_path)
    write_json(cfg_path, cfg)
    if args.json:
        print(json.dumps({"backup": str(bak) if bak else None}, ensure_ascii=False))
    else:
        print(f"backup: {bak}")
        print("written: " + str(cfg_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
