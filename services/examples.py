"""examples — G14 静态示例块注入（C1/C2 重做）。

纯 stdlib；**不做 mtime 缓存**（见 loader docstring）。
块作为**一条恒定 system 消息**注入（稳定缓存前缀，见 main 的接线）。

## 数据来源（2026-09-23 脱敏后）

示例语料**源自真实群聊语句**，所以整批移出仓库（仓库是 public）。
仓库里只有 `examples_default.ENTRIES`（**恒空**，见该模块 docstring）。

```
<data_dir>/example_dialogs.json   有可解析条目 → 用它（部署形态；scp 迁移过来）
<data_dir>/examples.json          同一个 loader 的**别名**（data_out 里的名字）
都没有 / 坏了 / 空                 → 空示例块，source="none"
```

**任何情况下都不会回退到旧示例句**：旧句在这份代码里一个字都不存在。

`ExamplesState` 记录本次用的是哪一路 + 哪个文件 + 失败原因，供启动自检打出来
（**降级必须可见**：`source == "none"` 时 main 会打 WARNING、pipeline 会写 trace）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import examples_default

#: 条数上限默认值（C1：不锁死在 12 —— 用户「13 条不一定够，甚至可能会增加」）
MAX_ENTRIES = 20

#: 头部（兼容旧引用；真身在 examples_default，是**框架文本**）
HEADER = examples_default.HEADER

#: 数据目录里示例语料的**可接受文件名**（按优先序）。
#: `example_dialogs.json` = 线上历史名；`examples.json` = `data_out/` 的交付名。
EXAMPLES_FILES: tuple[str, ...] = ("example_dialogs.json", "examples.json")


@dataclass
class ExamplesState:
    mtime: float = 0.0
    block: str = ""
    #: "file"（数据目录文件）/ "bundled"（内置，脱敏后恒不可达）/ "none"（没语料）
    source: str = "none"
    entries: int = 0
    #: 实际生效的文件（降级时为空串）—— 日志里点名用
    path: str = ""
    #: "文件在但用不了"的原因（与"压根没文件"可区分，N6 同族纪律）
    error: str = ""


def candidate_paths(path) -> list[Path]:
    """给定主路径 → 依次尝试的候选文件（含同目录别名）。"""
    path = Path(path)
    out: list[Path] = [path]
    for name in EXAMPLES_FILES:
        p = path.parent / name
        if p not in out:
            out.append(p)
    return out


def parse_payload(data) -> tuple[str, list[dict]]:
    """接受两种形态：

    * 裸数组 —— 历史 `example_dialogs.json`（线上既有文件就是这个形态）；
    * `{"header": ..., "entries": [...]}` —— `data_out/examples.json`（自带 HEADER，
      便于"一个文件即完整语料"地 scp 与备份）。

    返回 ``(文件自带的 header 或空串, 条目列表)``。
    """
    if isinstance(data, dict):
        entries = data.get("entries")
        return str(data.get("header") or "").strip(), (
            entries if isinstance(entries, list) else [])
    if isinstance(data, list):
        return "", data
    return "", []


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

    ``block == ""`` 出现在：`max_entries=0`，或**数据目录没有任何可用语料**
    （2026-09-23 之后这是**可达且必须可见**的形态 —— 忘了 scp 就长这样）。

    ⚠️ **不做 mtime 缓存**（独立核验 N-1）：文件 mtime 走内核**粗时钟**，
    实测同一刻度内的改写 `st_mtime_ns` **完全相同** → 任何"mtime 指纹"都会
    返回**旧块**（新示例不可见，且不报错）。这与段文件那边（N7）是同一根因，
    两条约定必须一致 —— 这个文件只有二十来条，每次现读比"缓存 + 偶发看不见"划算。
    ``prev`` 参数保留只为兼容既有调用点（不再用于新鲜度判断）。
    """
    path = Path(path)
    lines: list[str] = []
    source = "none"
    used = ""
    mt = 0
    err = ""
    file_header_used = ""
    for cand in candidate_paths(path):
        try:
            if not cand.is_file():
                continue
            st = cand.stat()
            data = json.loads(cand.read_text("utf-8"))
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
            err = f"{cand.name}: {type(e).__name__}: {e}"
            continue
        file_header, entries = parse_payload(data)
        rendered = _render(entries, max_entries)
        if rendered:
            lines, source, used, mt, err = rendered, "file", str(cand), st.st_mtime_ns, ""
            file_header_used = file_header
            break
        err = err or f"{cand.name}: 没有可用条目（每条需 ≥2 条带内容的消息）"
    if lines:
        # 文件自带 HEADER 就用它（data_out/examples.json），否则用框架头
        header = file_header_used or HEADER
    else:
        # 仓库里没有内置语料（ENTRIES 恒空）→ 这段只在有人把语料塞回模块时才命中
        lines = _render(examples_default.ENTRIES, max_entries)
        header = examples_default.HEADER
        source = "bundled" if lines else "none"
    block = (header + "\n" + "\n".join(lines)) if lines else ""
    return block, ExamplesState(
        mtime=mt, block=block, source=source, entries=len(lines), path=used, error=err)
