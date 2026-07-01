"""§5 analyze_style.py — derive editable style profile from §4 outputs.

Reads:
  data_out/my_conversation_pairs.jsonl
  data_out/cleaned_my_messages.jsonl
  data_out/cleaned_group_messages_sample.jsonl

Writes (UTF-8, indent=2, ensure_ascii=False, atomic .tmp -> rename, .bak kept):
  data_out/my_style_profile.json
  data_out/my_lexicon.json
  data_out/my_emoticons.json
  data_out/my_message_stats.json
  data_out/my_hourly_distribution.json
  data_out/member_relations.json
  data_out/system_prompt_fragments.json

All files are meant to be human-edited downstream. Numbers are kept simple
(ints / one-decimal floats); structure is stable across reruns so that a
manual edit followed by a rerun shows a clean diff.

Run:
  python -m astrbot_plugin_persona_agent.tools.analyze_style
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

import jieba


# Lightweight Chinese stopword list (no external file).
STOPWORDS = set("""
的 了 是 我 你 他 她 它 们 也 都 就 又 还 在 和 与 跟 把 让 被 把 个 之 而 但 不 没 没有
吗 呢 啊 呀 哦 哈 嗯 喵 哼 啦 嘞 哒 噢 哇 呵 嗨 唉 哎 呃 嗯 哦
什么 怎么 为什么 这个 那个 一个 一下 这样 那样 这些 那些 一些 这么 那么
有 没 要 想 来 去 走 给 跟 用 做 看 听 说 知道 觉得 喜欢 可以 应该 不能 不会
很 太 真 真的 好 多 少 大 小 已经 还是 还有 而且 但是 然后 所以 因为 如果 不过
今天 明天 昨天 现在 之前 之后 一直 刚刚 突然 马上 已经
那 对 这 吧 到 从 上 下 里 中 里面 外面 时候 时 一 二 三 几 才 只 也是 已 又是
出来 进去 起来 下来 上来 一下 一会 一种 一样
""".split())

# Pre-add some tokens jieba splits oddly for chat slang.
for w in ["哈哈哈", "草", "卧槽", "嘿嘿", "嘤嘤嘤", "笑死", "绷不住"]:
    jieba.add_word(w)


# --- IO helpers ----------------------------------------------------------
DATA = Path("data_out")

def iter_jsonl(path: Path) -> Iterable[dict]:
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def atomic_write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    if path.exists():
        path.replace(path.with_suffix(path.suffix + ".bak"))
    tmp.replace(path)


# --- Text utilities ------------------------------------------------------
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # broad emoji
    "\U00002600-\U000027BF"   # symbols
    "\U0001F1E6-\U0001F1FF"   # flags
    "]"
)
KAOMOJI_RE = re.compile(r"(?:\(|（)[^()（）\s]{0,12}(?:\)|）)")
QQ_FACE_RE = re.compile(r"\[(表情|图片|视频|动画表情|emoji|gif)[^\]]*\]")
PAREN_NOTE_RE = re.compile(r"[（(](\S[^）)]{0,30})[）)]$")  # trailing (note)
URL_RE = re.compile(r"https?://\S+")


def char_buckets(n: int) -> str:
    if n <= 0: return "0"
    if n <= 2: return "1-2"
    if n <= 5: return "3-5"
    if n <= 10: return "6-10"
    if n <= 20: return "11-20"
    if n <= 50: return "21-50"
    return "50+"


def quantiles(xs: list[int], qs: list[float]) -> dict[str, float]:
    if not xs:
        return {f"p{int(q*100)}": 0.0 for q in qs}
    s = sorted(xs)
    out = {}
    for q in qs:
        k = int(round(q * (len(s) - 1)))
        out[f"p{int(q*100)}"] = float(s[k])
    return out


def tokenize_cn(text: str) -> list[str]:
    """jieba tokens, lowercase, strip pure punctuation/digits/url placeholders."""
    text = URL_RE.sub("", text)
    text = QQ_FACE_RE.sub("", text)
    out = []
    for tok in jieba.lcut(text, cut_all=False):
        tok = tok.strip()
        if not tok or len(tok) == 1 and not tok.isalnum() and not "\u4e00" <= tok <= "\u9fff":
            continue
        if tok in STOPWORDS:
            continue
        # Drop pure numbers / single ascii letters.
        if tok.isdigit():
            continue
        if len(tok) == 1 and not "\u4e00" <= tok <= "\u9fff":
            continue
        out.append(tok)
    return out


def char_ngrams(text: str, n_lo: int = 2, n_hi: int = 5) -> Iterable[str]:
    """Yield character n-grams from CJK runs only (skip ASCII to avoid url noise)."""
    runs = re.findall(r"[\u4e00-\u9fff]+", text)
    for run in runs:
        for n in range(n_lo, n_hi + 1):
            if len(run) < n:
                continue
            for i in range(len(run) - n + 1):
                yield run[i:i + n]


# --- Analyzers -----------------------------------------------------------
def analyze_messages(my_msgs: list[dict]):
    """Compute lexicon, emoticons, length stats, hourly distribution."""
    text_lens = []
    burst_lens = []
    word_counter: Counter[str] = Counter()
    ngram_counter: Counter[str] = Counter()
    emoji_counter: Counter[str] = Counter()
    kaomoji_counter: Counter[str] = Counter()
    qq_face_counter: Counter[str] = Counter()
    paren_note_counter: Counter[str] = Counter()
    hour_counter: Counter[int] = Counter()
    bucket_counter: Counter[str] = Counter()
    total_chars = 0

    for m in my_msgs:
        text = m["text"]
        n_burst = m.get("n_burst", 1)
        burst_lens.append(n_burst)
        # Split merged burst back into pieces for per-message length distribution.
        pieces = text.split("\n") if n_burst > 1 else [text]
        for p in pieces:
            n = len(p)
            text_lens.append(n)
            total_chars += n
            bucket_counter[char_buckets(n)] += 1
        for em in EMOJI_RE.findall(text):
            emoji_counter[em] += 1
        for k in KAOMOJI_RE.findall(text):
            kaomoji_counter[k] += 1
        for q in QQ_FACE_RE.findall(text):
            qq_face_counter[q] += 1
        for p in pieces:
            m_pn = PAREN_NOTE_RE.search(p)
            if m_pn:
                paren_note_counter[m_pn.group(0)] += 1
        for tok in tokenize_cn(text):
            word_counter[tok] += 1
        for ng in char_ngrams(text, 2, 5):
            ngram_counter[ng] += 1

        # Hourly (UTC; downstream can shift to local).
        try:
            dt = datetime.fromisoformat(m["ts"].replace("Z", "+00:00"))
            hour_counter[dt.hour] += 1
        except (KeyError, ValueError):
            pass

    return {
        "text_lens": text_lens,
        "burst_lens": burst_lens,
        "total_chars": total_chars,
        "word_counter": word_counter,
        "ngram_counter": ngram_counter,
        "emoji_counter": emoji_counter,
        "kaomoji_counter": kaomoji_counter,
        "qq_face_counter": qq_face_counter,
        "paren_note_counter": paren_note_counter,
        "hour_counter": hour_counter,
        "bucket_counter": bucket_counter,
    }


def filter_ngrams(ngram_counter: Counter[str], word_counter: Counter[str], top_k: int = 200):
    """Keep n-grams that look like distinctive phrases, not substrings of common words.

    Heuristic:
      - drop n-grams that are themselves common single-word tokens
      - drop pure-stopword n-grams
      - drop n-grams contained in / containing a longer n-gram with comparable count
        (substring subsumption — keeps "换老婆" and drops "换老" if 老婆 count is similar)
      - require min count 3
    """
    # First pass: collect candidates above threshold.
    candidates = []
    for ng, c in ngram_counter.most_common(top_k * 8):
        if c < 3:
            break
        if ng in word_counter and word_counter[ng] >= c * 0.8:
            continue
        if all(ch in STOPWORDS for ch in ng):
            continue
        candidates.append((ng, c))

    # Subsumption pass: if a longer candidate contains this one and has count >= 0.7 * this,
    # drop the shorter one (it's just a fragment).
    kept_set = set()
    sorted_by_len_desc = sorted(candidates, key=lambda x: (-len(x[0]), -x[1]))
    longer_kept: list[tuple[str, int]] = []
    for ng, c in sorted_by_len_desc:
        subsumed = False
        for ng2, c2 in longer_kept:
            if ng != ng2 and ng in ng2 and c2 >= c * 0.7:
                subsumed = True
                break
        if not subsumed:
            longer_kept.append((ng, c))
            kept_set.add(ng)

    # Re-sort by count descending and cap at top_k.
    kept = [{"phrase": ng, "count": c}
            for ng, c in sorted(candidates, key=lambda x: -x[1])
            if ng in kept_set][:top_k]
    return kept


def analyze_relations(pairs: list[dict], sample: list[dict], me_uin: str, top_k: int = 80):
    """Build a per-member relation summary."""
    cooccur: Counter[str] = Counter()  # who appeared in my pair contexts
    name_by_uin: dict[str, Counter[str]] = defaultdict(Counter)

    # Co-occurrence: any member appearing in my conversation contexts.
    for p in pairs:
        for m in p["context"]:
            uin = m.get("uin") or ""
            name = m.get("name") or ""
            if not uin or uin == me_uin:
                continue
            cooccur[uin] += 1
            if name:
                name_by_uin[uin][name] += 1

    # Total group activity per uin from the sample (already a 50k reservoir).
    group_activity: Counter[str] = Counter()
    for m in sample:
        uin = m.get("uin") or ""
        if uin and uin != me_uin:
            group_activity[uin] += 1
            name = m.get("name") or ""
            if name:
                name_by_uin[uin][name] += 1

    members = []
    for uin, c in cooccur.most_common(top_k):
        name_freq = name_by_uin[uin].most_common(3)
        members.append({
            "uin": uin,
            "alias": "",
            "other_names": [n for n, _ in name_freq[1:]] if len(name_freq) > 1 else [],
            "closeness": "known",
            "notes": "",
        })
    return {"members": members}


def build_prompt_fragments(profile: dict, lexicon: dict, hourly: dict, members: dict) -> dict:
    """Initial natural-language fragments for the LLM system prompt.

    No 'as an AI / 我是 AI' phrasing; first-person, concrete, terse.
    """
    avg_len = profile["length_stats"]["mean_chars"]
    p50 = profile["length_stats"]["quantiles"]["p50"]
    p90 = profile["length_stats"]["quantiles"]["p90"]
    burst_share = profile["burst_stats"]["share_of_pairs_with_burst"]
    top_words = [w["word"] for w in lexicon["top_words"][:15]]
    top_phrases = [p["phrase"] for p in lexicon["top_phrases"][:10]]

    # Peak hours
    counts = hourly["hourly_message_count"]
    sorted_hours = sorted(range(24), key=lambda h: counts[str(h)], reverse=True)
    peak_hours = sorted(sorted_hours[:6])
    quiet_hours = sorted(sorted_hours[-6:])

    close_members = [m.get("alias") or m.get("current_name", "") for m in (members.get("members") or members.get("top_members", []))[:8]]
    close_members = [n for n in close_members if n]

    return {
        "identity": (
            "你是这个 QQ 群里的一名普通成员，不是助手，不是 AI，不是机器人。"
            "回复时永远使用第一人称，不要解释自己的身份，不要说'作为'。"
        ),
        "tone": (
            f"你说话非常简短，平均一句 {avg_len:.0f} 字，"
            f"中位数 {int(p50)} 字，长度极少超过 {int(p90)} 字。"
            f"约 {int(burst_share * 100)}% 的回复你会拆成连发，用换行表达停顿。"
            "禁止整段长论述，禁止编号列表，禁止 markdown 加粗或标题。"
        ),
        "vocabulary": (
            "你常用的实词：" + "、".join(top_words[:12]) + "。"
            "你的口癖短语：" + "、".join(top_phrases[:8]) + "。"
            "保留这些表达，不要换成更书面的同义词。"
        ),
        "schedule": (
            f"你日常活跃在 UTC {peak_hours[0]:02d}-{peak_hours[-1]:02d} 时段，"
            f"在 UTC {quiet_hours[0]:02d}-{quiet_hours[-1]:02d} 时段几乎不发言。"
            "夜深或清晨触发主动发言时要更克制。"
        ),
        "relations": (
            "群里和你互动较多的成员："
            + "、".join(close_members[:8])
            + "。不要把陌生群友当熟人，称呼以他们的当前昵称为准。"
        ),
        "rules": [
            "不要主动报上自己的真实信息（学校/位置/手机/工作）。",
            "如果不知道，直接说不知道，不要编。",
            "不要把上下文里别人的话当成自己的发言。",
            "不要回复明显违法/违规话题；保持沉默或转移。",
        ],
        "_meta": {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "note": "This file is the LLM system prompt seed. Edit freely. "
                    "The plugin loads it via mtime watch; weekly drift tasks only suggest changes.",
        },
    }


# --- Main ----------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Build editable style profile from §4 outputs.")
    parser.add_argument("--data", default="data_out", help="data directory")
    parser.add_argument("--me-uin", default="337934842", help="style-source uin")
    parser.add_argument("--top-words", type=int, default=300)
    parser.add_argument("--top-phrases", type=int, default=200)
    parser.add_argument("--top-emojis", type=int, default=100)
    parser.add_argument("--top-members", type=int, default=80)
    args = parser.parse_args()

    data = Path(args.data)
    pairs_path = data / "my_conversation_pairs.jsonl"
    my_path = data / "cleaned_my_messages.jsonl"
    sample_path = data / "cleaned_group_messages_sample.jsonl"

    if not pairs_path.exists() or not my_path.exists():
        sys.stderr.write("Run build_dataset.py first.\n")
        return 2

    sys.stderr.write("loading...\n"); sys.stderr.flush()
    my_msgs = list(iter_jsonl(my_path))
    pairs = list(iter_jsonl(pairs_path))
    sample = list(iter_jsonl(sample_path))
    sys.stderr.write(f"  my_msgs={len(my_msgs):,}  pairs={len(pairs):,}  sample={len(sample):,}\n")

    sys.stderr.write("analyzing messages...\n"); sys.stderr.flush()
    a = analyze_messages(my_msgs)

    # --- length / burst stats ---
    text_lens = a["text_lens"]
    burst_lens = a["burst_lens"]
    n_pieces = len(text_lens)
    n_pairs = len(my_msgs)
    length_stats = {
        "n_pieces": n_pieces,
        "mean_chars": round(a["total_chars"] / max(n_pieces, 1), 2),
        "quantiles": quantiles(text_lens, [0.1, 0.25, 0.5, 0.75, 0.9, 0.99]),
        "bucket_histogram": dict(a["bucket_counter"].most_common()),
    }
    burst_stats = {
        "n_pairs": n_pairs,
        "n_with_burst": sum(1 for b in burst_lens if b > 1),
        "share_of_pairs_with_burst": round(sum(1 for b in burst_lens if b > 1) / max(n_pairs, 1), 3),
        "max_burst": max(burst_lens) if burst_lens else 0,
        "burst_quantiles": quantiles(burst_lens, [0.5, 0.75, 0.9, 0.99]),
    }

    # --- profile ---
    reply_share = round(
        sum(1 for p in pairs if p.get("trigger") == "reply") / max(n_pairs, 1), 3
    )
    active_days = len({m["ts"][:10] for m in my_msgs if m.get("ts")})
    profile = {
        "me_uin": args.me_uin,
        "n_pairs": n_pairs,
        "n_pieces": n_pieces,
        "active_days": active_days,
        "reply_trigger_share": reply_share,
        "length_stats": length_stats,
        "burst_stats": burst_stats,
    }
    atomic_write_json(data / "my_style_profile.json", profile)

    # --- lexicon ---
    word_counter = a["word_counter"]
    ngram_counter = a["ngram_counter"]
    lexicon = {
        "n_tokens_total": sum(word_counter.values()),
        "top_words": [{"word": w, "count": c}
                      for w, c in word_counter.most_common(args.top_words)],
        "top_phrases": filter_ngrams(ngram_counter, word_counter, args.top_phrases),
    }
    atomic_write_json(data / "my_lexicon.json", lexicon)

    # --- emoticons ---
    emoticons = {
        "emoji": [{"glyph": g, "count": c} for g, c in a["emoji_counter"].most_common(args.top_emojis)],
        "kaomoji": [{"glyph": g, "count": c} for g, c in a["kaomoji_counter"].most_common(args.top_emojis)],
        "qq_face_placeholder": [{"text": g, "count": c} for g, c in a["qq_face_counter"].most_common(args.top_emojis)],
        "trailing_paren_note": [{"text": g, "count": c} for g, c in a["paren_note_counter"].most_common(args.top_emojis)],
    }
    atomic_write_json(data / "my_emoticons.json", emoticons)

    # --- message stats ---
    msg_stats = {
        "length_stats": length_stats,
        "burst_stats": burst_stats,
        "pair_trigger": {
            "reply": sum(1 for p in pairs if p.get("trigger") == "reply"),
            "window": sum(1 for p in pairs if p.get("trigger") == "window"),
        },
    }
    atomic_write_json(data / "my_message_stats.json", msg_stats)

    # --- hourly distribution ---
    hour_counts = {str(h): int(a["hour_counter"].get(h, 0)) for h in range(24)}
    total_h = sum(hour_counts.values()) or 1
    # default daily budget (interjection upper cap, conservative)
    daily_budget = 24
    budget = {}
    for h in range(24):
        share = hour_counts[str(h)] / total_h
        budget[str(h)] = round(share * daily_budget, 2)
    hourly = {
        "tz_note": "Counts are UTC. The plugin should shift to its local TZ on load.",
        "hourly_message_count": hour_counts,
        "hourly_share": {h: round(hour_counts[h] / total_h, 4) for h in hour_counts},
        "default_daily_budget": daily_budget,
        "hourly_budget": budget,
    }
    atomic_write_json(data / "my_hourly_distribution.json", hourly)

    # --- relations ---
    relations = analyze_relations(pairs, sample, args.me_uin, args.top_members)
    rel_path = data / "member_relations.json"
    if rel_path.exists():
        suggested_path = data / "member_relations.suggested.json"
        atomic_write_json(suggested_path, relations)
        sys.stderr.write(f"member_relations.json exists; wrote suggestion to {suggested_path.name} (review + merge manually).\n")
    else:
        atomic_write_json(rel_path, relations)

    # --- system prompt fragments ---
    fragments = build_prompt_fragments(profile, lexicon, hourly, relations)
    frag_path = data / "system_prompt_fragments.json"
    if frag_path.exists():
        suggested_path = data / "system_prompt_fragments.suggested.json"
        atomic_write_json(suggested_path, fragments)
        sys.stderr.write(f"system_prompt_fragments.json exists; wrote suggestion to {suggested_path.name} (review + merge manually).\n")
    else:
        atomic_write_json(frag_path, fragments)

    sys.stderr.write("wrote style files under data_out/.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
