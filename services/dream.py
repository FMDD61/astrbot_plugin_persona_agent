# -*- coding: utf-8 -*-
"""DreamService —— 从最近七篇日记"做梦"（S11）。

## 定位（用户 2026-09-14 明确）

- **不做熟悉度汇报** —— 那已从 DreamJob 剥离（"DreamJob 是做梦，不应该负责处理
  熟悉度汇报相关内容"）
- **产物要推送给用户**（周报之外，用户明确"接受除日记外的所有总结内容推送"）
- **原料** = 最近 7 个**不同 day** 的日记；不足 7 篇按实际数量做
- **残缺化** = 每篇日记**随机丢弃 20%~30% 的句子**（用户指定比例）
- **温度 1.3、思考强度 low**（用户指定，先跑再看效果调）
- 样例文本是**参考**，不是要照搬的结构/手法模板

## 两阶段（采纳子代理建议）

子代理核校素材后指出一个关键限制：

> 素材包能支撑"**怎么写**"，撑不起"**写什么**"。样例之所以成立，是因为日记里有
> **可触的身体细节**（发烧、膝盖疼、吃药）。建议先抽取 3–6 个身体/感觉锚点，
> 再让它们成为梦境中心 —— 这一步 few-shot 替代不了。

所以分两步：
  ① **锚点抽取**（低温、结构化）→ 3–6 个身体/感觉锚点（体温/疼痛/睡眠/气味/时段）
  ② **做梦**（温度 1.3、自由写作）→ 以锚点为"感觉中心"生成梦境语段

费用：每周 2 次调用，可忽略。

## 与旧实现的区别

旧 `DreamJob.run()` 做的是"关系变更建议 + 话题趋势"，且 cron **直挂 run() 从不推送**
（用户"上周没收到做梦内容"的根因）。本模块只负责"做梦"。
"""
from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# 残缺化比例（用户指定 20%~30%）
DROP_MIN = 0.20
DROP_MAX = 0.30

# 取几篇日记
DIARY_WINDOW = 7

# few-shot 片段：优先"课文感"弱的条目（子代理建议 —— 名篇课文在极高温度下
# 容易被整句照抄）。这里按 id 挑选，见 data_out/dream_fragments.json。
DEFAULT_FEWSHOT_IDS = ("a3", "a19", "a13", "a11", "a26", "a17")


# ---------------------------------------------------------------- 日记取材

def split_sentences(text: str) -> list[str]:
    """按中文句末标点切句（保留标点）。"""
    t = (text or "").strip()
    if not t:
        return []
    # 在句末标点后切分，标点归属前一句
    parts = re.split(r"(?<=[。！？；…])", t)
    return [p.strip() for p in parts if p and p.strip()]


def fragment(text: str, rnd: random.Random,
             drop_min: float = DROP_MIN, drop_max: float = DROP_MAX) -> str:
    """**残缺化**：随机丢弃 20%~30% 的句子（用户指定的比例与含义）。

    为什么丢句子而不是"截断"或"只给摘要"：丢句保留单句完整，读起来像
    "梦里的遗忘"（记得片段、忘了连接），而截断会留下半句、摘要会抹掉质感。

    护栏：**至少保留 1 句**。句子少于 3 句时不丢（丢了就不成篇）。
    """
    sents = split_sentences(text)
    if len(sents) < 3:
        return text.strip()
    ratio = rnd.uniform(drop_min, drop_max)
    n_drop = max(1, int(round(len(sents) * ratio)))
    # 护栏：句子少时取整会跌破下限（实测 6 句时 20% 算得 1 句 = 17%）。
    # 用 ceil 抬到下限，但仍不超过"至少留 1 句"。
    floor = int(drop_min * len(sents) + 0.999)    # ceil
    if n_drop < floor and floor <= len(sents) - 1:
        n_drop = floor
    n_drop = min(n_drop, len(sents) - 1)          # 至少留 1 句
    keep_idx = sorted(rnd.sample(range(len(sents)), len(sents) - n_drop))
    return "".join(sents[i] for i in keep_idx)


def gather_diaries(data_dir: str | Path, group_id: str,
                   limit: int = DIARY_WINDOW) -> list[dict]:
    """收集最近 ``limit`` 个**不同 day** 的日记（同日取最新一条）。

    取材位置有两个（历史原因）：
      - ``<data>/logs/<gid>/daily_diary.jsonl``（现行）
      - ``<data>/daily_diary.jsonl``（早期根目录，保留兼容）

    实测存在**同日多条**（B-018 幂等是后来才加的，旧重复仍在）→ 这里按 day
    去重取最新，不依赖清理历史。
    """
    base = Path(data_dir)
    paths = [base / "logs" / str(group_id) / "daily_diary.jsonl",
             base / "daily_diary.jsonl"]
    by_day: dict[str, dict] = {}
    for p in paths:
        if not p.exists():
            continue
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(r, dict):
                continue
            if str(r.get("group_id") or "") != str(group_id):
                continue
            day = str(r.get("day") or "")
            summary = str(r.get("summary") or "").strip()
            if not day or not summary:
                continue
            # 同日取最新：用 created_at 排序；缺失则后出现的覆盖（文件是追加的）
            prev = by_day.get(day)
            if prev is None or str(r.get("created_at") or "") >= str(
                    prev.get("created_at") or ""):
                by_day[day] = {"day": day, "summary": summary,
                               "created_at": str(r.get("created_at") or "")}
    days = sorted(by_day, reverse=True)[:limit]
    return [by_day[d] for d in sorted(days)]      # 按日期升序返回（时间顺序）


