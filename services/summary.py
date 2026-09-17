"""summary — G13 周/月摘要金字塔（纯 stdlib，可离线单测）。

数据流：`logs/<gid>/daily_diary.jsonl`（日日记，现行路径；根目录旧路径
仅作兼容）→ 周（周一 02:10）/ 月（1 日 02:15）cron 汇总 →
weekly_summary.jsonl / monthly_summary.jsonl；
防失真：每个归档日从 session_<group>_<day>.json 抽样原文
（仅 user 消息，max_sample_messages 条/天，不抽 bot 消息）。

LLM 改写由 main 注入（本模块只做：窗口计算 / 数据收集 / prompt 组装 / 落盘），
推送 bind_dream 私聊由 main 完成。

全部接口以本地日期（date 对象）驱动，便于离线单测。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

DIARY_FILE = "daily_diary.jsonl"
WEEKLY_FILE = "weekly_summary.jsonl"
MONTHLY_FILE = "monthly_summary.jsonl"
YEARLY_FILE = "yearly_summary.jsonl"
_PRELUDE = (
    "请把过去一段时间群里发生的事写成一段简短摘要（第一人称、本人语气），"
    "按时间顺序包含主要话题与群友互动，不要列条、不要编造日日记与原文里没有的事。"
)


def weekly_window(today: date) -> tuple[date, date, str]:
    """Last 7 full days ending yesterday + ISO week label of the end day."""
    end = today - timedelta(days=1)
    start = end - timedelta(days=6)
    iso = end.isocalendar()
    return start, end, f"{iso.year}-W{iso.week:02d}"


def monthly_window(today: date) -> tuple[date, date, str]:
    """Previous calendar month + label YYYY-MM."""
    first_this = today.replace(day=1)
    end = first_this - timedelta(days=1)
    start = end.replace(day=1)
    return start, end, end.strftime("%Y-%m")


def yearly_window(today: date) -> tuple[date, date, str]:
    """Previous complete calendar year + label YYYY。（S12 新增）

    ## 为什么年报读**月报**而不是日记

    用户 2026-09-15 确认："保持扁平结构和年报读月报"。层级选择的关键在于
    **刻度是否可整除**：

      - 日 → 月：可整除（每月一个自然月）→ 月报读日日记，**无错位**
      - 月 → 年：可整除（每年 12 个自然月）→ 年报读月报，**无错位**
      - 日 → 周：**不可整除**（ISO 周与月边界互相切割）

    所以**周报是旁支、不进主链**：它有自己的视角（"近期动态"），
    与月报内容有重叠是正常的。若强行让月报读周报，跨月的那一周会让
    两个月都"不完整"，且该误差**无法在月层级修正**（周不可分）。
    """
    y = today.year - 1
    return date(y, 1, 1), date(y, 12, 31), str(y)


def list_summaries(path: Path, group_id: str, start: date, end: date,
                   prefix: str = "") -> list[dict]:
    """读某层的摘要记录（用于年报读月报）。``prefix`` 按 period 前缀过滤。"""
    out: list[dict] = []
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        if str(rec.get("group_id", "")) != group_id:
            continue
        period = str(rec.get("period") or "")
        if prefix and not period.startswith(prefix):
            continue
        summary = str(rec.get("summary") or "").strip()
        if not summary:
            continue
        out.append({"period": period, "summary": summary})
    out.sort(key=lambda r: r["period"])
    return out


def list_diaries(path: Path, group_id: str, start: date, end: date) -> list[dict]:
    """Diary records in [start, end] day range; tolerant of corrupt lines."""
    wanted = {(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((end - start).days + 1)}
    out: list[dict] = []
    try:
        for line in path.read_text("utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(rec.get("group_id", "")) != group_id:
                continue
            if str(rec.get("day", "")) not in wanted:
                continue
            if (rec.get("summary") or "").strip():
                out.append(rec)
    except OSError:
        return []
    return out


def read_diaries(data_dir: "str | Path", group_id: str, start: date,
                 end: date) -> list[dict]:
    """读窗口内的日日记（**双路径** + 同日去重）。

    🔴 B-024（2026-09-17 生产验证）：日记自 `e566116`（2026-09-08）起写在
    `<data>/logs/<gid>/daily_diary.jsonl`，而本模块此前一直读**数据根目录**的
    同名文件（内容停在 2026-08-29）→ 生产周报 `n_diaries` 恒为 0，
    并连带让 `main._propose_relations` 的 `not diaries` 守卫永久提前返回
    （**S12 关系提升提案从不产出**）。

    口径与 `services/dream.py::gather_diaries` 一致（同一迁移，那边早已双路径）：
    现行路径优先；同一天多条只留**较新**一条（B-018 幂等之前写下的历史重复仍在）。
    """
    base = Path(data_dir)
    # 旧路径先读、现行路径后读 → 同日冲突时现行覆盖旧
    paths = (base / DIARY_FILE, base / "logs" / str(group_id) / DIARY_FILE)
    by_day: dict[str, dict] = {}
    for path in paths:
        for rec in list_diaries(path, group_id, start, end):
            by_day[str(rec.get("day") or "")] = rec   # 同日：后者（较新）胜
    return [by_day[d] for d in sorted(by_day) if d]


def sample_days(data_dir: str, group_id: str, start: date, end: date,
                max_per_day: int = 6) -> list[str]:
    """Sample user messages from archived per-day session files (anti-drift)."""
    d = Path(data_dir)
    safe = group_id.replace("/", "_").replace("\\", "_")
    samples: list[str] = []
    n = (end - start).days
    for i in range(n + 1):
        day = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        path = d / f"session_{safe}_{day}.json"
        try:
            payload = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        msgs = payload.get("messages") or []
        grabbed = 0
        for m in msgs:
            if m.get("role") != "user":
                continue
            content = (m.get("content") or "").strip()
            if not content:
                continue
            name = m.get("name") or ""
            samples.append(f"[{name or '成员'}] {content[:200]}")
            grabbed += 1
            if grabbed >= max_per_day:
                break
    return samples


def build_prompt(kind: str, group_id: str, label: str, diaries: list[dict],
                 samples: list[str], max_chars: int = 800,
                 monthlies: Optional[list[dict]] = None) -> str:
    """组装摘要提示词。

    `yearly` 走**月报**作原料（`monthlies`），其余走日日记 + 原文抽样。
    """
    label_cn = {"weekly": "一周", "monthly": "一个月",
                "yearly": "一年"}.get(kind, "一段时间")
    if kind == "yearly":
        lines = [
            f"（请为群 {group_id} 写 {label_cn}（{label}）的本人语气回顾，"
            f"约 {max(min(max_chars, 800), 120)} 字以内。）",
            "【各月月记】",
        ]
        ml = monthlies or []
        if ml:
            for rec in ml:
                lines.append(f"- {rec.get('period')}: {str(rec.get('summary'))[:300]}")
        else:
            lines.append("（无）")
        lines.append("\n请把这一年的脉络写成一段连贯的回顾，不要逐月罗列。")
        return "\n".join(lines)
    lines = [
        f"（请为群 {group_id} 写 {label_cn}（{label}）的本人语气摘要，"
        f"约 {max(min(max_chars, 800), 120)} 字以内。）",
        "【日日记】",
    ]
    if diaries:
        for rec in diaries:
            lines.append(f"- {rec.get('day')}: {str(rec.get('summary'))[:300]}")
    else:
        lines.append("（无）")
    lines.append("【原文抽样（防失真，可引用其语气但不要复读整段）】")
    if samples:
        for s in samples[:24]:
            lines.append(f"- {s}")
    else:
        lines.append("（无）")
    return "\n".join(lines)


def append_summary(path: Path, kind: str, group_id: str, label: str, summary: str, meta: dict) -> dict:
    import time as _time
    record = {
        "kind": kind,
        "group_id": group_id,
        "period": label,
        "summary": summary,
        "n_diaries": int(meta.get("n_diaries", 0)),
        "n_samples": int(meta.get("n_samples", 0)),
        "created_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


class SummaryService:
    def __init__(self, data_dir: str) -> None:
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def collect(self, kind: str, group_id: str, today: Optional[date] = None
                ) -> dict:
        """收集某一层级的原料。

        - `weekly` / `monthly`：窗口取**日日记**（+ 抽样原文防失真）
        - `yearly`：窗口取**12 篇月报**（月→年可整除，无错位；理由见
          `yearly_window` 的 docstring）
        """
        today = today or date.today()
        if kind == "weekly":
            start, end, label = weekly_window(today)
        elif kind == "monthly":
            start, end, label = monthly_window(today)
        else:
            start, end, label = yearly_window(today)

        if kind == "yearly":
            # 年报读月报：把该年 12 篇月报当作"原料"（每篇已是干净的聚合）
            src = list_summaries(self.output_path("monthly"), group_id,
                                 start, end, prefix=label)
            return {
                "kind": kind, "group_id": group_id, "label": label,
                "start": start.isoformat(), "end": end.isoformat(),
                "diaries": [], "samples": [],
                "monthlies": src,
                "n_diaries": len(src), "n_samples": 0,
            }

        # B-024: 双路径（现行 logs/<gid>/ 优先，根目录旧路径兼容）
        diaries = read_diaries(self._dir, group_id, start, end)
        samples = sample_days(str(self._dir), group_id, start, end)
        return {
            "kind": kind, "group_id": group_id, "label": label,
            "start": start.isoformat(), "end": end.isoformat(),
            "diaries": diaries, "samples": samples,
            "n_diaries": len(diaries), "n_samples": len(samples),
        }

    def output_path(self, kind: str) -> Path:
        return self._dir / {"weekly": WEEKLY_FILE, "monthly": MONTHLY_FILE,
                            "yearly": YEARLY_FILE}.get(kind, WEEKLY_FILE)
