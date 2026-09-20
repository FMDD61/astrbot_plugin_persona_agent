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
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

DIARY_FILE = "daily_diary.jsonl"
WEEKLY_FILE = "weekly_summary.jsonl"
MONTHLY_FILE = "monthly_summary.jsonl"
YEARLY_FILE = "yearly_summary.jsonl"
# ---------------------------------------------------------------- C31：格式与 PHI
#
# 设计依据 `docs/specs/summary_v2.md`：
#   §2  **格式必须约定死**（frontmatter + 正文）—— vision S6 的教训：
#       自由文本可用率 31.2%，约定固定形状后 100%；
#   §3  **正文永不进 RP 上下文**（每天往 §3 塞 200 字散文，与"把输出压回 7.9 字"
#       的方向相反）→ digest 进 §3，body 只喂上一层；
#   §7  PHI **包成末尾的 system 块**；原料留在 user（部分 provider 不接受全 system 请求）。

#: digest 与 body 之间的围栏（模型照抄的格式）
DIGEST_FENCE = "---"


def split_digest_body(text: str) -> tuple[str, str]:
    """把模型输出按 ``---`` 围栏切成 ``(digest, body)``（C31）。

    期望形状（summary_v2 §2）：

    ```
    ---
    date: 2026-09-17
    digest: 一句话
    ---
    正文……
    ```

    容错：
      * 没有围栏 → ``("", 全文)`` —— **正文照收**，只是这一篇没有 digest。
        宁可少一句摘要，也不要因为格式没遵守就丢掉整篇（那会让上一层原料凭空变少，
        而表现是"今天没发生什么"）；
      * 有围栏但缺 ``digest:`` 行 → 同上；
      * 模型把整段包进 ``` 代码围栏 → 先剥掉。
    """
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    lines = t.splitlines()
    idx = [i for i, ln in enumerate(lines) if ln.strip() == DIGEST_FENCE]
    if len(idx) < 2:
        return "", t
    head_lines = lines[idx[0] + 1:idx[1]]
    body = "\n".join(lines[idx[1] + 1:]).strip()
    digest = ""
    for ln in head_lines:
        m = re.match(r"^\s*digest\s*[:：]\s*(.+?)\s*$", ln)
        if m:
            digest = m.group(1).strip()
            break
    return digest, body


#: 四个 PHI（summary_v2 §5 定稿文案；一律作为**末尾的 system 块**）
DIARY_PHI = """【今天结束了】
把今天群里发生的事记成一篇日记，就按这个格式：

---
date: 今天的绝对日期
digest: 一句话，说清今天最值得记的事
---
正文，平实记述今天聊了什么、谁做了什么。

只写真的发生过的。"""

WEEKLY_PHI = """【这一周结束了】
下面是我这一周每天的日记。把这一周的事写成一篇周记，就按这个格式：

---
week: 周次
range: 起止日期
digest: 一句话，说清这一周最值得记的事
---
正文，把这一周的脉络写连贯，不要逐日罗列。

只写真的发生过的。"""

MONTHLY_PHI = """【这个月结束了】
下面是我这个月每周的记录。把这一个月的事写成一篇月记，就按这个格式：

---
month: 月份
digest: 一句话，说清这一个月最值得记的事
---
正文，把这一个月的脉络写连贯，不要逐周罗列。

只写真的发生过的。"""

YEARLY_PHI = """【这一年结束了】
下面是我这一年每个月的记录。把这一年的事写成一篇年记，就按这个格式：

---
year: 年份
digest: 一句话，说清这一年最值得记的事
---
正文，把这一年的脉络写连贯，不要逐月罗列。

只写真的发生过的。"""

PHI_BY_KIND = {"daily": DIARY_PHI, "weekly": WEEKLY_PHI,
               "monthly": MONTHLY_PHI, "yearly": YEARLY_PHI}


def build_phi(kind: str) -> str:
    """该层的 PHI（末尾 system 块）。**不含原料** —— 原料走 user（C31）。"""
    return PHI_BY_KIND.get(kind, "")


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
        # C31：年报读月报的 **body**（digest 只进 §3）
        body = str(rec.get("body") or rec.get("summary") or "").strip()
        if not body:
            continue
        out.append({"period": period, "body": body,
                    "digest": str(rec.get("digest") or "").strip()})
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
            # C31：新记录是 body/digest；旧记录只有 summary（= 当时的正文）
            body = str(rec.get("body") or rec.get("summary") or "").strip()
            if body:
                rec["body"] = body
                rec["digest"] = str(rec.get("digest") or "").strip()
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
    """组装**原料块**（进 user；指令与格式在 `build_phi()` 的 system 块里）。

    C31（`summary_v2.md` §7.2）把原来那条"原料 + 指令"的混合消息**拆成两半**：
      * 原料（日日记 / 各月月记 / 原文抽样）留在 **user**；
      * 指令与格式要求（PHI）挪到 **末尾的 system 块**。
    为什么必须拆：整条改成 system 会让请求里**没有任何非 system 消息**，
    部分 provider 不接受。拆完三个调用（日记/周月年记）形状完全同构。

    `yearly` 走**月报的 body** 作原料（定案：周/月读上一层 body，digest 只进 §3）。
    函数名保留 `build_prompt`（调用点与既有测试都在用），语义已收窄为"原料"。
    """
    if kind == "yearly":
        lines = ["【各月月记】"]
        ml = monthlies or []
        if ml:
            for rec in ml:
                text = str(rec.get("body") or rec.get("summary") or "")
                lines.append(f"- {rec.get('period')}: {text[:300]}")
        else:
            lines.append("（无）")
        return "\n".join(lines)
    lines = ["【日日记】"]
    if diaries:
        for rec in diaries:
            text = str(rec.get("body") or rec.get("summary") or "")
            lines.append(f"- {rec.get('day')}: {text[:300]}")
    else:
        lines.append("（无）")
    lines.append("【原文抽样（防失真，可引用其语气但不要复读整段）】")
    if samples:
        for s in samples[:24]:
            lines.append(f"- {s}")
    else:
        lines.append("（无）")
    return "\n".join(lines)


def append_summary(path: Path, kind: str, group_id: str, label: str,
                   summary: str, meta: dict) -> dict:
    """落盘一条摘要记录（C31：**digest + body 两字段**）。

    参数名保留 `summary`（调用点语义是"模型这次输出的全文"），但落盘时**切开**：
      * `digest` —— 一句话，只进 §3 长期记忆；
      * `body`   —— 正文，只喂**上一层**总结（周读日记 body、月读周 body…）；
      * **不再写 `summary`**（旧字段）。旧记录只有 `summary` 时，读侧按
        "body = body or summary" 回落（见 `list_diaries`）—— 迁移期不能凭空少原料。
    """
    import time as _time
    digest, body = split_digest_body(summary)
    record = {
        "kind": kind,
        "group_id": group_id,
        "period": label,
        "digest": digest,
        "body": body,
        "n_diaries": int(meta.get("n_diaries", 0)),
        "n_samples": int(meta.get("n_samples", 0)),
        "created_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
    }
    if not digest:
        # 格式没遵守：正文照收，但这件事必须可统计（summary_v2 §2 的可用率口径）
        record["digest_missing"] = True
    if not body:
        record["body_missing"] = True
    if not body and digest:
        # 极端兜底：只有 digest 没有正文 → 正文用 digest，别让上一层拿到空原料
        record["body"] = digest
        record["body_from_digest"] = True
    path.parent.mkdir(parents=True, exist_ok=True)
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