# ---------------------------------------------------------------- few-shot

def load_fragments(data_dir: str | Path, ids=DEFAULT_FEWSHOT_IDS) -> list[dict]:
    """读风格锚定片段（公版名篇）。

    只在 ``<data>/dream_fragments.json`` 找不到时才去工作区找 —— 生产环境
    以数据目录为准（部署时随插件一起放过去）。
    """
    cands = [Path(data_dir) / "dream_fragments.json"]
    for p in cands:
        if not p.exists():
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        items = {str(x.get("id")): x for x in (d.get("items") or [])
                 if isinstance(x, dict)}
        out = [items[i] for i in ids if i in items]
        if out:
            return out
    return []


# ---------------------------------------------------------------- 提示词

# 锚点抽取（阶段①）—— **强制 JSON schema**，低温
#
# 🔴 实测（2026-09-14）：自由文本 + "不要解释"反而让模型**反复纠结**
# "什么才算身体感觉"，2048 token 全花在思考上、`finish_reason=length`、
# content 全空（与 B-019 识图同形 —— **约定输出格式是唯一主导因素**）。
# 实测对比（同一批日记）：
#   自由文本「每条一行，不要解释」      → length / 14.9s / completion 2048（全思考）/ **空**
#   自由文本 + max_tokens 4096          → stop   / 14.1s / 思考 1818 / 输出是**模仿口吻的句子**
#   **JSON schema**                     → stop   / **6.1s** / completion 612 / ✅ 有效锚点
ANCHOR_SYSTEM = (
    "读日记，列出其中**身体和感官**的细节。\n"
    "只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块：\n"
    '{"anchors": ["...", "..."]}\n'
    "anchors 放 3-6 条，每条 6-16 字，只写体感"
    "（体温/疼/困/饿/气味/光线/声音/时段），不写事件、不写人名、不写观点。"
)


def parse_anchors(raw: str) -> str:
    """从锚点响应里取 ``anchors`` 列表，拼成逐行文本（失败返回原文本）。"""
    t = (raw or "").strip()
    if not t:
        return ""
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    obj = None
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
            except json.JSONDecodeError:
                obj = None
    if isinstance(obj, dict):
        items = obj.get("anchors")
        if isinstance(items, list):
            lines = [str(x).strip() for x in items if str(x).strip()]
            if lines:
                return "\n".join(lines)
    # 兜底：原样返回（调用方仍能当参考文本用）
    return t


# 做梦（阶段②）—— 自由写作
DREAM_SYSTEM = (
    "你刚睡醒，要写下你做的梦。\n"
    "\n"
    "下面给你几篇日记（有缺失，句子被随机丢掉了一些）、几个感觉锚点、"
    "以及几段别人写的文字作为**语感参考**。\n"
    "\n"
    "请写一段 400-700 字的梦境语段。要点：\n"
    "- 把感觉写成有质地、有重量、能碰到的东西（光、声音、温度都可以是物质）\n"
    "- 时间只缓慢推移，不推进情节\n"
    "- 一次只围绕一两个「感觉的中心」转，不要串成故事\n"
    "- 群友的名字可以保留（它们是梦里的坐标），但**不要复述他们做了什么**\n"
    "- 结尾让一切慢慢沉降下去，停在半明半暗的地方，不要点题\n"
    "\n"
    "**不要**：\n"
    "- 出现「仿佛整个世界都安静了」「时间仿佛静止」「心中涌起一股暖流」"
    "「不知过了多久」这类套语\n"
    "- 在比喻后面再解释这个比喻意味着什么\n"
    "- 写「梦醒了」「这个梦告诉我」「梦的尽头是……」这类元叙述\n"
    "- 总结中心思想、写金句、排比、滥用感叹号\n"
    "\n"
    "参考文字只用来找语感，**不要照抄其中的句子**。"
)


@dataclass
class DreamResult:
    text: str = ""
    anchors: str = ""
    days: list[str] = field(default_factory=list)
    diaries_used: int = 0
    fragments_used: int = 0
    dropped_ratio: float = 0.0
    stats: dict = field(default_factory=dict)


