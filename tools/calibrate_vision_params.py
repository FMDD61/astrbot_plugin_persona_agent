#!/usr/bin/env python3
"""calibrate_vision_params — 识图调用 LLM 参数的离线标定台。

## 为什么需要它

`services/vision.py` / `tools/build_sticker_index.py` 的识图调用有约 8% 的图片返回
``{"content": "", "reasoning": "<很长>"}`` —— **正文缺失**。实测（2026-09-13）抓到的
真因不是"模型不听话"，而是 **推理 token 吃光了 ``max_tokens``**：

    max_tokens=512  → finish_reason="length", completion_tokens=512,
                      reasoning_tokens=512, content=""     ← 正文一个 token 都没轮到
    max_tokens=1024 → finish_reason="stop",  reasoning_tokens=175, content=正文 ✓

但 max_tokens 不是唯一变量：提示词是否约定**固定返回格式**会直接改变模型"思考排版"
的长度（实测同一张图，自由文本格式的思考里有一大半在纠结"要不要包含关键词、怎么写"）。
所以本脚本把 format / temperature / reasoning_effort / max_tokens 拉成网格做**配对实验**。

## 设计要点

- **复用生产预处理**：直接 import ``tools.build_sticker_index.prepare_for_vision``
  （768px / JPEG q82 / gif 首帧），不重写 —— 标定结果必须能搬到生产。
- **配对（paired）**：同一组图片跑遍所有配置，逐图比较，避免"难图碰巧落在某个配置"。
- **混合测试集**：``--images-dir`` 可给多次，默认**按目录轮转抽样**（``--n`` 是总数）。
  建议一个目录放"历史失败样本"（难图，信号强）、一个放随机正常样本（无偏基准）。
- **只读 content 也给结论**：每条记录都留下 ``usage`` / ``finish_reason``，
  于是"截断"与"模型把答案塞进 reasoning"可以分开统计，不靠猜。
- **可断点续跑**：每次调用立刻 append 到 ``<out>.jsonl``；重跑时已记录的
  (config, image) 组合直接跳过。跑一半被限流/超时不用从头再来。
- **不打印密钥**：只从环境变量读，且只出现一次；日志里绝不含 key。

## 用法

    export STICKER_VISION_API_BASE="$(head -1 /tmp/cred.txt | tr -d '\\r\\n')"
    export STICKER_VISION_API_KEY="$(tail -1 /tmp/cred.txt | tr -d '\\r\\n')"

    # 全网格筛查（默认 19 个配置，串行 + 间隔；实测 ~8s/次）
    python3 -m tools.calibrate_vision_params \\
        --images-dir ../emote/_失败_81张 \\
        --images-dir ../data_out/sticker_library \\
        --n 24 --out /tmp/vision_calib.json

    # 只跑指定配置（id 精确或前缀匹配）
    python3 -m tools.calibrate_vision_params --configs b-t0.0-low-1024,a-t0.3-low-512 ...

    # 稳定性：同一 (配置,图片) 重复 3 次（断点续跑按已有成功数补齐）
    python3 -m tools.calibrate_vision_params --repeats 3 --configs b-t0.0-low-1024 ...

    # 原图直送 vs 降采样（量化 services/vision.py 现在的"不降采样"代价）
    python3 -m tools.calibrate_vision_params --raw-images --tag raw ...

    # 只看配置清单
    python3 -m tools.calibrate_vision_params --list-configs

    # 只汇总已有 jsonl（不发起调用；jsonl 路径 = <out>.jsonl）
    python3 -m tools.calibrate_vision_params --out /tmp/vision_calib.json --analyze-only
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tools.build_sticker_index import prepare_for_vision  # noqa: E402

EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"

# ---------------------------------------------------------------- 提示词

# (a0) 生产 services/vision.py 的 system prompt（无关键词要求）
SYS_A0 = (
    "用中文简要描述图片中确定可见的内容，不超过80字；如是表情包说明其情绪和梗。"
    "不要猜测人物身份、不要脑补图中没有的内容；看不清就说看不清。"
)

# (a) 现状 tools/build_sticker_index.py 的 system prompt（自由文本 + 关键词要求）
SYS_A = (
    "用中文简要描述这张图片中确定可见的内容，不超过80字；"
    "如是表情包说明其情绪和梗，并给出 2-4 个适合检索的情绪/动作关键词。"
    "不要猜测人物身份、不要脑补图中没有的内容；看不清就说看不清。"
)

# (b) 只输出一个 JSON 对象 —— 用 schema 消灭"排版纠结"
SYS_B = (
    "你是图片标注器。看图片，只输出**一个 JSON 对象**，不要任何解释、不要 markdown 代码块。"
    '格式固定为：{"desc": "图片描述", "tags": ["关键词1", "关键词2"]}\n'
    "字段约束：\n"
    '- "desc"：中文字符串，只描述图中**确定可见**的内容，长度 8-80 字，'
    "一句话写完，不加换行；若是表情包，在句中点明情绪（如委屈/无语/嘲讽）与可能的梗；"
    "不确定的不要写。\n"
    '- "tags"：2-4 个中文字符串，每个 2-6 字，是可用于检索的情绪/动作关键词'
    "（如「无语」「抱头」「流泪」）。\n"
    "不要猜测人物身份、不要脑补图中没有的内容；看不清就在 desc 里写「画面模糊，看不清」。"
)

# (c) 固定标记行 —— 便于程序稳定截取，同时保留自然行文
SYS_C = (
    "你是图片描述器。严格按下面的两行格式输出，不要输出任何其他内容、不要解释、不要 markdown：\n"
    "描述：<一句话，中文字符串，只写图中确定可见的内容，8-80 字；"
    "若是表情包，在句中点明情绪与可能的梗>\n"
    "关键词：<2-4 个中文关键词，每个 2-6 字，用中文顿号、分隔>\n"
    "第一行必须以「描述：」开头，第二行必须以「关键词：」开头。"
    "不要猜测人物身份、不要脑补图中没有的内容；看不清就在描述里写「画面模糊，看不清」。"
)

FORMATS: dict[str, str] = {"a": SYS_A, "a0": SYS_A0, "b": SYS_B, "c": SYS_C}

USER_TEXT = "描述这张图片。"


# ---------------------------------------------------------------- 配置网格

def _cfg(fmt: str, temp: float, effort: Optional[str], max_tokens: int) -> dict[str, Any]:
    e = effort or "none"
    return {
        "id": f"{fmt}-t{temp}-{e}-{max_tokens}",
        "format": fmt,
        "temperature": temp,
        "reasoning_effort": effort,      # None = 完全不发送该字段
        "max_tokens": max_tokens,
        "system": FORMATS[fmt],
    }


# 18 个配置：覆盖 format{a,a0,b,c} × temp{0.0,0.3,0.7} × effort{low,不发送}
# × max_tokens{512,1024,2048}，并含**逐字复刻的生产配置** a-t0.3-low-512 作基准。
CONFIGS: list[dict[str, Any]] = [
    # —— 现状基准（build_sticker_index 逐字复刻）
    _cfg("a", 0.3, "low", 512),
    # —— 只动 max_tokens
    _cfg("a", 0.3, "low", 1024),
    _cfg("a", 0.3, "low", 2048),
    # —— 只动 temperature
    _cfg("a", 0.0, "low", 1024),
    _cfg("a", 0.7, "low", 1024),
    # —— 只动 reasoning_effort（None = 不发送该参数）
    _cfg("a", 0.0, None, 1024),
    # —— 无关键词要求的旧 prompt（对照：思考是否被"要不要关键词"拖长）
    _cfg("a0", 0.3, "low", 1024),
    # —— 格式 b（JSON）
    _cfg("b", 0.3, "low", 1024),
    _cfg("b", 0.0, "low", 1024),
    _cfg("b", 0.7, "low", 1024),
    _cfg("b", 0.0, None, 1024),
    _cfg("b", 0.0, "low", 512),
    _cfg("b", 0.0, "low", 2048),
    # —— 格式 c（固定标记行）
    _cfg("c", 0.3, "low", 1024),
    _cfg("c", 0.0, "low", 1024),
    _cfg("c", 0.7, "low", 1024),
    _cfg("c", 0.0, None, 1024),
    _cfg("c", 0.0, "low", 512),
    _cfg("c", 0.0, "low", 2048),
]

# ---------------------------------------------------------------- 解析/判定

# 提示词复述 / 自我规训特征（沿用 services/vision.py 的 _LEAK_MARKERS 并补齐任务清单）
LEAK_MARKERS = (
    "如是表情包", "不超过80", "不猜测人物", "不要脑补", "需要谨慎", "不能猜",
    "用户说", "本条要求", "分析请求", "草拟描述", "字数检查",
    "不要输出", "格式固定", "字段约束", "严格按下面", "第一行必须", "关键词：<",
)


def looks_like_leaked_prompt(text: str) -> bool:
    return any(m in (text or "") for m in LEAK_MARKERS)


def strip_code_fence(s: str) -> str:
    t = (s or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def split_tags(raw: str) -> list[str]:
    """把「无语、嫌弃 侧目;呆滞」这类串切成干净的关键词列表。"""
    parts = re.split(r"[、,，;；/|\s]+", (raw or "").strip())
    out: list[str] = []
    for p in parts:
        p = p.strip().strip("。.！!？?\"'「」【】[]()（）*#-—")
        if 1 <= len(p) <= 8 and not looks_like_leaked_prompt(p):
            out.append(p)
    return out


_KW_RE = re.compile(r"关键词\s*[:：]\s*([^\n]+)")


def parse_output(content: str, reasoning: str, fmt: str) -> dict[str, Any]:
    """按格式解析一次响应。返回 desc/tags/合规标志，**不做** reasoning 兜底。"""
    res: dict[str, Any] = {
        "desc": "", "tags": [], "parse_ok": False,
        "json_raw_ok": False, "json_recovered_ok": False, "marker_ok": False,
        "desc_len": 0,
    }
    text = (content or "").strip()
    if not text:
        return res

    if fmt == "b":
        obj = None
        try:
            obj = json.loads(text)
            res["json_raw_ok"] = isinstance(obj, dict)
        except Exception:
            try:
                obj = json.loads(strip_code_fence(text))
                res["json_recovered_ok"] = isinstance(obj, dict)
            except Exception:
                obj = None
        if isinstance(obj, dict):
            desc = str(obj.get("desc") or obj.get("description") or "").strip()
            tags = obj.get("tags") or obj.get("keywords") or []
            if isinstance(tags, str):
                tags = split_tags(tags)
            tags = [str(t).strip() for t in tags if str(t).strip()][:8]
            res.update(desc=desc, tags=tags, parse_ok=bool(desc))
            res["desc_len"] = len(desc)
            return res
        # JSON 解析失败 → 退化：可能模型还是写了 "描述：xxx" 或纯文本
        m = re.search(r'"desc"\s*:\s*"([^"]{2,})"', text)
        if m:
            res["desc"] = m.group(1).strip()
        if res["desc"]:
            res["parse_ok"] = True
            res["desc_len"] = len(res["desc"])
        return res

    if fmt == "c":
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if lines and lines[0].startswith("描述："):
            res["marker_ok"] = True
            res["desc"] = lines[0][len("描述："):].strip()
            if not res["desc"] and len(lines) > 1:
                res["desc"] = lines[1]
        else:
            m = re.search(r"描述\s*[:：]\s*([^\n]+)", text)
            if m:
                res["desc"] = m.group(1).strip()
        m = _KW_RE.search(text)
        if m:
            res["tags"] = split_tags(m.group(1))
        res["parse_ok"] = bool(res["desc"])
        res["desc_len"] = len(res["desc"])
        return res

    # fmt a / a0：自由文本。整段（可多行）都是描述
    res["desc"] = text
    m = _KW_RE.search(text)
    if m:
        res["tags"] = split_tags(m.group(1))
        # 描述部分 = 去掉「关键词：…」那一行
        res["desc"] = _KW_RE.sub("", text).strip().strip("。;；").strip()
    res["parse_ok"] = bool(res["desc"])
    res["desc_len"] = len(res["desc"])
    return res


def production_fallback(reasoning: str) -> str:
    """复刻 services/vision.py::extract_completion_text 的 reasoning 兜底。"""
    if not reasoning:
        return ""
    for pat in (r"(?:最后输出|最终输出|最终润色|最终答案|结论)[：:]?\s*([^\n]{2,})",
                r"(?:输出|答案)[：:]?\s*([^\n]{2,})$"):
        m = re.search(pat, reasoning)
        if m:
            cand = m.group(1).strip()
            return "" if looks_like_leaked_prompt(cand) else cand
    tail = [l.strip() for l in reasoning.splitlines() if l.strip()]
    cand = tail[-1] if tail else ""
    if len(cand) <= 40 and not looks_like_leaked_prompt(cand):
        return cand
    return ""


def judge(rec: dict[str, Any]) -> dict[str, Any]:
    """一条记录的判定结果（content 层 + 管线层）。"""
    content = str(rec.get("content") or "").strip()
    reasoning = str(rec.get("reasoning") or "")
    fmt = rec["format"]
    p = parse_output(content, reasoning, fmt)
    desc = p["desc"]
    usable = bool(desc) and len(desc) >= 8 and not looks_like_leaked_prompt(desc)
    tags = p["tags"]
    kw_ok = 2 <= len(tags) <= 4
    fb = production_fallback(reasoning) if not content else ""
    fb_usable = bool(fb) and len(fb) >= 8 and not looks_like_leaked_prompt(fb)

    compliant = True
    if fmt == "b":
        compliant = p["json_raw_ok"] and bool(p["desc"]) and 2 <= len(tags) <= 4 and len(desc) <= 100
    elif fmt == "c":
        compliant = p["marker_ok"] and bool(desc) and kw_ok
    else:
        # 自由文本：「不超过80字」放宽到 100 字算合规（中文按字符计）
        compliant = bool(desc) and len(desc) <= 100

    return {
        "content_nonempty": bool(content),
        "desc": desc,
        "desc_len": len(desc),
        "usable": usable,
        "tags": tags,
        "kw_ok": kw_ok,
        "json_raw_ok": p["json_raw_ok"],
        "json_recovered_ok": p["json_recovered_ok"],
        "marker_ok": p["marker_ok"],
        "compliant": compliant,
        "leaked": looks_like_leaked_prompt(desc) if desc else False,
        "fallback_desc": fb,
        "fallback_usable": fb_usable,
    }


# ---------------------------------------------------------------- 抽样

def _list_images(d: Path) -> list[Path]:
    if not d.is_dir():
        raise SystemExit(f"--images-dir 不是目录: {d}")
    return sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in EXTS)


def pick_images(dirs: list[Path], n: int, seed: int,
                exclude: Optional[set[str]] = None) -> list[dict[str, Any]]:
    """按目录轮转抽样，跨目录按 sha256 去重（同图不同名会让配对统计失真）。"""
    import hashlib

    exclude = exclude or set()
    rng = random.Random(seed)
    pools = []
    for d in dirs:
        items = [p for p in _list_images(d) if p.name not in exclude]
        rng.shuffle(items)
        pools.append((d, items))
    chosen: list[dict[str, Any]] = []
    seen: set[str] = set()
    idx = 0
    while len(chosen) < n and any(idx < len(p[1]) for p in pools):
        for d, items in pools:
            if idx >= len(items) or len(chosen) >= n:
                continue
            p = items[idx]
            try:
                sha = hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError:
                continue
            if sha in seen:
                continue
            seen.add(sha)
            chosen.append({"file": p.name, "path": str(p), "dir": str(d), "sha256": sha})
        idx += 1
    return chosen


def _sniff_mime(data: bytes) -> str:
    """极简 mime 嗅探（与 services/vision.py::sniff_mime 同义，避免 import 重依赖）。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def build_payload_data(path: str, raw: bool) -> tuple[str, str]:
    """返回 (base64, mime)。raw=True 时**原图直送**（复刻 services/vision.py 的现状：
    它不做降采样，直接把原图字节发出去 —— 这正是大图 26-36s 的来源）。"""
    if raw:
        data = Path(path).read_bytes()
        return base64.b64encode(data).decode(), _sniff_mime(data)
    data, mime = prepare_for_vision(path)
    return base64.b64encode(data).decode(), mime


# ---------------------------------------------------------------- 调用

def call_vision(*, api_base: str, api_key: str, model: str, cfg: dict[str, Any],
                b64: str, mime: str, timeout: float,
                max_retries: int = 3) -> dict[str, Any]:
    """一次识别调用。串行使用；429/5xx 指数退避。返回原始响应 + 计时 + usage。"""
    import httpx

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": cfg["system"]},
            {"role": "user", "content": [
                {"type": "text", "text": USER_TEXT},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ]},
        ],
        "max_tokens": cfg["max_tokens"],
        "temperature": cfg["temperature"],
    }
    if cfg["reasoning_effort"]:
        payload["reasoning_effort"] = cfg["reasoning_effort"]

    last: dict[str, Any] = {}
    for attempt in range(max_retries + 1):
        t0 = time.time()
        try:
            # trust_env=False 必需：开发机 no_proxy 含 [::1] → httpx InvalidURL。
            with httpx.Client(timeout=timeout, trust_env=False) as c:
                r = c.post(api_base.rstrip("/") + "/chat/completions",
                           headers={"Authorization": f"Bearer {api_key}"}, json=payload)
            dt = time.time() - t0
            if r.status_code == 429 or r.status_code >= 500:
                last = {"http": r.status_code, "elapsed": dt,
                        "error": f"HTTP {r.status_code}", "body": r.text[:200]}
                if attempt < max_retries:
                    time.sleep(5 * (2 ** attempt))
                    continue
                return last
            if r.status_code != 200:
                return {"http": r.status_code, "elapsed": dt,
                        "error": f"HTTP {r.status_code}", "body": r.text[:300]}
            d = r.json()
            ch = (d.get("choices") or [{}])[0]
            msg = ch.get("message") or {}
            usage = d.get("usage") or {}
            det = usage.get("completion_tokens_details") or {}
            return {
                "http": 200, "elapsed": dt, "error": "",
                "finish_reason": ch.get("finish_reason"),
                "content": str(msg.get("content") or ""),
                "reasoning": str(msg.get("reasoning") or msg.get("reasoning_content") or ""),
                "usage": usage,
                "completion_tokens": usage.get("completion_tokens"),
                "reasoning_tokens": det.get("reasoning_tokens"),
                "prompt_tokens": usage.get("prompt_tokens"),
            }
        except Exception as e:  # 超时/连接错误
            dt = time.time() - t0
            last = {"http": 0, "elapsed": dt, "error": f"{type(e).__name__}: {e}"}
            if attempt < max_retries and not isinstance(e, httpx.HTTPStatusError):
                time.sleep(2 * (attempt + 1))
                continue
            return last
    return last


# ---------------------------------------------------------------- 汇总

def _avg(xs: list[float]) -> float:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 1) if xs else 0.0


def _pct(k: int, n: int) -> float:
    return round(100.0 * k / n, 1) if n else 0.0


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return round(s[len(s) // 2], 1)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r.get("judge"):
            by.setdefault(r["config"], []).append(r)

    table: dict[str, Any] = {}
    for cid, rs in by.items():
        n = len(rs)
        j = [r["judge"] for r in rs]
        ok = [r for r in rs if r.get("http") == 200]
        trunc = sum(1 for r in ok if r.get("finish_reason") == "length")
        table[cid] = {
            "n": n,
            "http_ok": len(ok),
            "content_nonempty": _pct(sum(x["content_nonempty"] for x in j), n),
            "usable": _pct(sum(x["usable"] for x in j), n),
            "kw_ok": _pct(sum(x["kw_ok"] for x in j), n),
            "compliant": _pct(sum(x["compliant"] for x in j), n),
            "json_raw_ok": _pct(sum(x["json_raw_ok"] for x in j), n) if rs[0]["format"] == "b" else None,
            "marker_ok": _pct(sum(x["marker_ok"] for x in j), n) if rs[0]["format"] == "c" else None,
            "truncated": _pct(trunc, n),
            "avg_sec": _avg([r.get("elapsed") for r in ok]),
            "avg_completion_tokens": _avg([r.get("completion_tokens") for r in ok]),
            "avg_reasoning_tokens": _avg([r.get("reasoning_tokens") for r in ok]),
            "max_reasoning_tokens": max([r.get("reasoning_tokens") or 0 for r in ok] or [0]),
            "desc_len_p50": _median([x["desc_len"] for x in j if x["desc_len"]]),
            "fallback_usable": _pct(sum(x["fallback_usable"] for x in j), n),
            "pipeline_usable": _pct(
                sum(1 for x, r in zip(j, rs)
                    if x["usable"] or (not x["content_nonempty"] and x["fallback_usable"])), n),
            "errors": sum(1 for r in rs if r.get("http") != 200),
            "temperature": rs[0]["temperature"],
            "reasoning_effort": rs[0]["reasoning_effort"] or "(不发送)",
            "max_tokens": rs[0]["max_tokens"],
            "format": rs[0]["format"],
        }
    return table


HDR = ("config", "n", "content↑", "usable↑", "kw↑", "fmt合规", "截断", "pipeline↑",
       "秒", "comp_tok", "reason_tok", "max_reason")


def print_table(table: dict[str, Any], sort_key: str = "usable") -> None:
    rows = sorted(table.items(), key=lambda kv: (-kv[1][sort_key], kv[1]["avg_sec"]))
    print("\n" + "=" * 132)
    print("配置对比表（↑ = 越高越好；截断 = finish_reason=length 的比例）")
    print("=" * 132)
    print(f"{'配置':<22}{'n':>4}{'content非空':>11}{'描述可用':>9}{'关键词':>8}"
          f"{'格式合规':>9}{'截断':>7}{'管线可用':>9}{'秒':>7}{'comp_tok':>9}{'reason_tok':>11}{'max_reason':>11}")
    print("-" * 132)
    for cid, m in rows:
        print(f"{cid:<22}{m['n']:>4}{m['content_nonempty']:>10.1f}%{m['usable']:>8.1f}%"
              f"{m['kw_ok']:>7.1f}%{m['compliant']:>8.1f}%{m['truncated']:>6.1f}%"
              f"{m['pipeline_usable']:>8.1f}%{m['avg_sec']:>7.1f}"
              f"{m['avg_completion_tokens']:>9.0f}{m['avg_reasoning_tokens']:>11.0f}"
              f"{m['max_reasoning_tokens']:>11.0f}")
    print("=" * 132)


# ---------------------------------------------------------------- main

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="识图 LLM 参数标定台")
    ap.add_argument("--images-dir", action="append", default=[], type=Path,
                    help="图片目录，可给多次（按目录轮转抽样；跨目录 sha256 去重）")
    ap.add_argument("--n", type=int, default=40, help="抽样总数（默认 40）")
    ap.add_argument("--seed", type=int, default=20260913)
    ap.add_argument("--out", type=Path, default=Path("/tmp/vision_calib.json"))
    ap.add_argument("--configs", default="",
                    help="逗号分隔的配置 id（精确或前缀）；空 = 全部")
    ap.add_argument("--list-configs", action="store_true")
    ap.add_argument("--analyze-only", action="store_true", help="只汇总已有 jsonl，不调用")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--sleep", type=float, default=0.4, help="每次调用后的间隔秒（防 429）")
    ap.add_argument("--repeats", type=int, default=1,
                    help="每个 (配置,图片) 要跑够几次（>1 用于测**稳定性**；断点续跑按已有成功数补齐）")
    ap.add_argument("--raw-images", action="store_true",
                    help="原图直送（复刻 services/vision.py 现状：不降采样）。用于量化"
                         "「降采样 vs 原图」对 prompt_tokens 与耗时的影响")
    ap.add_argument("--tag", default="", help="给配置 id 加后缀，便于把 A/B 批次分开统计")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--images", default="", help="逗号分隔的显式图片路径（覆盖 --images-dir/--n）")
    ap.add_argument("--images-from", type=Path, default=None,
                    help="从文件读图片路径（每行一个），覆盖 --images-dir/--n")
    ap.add_argument("--exclude-files", type=Path, default=None,
                    help="从文件读**文件名**（每行一个），在抽样/列表里剔除（做不相交的确认集）")
    args = ap.parse_args(argv)

    if args.list_configs:
        for c in CONFIGS:
            print(f"{c['id']:<22} fmt={c['format']} temp={c['temperature']} "
                  f"effort={c['reasoning_effort'] or '(不发送)'} max_tokens={c['max_tokens']}")
        return 0

    jsonl = args.out.with_suffix(args.out.suffix + ".jsonl")

    # ---- 选配置
    cfgs = CONFIGS
    if args.configs:
        keys = [k.strip() for k in args.configs.split(",") if k.strip()]
        cfgs = [c for c in CONFIGS if any(c["id"] == k or c["id"].startswith(k) for k in keys)]
        if not cfgs:
            print(f"没有匹配的配置: {args.configs}", file=sys.stderr)
            return 2
    if args.tag:
        cfgs = [dict(c, id=f"{c['id']}+{args.tag}") for c in cfgs]

    # ---- 选图
    excl: set[str] = set()
    if args.exclude_files and args.exclude_files.exists():
        excl = {l.strip() for l in args.exclude_files.read_text(encoding="utf-8").splitlines() if l.strip()}

    if args.images_from:
        imgs = []
        for line in args.images_from.read_text(encoding="utf-8").splitlines():
            p = line.strip()
            if not p or Path(p).name in excl:
                continue
            imgs.append({"file": Path(p).name, "path": p, "dir": str(Path(p).parent), "sha256": ""})
    elif args.images:
        imgs = []
        for p in args.images.split(","):
            p = p.strip()
            if p and Path(p).name not in excl:
                imgs.append({"file": Path(p).name, "path": p, "dir": str(Path(p).parent), "sha256": ""})
    else:
        if not args.images_dir:
            print("需要 --images-dir 或 --images（或 --analyze-only）", file=sys.stderr)
            return 2
        imgs = pick_images(args.images_dir, args.n, args.seed, excl)

    # ---- 载入已完成记录（断点续跑）
    done: set[tuple[str, str]] = set()
    counts: dict[tuple[str, str], int] = {}
    rows: list[dict[str, Any]] = []
    if jsonl.exists():
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            rows.append(r)
            if r.get("http") == 200:
                done.add((r["config"], r["file"]))
                k = (r["config"], r["file"])
                counts[k] = counts.get(k, 0) + 1

    if not args.analyze_only:
        api_base = (os.environ.get("STICKER_VISION_API_BASE") or "").strip().strip('"').strip("'")
        api_key = (os.environ.get("STICKER_VISION_API_KEY") or "").strip().strip('"').strip("'")
        if not api_base or not api_key:
            print("缺少 STICKER_VISION_API_BASE / STICKER_VISION_API_KEY", file=sys.stderr)
            return 2
        todo = []
        for c in cfgs:
            for im in imgs:
                need = args.repeats - counts.get((c["id"], im["file"]), 0)
                todo.extend([(c, im)] * max(0, need))
        print(f"图片 {len(imgs)} 张 × 配置 {len(cfgs)} 个 × {args.repeats} 次 = "
              f"{len(cfgs) * len(imgs) * args.repeats} 次调用；待跑 {len(todo)}"
              f"（已完成 {len(cfgs) * len(imgs) * args.repeats - len(todo)}）", flush=True)

        prep: dict[str, tuple[str, str]] = {}
        t_start = time.time()
        for i, (cfg, im) in enumerate(todo, 1):
            if im["path"] not in prep:
                try:
                    prep[im["path"]] = build_payload_data(im["path"], args.raw_images)
                except Exception as e:
                    print(f"  ! 预处理失败 {im['file']}: {e}", file=sys.stderr)
                    prep[im["path"]] = ("", "")
            b64, mime = prep[im["path"]]
            if not b64:
                continue
            out = call_vision(api_base=api_base, api_key=api_key, model=args.model,
                              cfg=cfg, b64=b64, mime=mime, timeout=args.timeout)
            rec = {
                "ts": round(time.time(), 2), "config": cfg["id"], "file": im["file"],
                "dir": im.get("dir", ""), "format": cfg["format"],
                "temperature": cfg["temperature"], "reasoning_effort": cfg["reasoning_effort"],
                "max_tokens": cfg["max_tokens"],
                "raw_image": bool(args.raw_images),
                "payload_bytes": len(b64) * 3 // 4,
                **out,
            }
            rec["judge"] = judge(rec)
            rows.append(rec)
            with jsonl.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            flag = "OK " if rec["judge"]["usable"] else "空!"
            print(f"  [{i}/{len(todo)}] {cfg['id']:<20} {im['file'][:28]:<28} "
                  f"{flag} {out.get('elapsed', 0):5.1f}s fr={out.get('finish_reason')} "
                  f"ct={out.get('completion_tokens')} rt={out.get('reasoning_tokens')}", flush=True)
            if args.sleep:
                time.sleep(args.sleep)
        print(f"总耗时 {(time.time() - t_start) / 60:.1f} 分钟", flush=True)

    seen_ids = {r["config"] for r in rows}
    table = summarize([r for r in rows if r["config"] in seen_ids])
    print_table(table)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "model": args.model,
        "seed": args.seed,
        "images": [{"file": i["file"], "dir": i.get("dir", "")} for i in imgs],
        "configs": [{k: v for k, v in c.items() if k != "system"} for c in cfgs],
        "systems": {c["id"]: c["system"] for c in cfgs},
        "table": table,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果写入 {args.out}（明细 {jsonl}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
