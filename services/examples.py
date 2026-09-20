"""examples — G14 静态示例块注入（C1/C2 重做）。

纯 stdlib；mtime 热重载（纳秒），A/B 切换就是改/换文件（无需重启）。
块作为**一条恒定 system 消息**注入（稳定缓存前缀，见 main 的接线）。

## 数据来源（用户 2026-09-20 定）

```
<data_dir>/example_dialogs.json   有可解析条目 → 用它（部署时"替换文件"这条路）
                                没有/坏了/空 → 回落到 services/examples_default.py
```

**任何情况下都不会回退到旧示例句**：旧句在这份代码里一个字都不存在 ——
回落目标就是新 20 条（由 `tools/gen_examples_default.py` 从草案生成）。
`ExamplesState.source` 记录本次用的是哪一路，供启动自检打出来（降级必须可见）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import examples_default

#: 条数上限默认值（C1：不锁死在 12 —— 用户「13 条不一定够，甚至可能会增加」）
MAX_ENTRIES = 20

#: 头部（兼容旧引用；真身在 examples_default，由草案 §A 生成）
HEADER = examples_default.HEADER


@dataclass
class ExamplesState:
    mtime: float = 0.0
    block: str = ""
    #: "file"（数据目录文件）/ "bundled"（内置默认）/ "none"（连默认都空）
    source: str = "none"
    entries: int = 0


def _render(entries: list[dict], max_entries: int) -> list[str]:
    """把条目渲染成逐行文本（`[话题] 角色: 内容 角色: 内容`）。"""
    lines: list[str] = []
    for ex in (entries or [])[:max_entries]:
        if not isinstance(ex, dict):
            continue
        parts = []
        for m in (ex.get("messages") or []):
            if not isinstance(m, dict):
                continue
            content = (m.get("content") or "").strip()
            role = (m.get("role") or "").strip()
            if content:
                parts.append(f"{role}: {content}".strip())
        if len(parts) >= 2:
            topic = (ex.get("topic") or "").strip()
            lines.append((f"[{topic}] " if topic else "") + " ".join(parts))
    return lines


def load_examples_block(
    path: Path,
    max_entries: int = MAX_ENTRIES,
    prev: Optional[ExamplesState] = None,
) -> tuple[str, ExamplesState]:
    """返回 ``(block, state)``。

    ``block == ""`` 只在 ``max_entries=0`` 或**连内置默认都没有**时出现
    （正常部署永远不会）。

    ⚠️ **不做 mtime 缓存**（独立核验 N-1）：文件 mtime 走内核**粗时钟**，
    实测同一刻度内的改写 `st_mtime_ns` **完全相同** → 任何"mtime 指纹"都会
    返回**旧块**（新示例不可见，且不报错）。这与段文件那边（N7）是同一根因，
    两条约定必须一致 —— 这个文件只有二十来条，每次现读比"缓存 + 偶发看不见"划算。
    ``prev`` 参数保留只为兼容既有调用点（不再用于新鲜度判断）。
    """
    path = Path(path)
    try:
        mt = path.stat().st_mtime_ns      # 仅作 state 记录/观测，不用于短路
    except OSError:
        mt = 0
    lines: list[str] = []
    source = "bundled"
    if mt > 0:
        try:
            data = json.loads(path.read_text("utf-8"))
            lines = _render(data if isinstance(data, list) else [], max_entries)
            if lines:
                source = "file"
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            lines = []
    if lines:
        header = HEADER
    else:
        # 回落内置默认（新 20 条）。⚠️ 这**不是**降级告警：文件缺失是正常部署形态；
        # 但 source 会如实写进 state，启动日志里看得见用的是哪一路。
        lines = _render(examples_default.ENTRIES, max_entries)
        header = examples_default.HEADER
        source = "bundled" if lines else "none"
    block = (header + "\n" + "\n".join(lines)) if lines else ""
    return block, ExamplesState(mtime=mt, block=block, source=source, entries=len(lines))