def build_anchor_prompt(diaries: list[dict]) -> str:
    parts = ["以下是几篇群聊日记：\n"]
    for d in diaries:
        parts.append(f"［{d['day']}］\n{d['summary']}\n")
    parts.append("\n请按要求抽出身体与感觉的锚点。")
    return "\n".join(parts)


def build_dream_prompt(diaries: list[dict], anchors: str,
                       fragments: list[dict]) -> str:
    parts: list[str] = []
    parts.append("## 日记（有缺失）\n")
    for d in diaries:
        parts.append(f"［{d['day']}］\n{d['summary']}\n")
    if anchors.strip():
        parts.append("## 感觉锚点\n" + anchors.strip() + "\n")
    if fragments:
        parts.append("## 语感参考（不要照抄）\n")
        for f in fragments:
            txt = str(f.get("text") or "").strip()
            if txt:
                parts.append(f"［{f.get('author', '')}］{txt}\n")
    parts.append("\n请写下你的梦。")
    return "\n".join(parts)


# ---------------------------------------------------------------- 编排

class DreamMaker:
    """把"取日记 → 残缺化 → 抽锚点 → 做梦"串起来。

    两个 LLM 调用由调用方注入（``anchor_fn`` / ``dream_fn``）—— 保持本类
    **不依赖 astrbot 运行时**，可离线单测。
    """

    def __init__(self, data_dir: str | Path, *,
                 anchor_fn=None, dream_fn=None,
                 seed: Optional[int] = None,
                 fragment_ids=DEFAULT_FEWSHOT_IDS) -> None:
        self._dir = Path(data_dir)
        self._anchor_fn = anchor_fn
        self._dream_fn = dream_fn
        self._rnd = random.Random(seed)
        self._ids = fragment_ids

    def prepare(self, group_id: str, limit: int = DIARY_WINDOW) -> DreamResult:
        """只做本地准备（取日记 + 残缺化 + 取片段），不调 LLM。

        返回 ``(DreamResult, 残缺化后的日记, few-shot 片段)`` —— **恒为三元组**。
        """
        diaries = gather_diaries(self._dir, group_id, limit=limit)
        if not diaries:
            # ⚠️ 必须返回**同构**的三元组：早先这里返回单个 DreamResult，
            # 导致 make() 解包时 TypeError（测试抓到）。用空列表占位。
            return DreamResult(stats={"error": "no_diaries"}), [], []
        total_sents = 0
        dropped = 0
        frag_diaries: list[dict] = []
        for d in diaries:
            before = split_sentences(d["summary"])
            after_txt = fragment(d["summary"], self._rnd)
            after = split_sentences(after_txt)
            total_sents += len(before)
            dropped += max(0, len(before) - len(after))
            frag_diaries.append({"day": d["day"], "summary": after_txt})
        fragments = load_fragments(self._dir, ids=self._ids)
        return DreamResult(
            days=[d["day"] for d in diaries],
            diaries_used=len(diaries),
            fragments_used=len(fragments),
            dropped_ratio=round(dropped / total_sents, 3) if total_sents else 0.0,
            stats={"total_sentences": total_sents, "dropped_sentences": dropped},
        ), frag_diaries, fragments

    async def make(self, group_id: str, limit: int = DIARY_WINDOW) -> DreamResult:
        """完整流程：① 抽锚点 ② 做梦。任一阶段失败则返回带 error 的结果。"""
        res, frag_diaries, fragments = self.prepare(group_id, limit=limit)
        if res.stats.get("error"):
            return res
        anchors = ""
        if self._anchor_fn is not None:
            try:
                anchors = parse_anchors(str(await self._anchor_fn(
                    ANCHOR_SYSTEM, build_anchor_prompt(frag_diaries)) or ""))
            except Exception as e:
                # 锚点失败不致命 —— 降级为"直接做梦"（少了意象源但仍有日记）
                res.stats["anchor_error"] = f"{type(e).__name__}: {e}"
        if self._dream_fn is None:
            res.stats["error"] = "no_dream_fn"
            return res
        try:
            text = str(await self._dream_fn(
                DREAM_SYSTEM,
                build_dream_prompt(frag_diaries, anchors, fragments)) or "").strip()
        except Exception as e:
            res.stats["error"] = f"dream_failed: {type(e).__name__}: {e}"
            return res
        res.text = text
        res.anchors = anchors
        if not text:
            res.stats["error"] = "empty_dream"
        return res


def persist_dream(data_dir: str | Path, group_id: str, res: DreamResult) -> dict:
    """把梦记进 ``dreams.jsonl``（长期留存 + 供推送与 LLM 消费）。"""
    import time
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "group_id": str(group_id),
        "days": res.days,
        "diaries_used": res.diaries_used,
        "fragments_used": res.fragments_used,
        "dropped_ratio": res.dropped_ratio,
        "anchors": res.anchors,
        "dream": res.text,
        "stats": res.stats,
    }
    p = Path(data_dir) / "logs" / str(group_id) / "dreams.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec
