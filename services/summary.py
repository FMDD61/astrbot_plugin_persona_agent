"""summary — G13 周/月摘要金字塔（纯 stdlib，可离线单测）。

数据流：daily_diary.jsonl（日日记，v3 已产出）→ 周（周一 02:10）/ 月
（1 日 02:15）cron 汇总 → weekly_summary.jsonl / monthly_summary.jsonl；
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
                 samples: list[str], max_chars: int = 800) -> str:
    label_cn = "一周" if kind == "weekly" else "一个月"
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
        today = today or date.today()
        start, end, label = weekly_window(today) if kind == "weekly" else monthly_window(today)
        diaries = list_diaries(self._dir / DIARY_FILE, group_id, start, end)
        samples = sample_days(str(self._dir), group_id, start, end)
        return {
            "kind": kind, "group_id": group_id, "label": label,
            "start": start.isoformat(), "end": end.isoformat(),
            "diaries": diaries, "samples": samples,
            "n_diaries": len(diaries), "n_samples": len(samples),
        }

    def output_path(self, kind: str) -> Path:
        return self._dir / (WEEKLY_FILE if kind == "weekly" else MONTHLY_FILE)
