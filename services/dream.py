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

## 单阶段（2026-09-19 删除锚点阶段，C33⑤）

原先分两步：① 低温抽 3–6 个身体/感觉锚点 → ② 高温做梦，以锚点为"感觉中心"。
**用户 2026-09-19 决定删掉阶段①**：

> 「**锚点直接删去**。这一部分**未在设计内**，而且**会对 LLM 的注意力
> 产生不确定的引导行为**。」

依据侧的两条事实（`docs/specs/dream_v2.md` §10）：

1. 该设计**出自一个子代理的推理**，不是实测；
2. 三次真实运行里有两次锚点为空（等于顺带跑了"无锚点"组），**没锚点那次也成立** ——
   现有数据构成一个非正式反例。

删除后：**每周只剩 1 次 LLM 调用**，且不再有"锚点空返回无从分辨"那一类留痕问题
（C33③ 随之作废 —— 那条留痕要求的存在前提就是阶段①）。

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
            # C31（summary_v2 §7.1）：**取 body 做原料**（digest 只进 §3 长期记忆）。
            digest = str(r.get("digest") or "").strip()
            body = str(r.get("body") or "").strip()
            legacy = False
            if not body:
                # ⚠️ 迁移桥：旧记录只有 `summary`（那就是当时的正文）。
                # **不能因为缺 body 就跳过** —— 迁移首日会让 dream/周报的原料全空，
                # 而表现是"没有日记"（静默失效；独立核验第 1 轮风险 2）。
                # 回落的事实要能被统计：`legacy_summary=True` → 进 stats。
                body = str(r.get("summary") or "").strip()
                legacy = bool(body)
            if not day or not body:
                continue
            # 同日取最新：用 created_at 排序；缺失则后出现的覆盖（文件是追加的）
            prev = by_day.get(day)
            if prev is None or str(r.get("created_at") or "") >= str(
                    prev.get("created_at") or ""):
                by_day[day] = {"day": day, "body": body, "digest": digest,
                               "legacy_summary": legacy,
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

# 做梦 —— 自由写作
#
# 身份（C33④，`dream_v2.md` §9 定案）：**只给 §4 世界/群聊信息** ——
# 梦的"我"靠日记的第一人称隐含继承（保留朦胧感），给身份段反而可能把
# 文学指令带偏。世界段由 `build_dream_system()` 追加（调用方注入）。
DREAM_SYSTEM = (
    "你刚睡醒，要写下你做的梦。\n"
    "\n"
    "下面给你几篇日记（有缺失，句子被随机丢掉了一些）、"
    "以及几段别人写的文字作为**语感参考**。\n"
    "\n"
    "请写一段四五百字上下的梦境语段。要点：\n"
    "- 把感觉写成有质地、有重量、能碰到的东西（光、声音、温度都可以是物质）\n"
    "- 时间只缓慢推移，不推进情节\n"
    "- 一次只围绕一两个「感觉的中心」转，不要串成故事\n"
    # 🔴 2026-09-14 实测修正（用户指出 + 6 次采样量化）：
    # 原句 "群友的名字可以保留（它们是梦里的坐标），但**不要复述他们做了什么**"
    # 有**两处**错误，直接导致了"点名清单"：
    #   ① "可以保留（它们是梦里的坐标）" —— 这不是禁止而是**生成指令**，
    #      而且"坐标"这个比喻被**字面执行**：名字变成空间静物
    #      （实测"浮在墙角""压在枕头底下""漂过去"）
    #   ② "不要复述他们做了什么" 堵死了名字与动作的自然结合 → 模型只能**陈列**名字
    # 实测：模型产出每 100 字 1.0–1.8 个名字（7–14 个），其中 2/6 次出现连续罗列
    # （"成员申、早上好、成员酉——这些字在视网膜上留着残影"）；而用户样例全篇仅 4 个
    # 名字且都嵌在叙述里。且样本里模型**违反**了那条禁令（"成员未说肚子痛"）——
    # 说明禁令方向就是错的。
    # ⇒ 改为不预设名字该不该出现；不加新禁令（用户：示例是参考，不是模板）。
    "- 群友的名字让它自然出现就好，不必刻意提起，也不必回避\n"
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


def build_dream_system(world_block: str = "") -> str:
    """做梦的 system = `DREAM_SYSTEM` ＋ **§4 世界段**（C33④）。

    只给世界段：它是唯一进梦的设定（`dream_v2.md` §9）。世界段为空时原样返回 ——
    宁可不给，也不要塞一个空标题。
    """
    base = DREAM_SYSTEM.strip()
    world = (world_block or "").strip()
    return f"{base}\n\n{world}" if world else base


@dataclass
class DreamResult:
    text: str = ""
    days: list[str] = field(default_factory=list)
    diaries_used: int = 0
    fragments_used: int = 0
    dropped_ratio: float = 0.0
    stats: dict = field(default_factory=dict)


def build_dream_prompt(diaries: list[dict], fragments: list[dict]) -> str:
    """做梦的 user（原料）。锚点阶段已删（C33⑤）→ 不再有锚点段。"""
    parts: list[str] = []
    parts.append("## 日记（有缺失）\n")
    for d in diaries:
        parts.append(f"［{d['day']}］\n{d['text']}\n")
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
    """把"取日记 → 残缺化 → 做梦"串起来（**单阶段**，C33⑤）。

    唯一那次 LLM 调用由调用方注入（``dream_fn``）—— 保持本类
    **不依赖 astrbot 运行时**，可离线单测。
    """

    def __init__(self, data_dir: str | Path, *,
                 dream_fn=None,
                 seed: Optional[int] = None,
                 fragment_ids=DEFAULT_FEWSHOT_IDS,
                 world_block: str = "") -> None:
        self._dir = Path(data_dir)
        self._dream_fn = dream_fn
        # C33④：system = DREAM_SYSTEM + §4 世界段（梦唯一的设定来源）
        self._system = build_dream_system(world_block)
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
        legacy_n = 0
        frag_diaries: list[dict] = []
        for d in diaries:
            # C33②：原料取 **body**（不是 summary/digest）
            before = split_sentences(d["body"])
            after_txt = fragment(d["body"], self._rnd)
            after = split_sentences(after_txt)
            total_sents += len(before)
            dropped += max(0, len(before) - len(after))
            if d.get("legacy_summary"):
                legacy_n += 1
            frag_diaries.append({"day": d["day"], "text": after_txt})
        fragments = load_fragments(self._dir, ids=self._ids)
        stats = {"total_sentences": total_sents, "dropped_sentences": dropped}
        if legacy_n:
            # 降级必须可见：这些日记是**旧格式**（只有 summary），原料质量可能偏低
            stats["legacy_summary_diaries"] = legacy_n
        return DreamResult(
            days=[d["day"] for d in diaries],
            diaries_used=len(diaries),
            fragments_used=len(fragments),
            dropped_ratio=round(dropped / total_sents, 3) if total_sents else 0.0,
            stats=stats,
        ), frag_diaries, fragments

    async def make(self, group_id: str, limit: int = DIARY_WINDOW) -> DreamResult:
        """完整流程（**单阶段**，C33⑤）：取日记 → 残缺化 → 做梦。"""
        res, frag_diaries, fragments = self.prepare(group_id, limit=limit)
        if res.stats.get("error"):
            return res
        if self._dream_fn is None:
            res.stats["error"] = "no_dream_fn"
            return res
        try:
            text = str(await self._dream_fn(
                self._system,
                build_dream_prompt(frag_diaries, fragments)) or "").strip()
        except Exception as e:
            res.stats["error"] = f"dream_failed: {type(e).__name__}: {e}"
            return res
        res.text = text
        if not text:
            res.stats["error"] = "empty_dream"
        return res


def persist_dream(data_dir: str | Path, group_id: str, res: DreamResult) -> dict:
    """把梦记进 ``dreams.jsonl``（长期留存 + **供推送**）。

    ⚠️ 原注释写「＋供 LLM 消费」是**错的**（C33①，用户 2026-09-19 确认
    「不知道是之前哪个子代理乱写的」）：梦的产物**只推送给人**，
    没有任何 LLM 消费者（梦不进 RP 上下文）。
    """
    import time
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "group_id": str(group_id),
        "days": res.days,
        "diaries_used": res.diaries_used,
        "fragments_used": res.fragments_used,
        "dropped_ratio": res.dropped_ratio,
        "dream": res.text,
        "stats": res.stats,
    }
    p = Path(data_dir) / "logs" / str(group_id) / "dreams.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec
