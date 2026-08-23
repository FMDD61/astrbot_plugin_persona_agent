"""select_examples — G14: build a curated candidate pool from
my_conversation_pairs.jsonl (style-source real pairs).

Pipeline:
  1. rule filter (length / lines / markers / fixed-bot phrases)
  2. topic bucketing (8 buckets)
  3. offline rank (deterministic, length favouring)
  4. optional --llm-score: persona-consistency 1-5 via the chat gateway
     (reads api_base/key from AstrBot cmd_config.json when run on the
     desktop; falls back to env OPENCODE_KEY/OPENCODE_API_BASE otherwise)

Outputs ONLY candidate files (default data_out/example_dialogs_candidates.*).
Never touches example_dialogs.json (human-reviewed merge happens later).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict

BOT_PHRASES = {"投食", "打工", "娶", "今日老婆", "投喂", "娶群友"}

BUCKETS = [
    ("问好", re.compile(r"早|午|晚|安|好|枣商|汪尚|ciallo", re.I)),
    ("贴贴", re.compile(r"贴|rua|抱|亲|摸|啃|蹭|可爱|软软", re.I)),
    ("吃", re.compile(r"吃|饿|饭|甜|买|零食|麻薯|蛋糕", re.I)),
    ("睡", re.compile(r"睡|困|晚安|夜|醒", re.I)),
    ("游戏", re.compile(r"游戏|玩|打|抽|卡|关|boss|角色|副本", re.I)),
    ("上班", re.compile(r"班|领导|工作|加班|累|钱", re.I)),
    ("吐槽", re.compile(r"烦|气|讨厌|难|无语|离谱|服了", re.I)),
    ("日常", re.compile(r".*")),
]


def rule_filter(rec: dict) -> bool:
    reply = (rec.get("reply_text") or "").strip()
    ctx = rec.get("context") or []
    if not reply or not ctx:
        return False
    n = len(reply)
    if n < 5 or n > 50:
        return False
    lines = [ln.strip() for ln in reply.splitlines() if ln.strip()]
    if not lines or len(lines) > 2:
        return False
    if "[" in reply or "@" in reply or "http" in reply.lower():
        return False
    if re.search(r"[\x00-\x1f]", reply):
        return False
    if len(lines) >= 2 and all(ln in BOT_PHRASES for ln in lines):
        return False
    if len(lines) == 1 and lines[0] in BOT_PHRASES:
        return False
    ctx = [m for m in ctx if (m.get("text") or "").strip()][-3:]
    if not ctx:
        return False
    if sum(len((m.get("text") or "").strip()) for m in ctx) > 220:
        return False
    return True


def bucket_of(reply: str) -> str:
    for name, rx in BUCKETS:
        if name == "日常":
            return name
        if rx.search(reply):
            return name
    return "日常"


def offline_score(reply: str) -> float:
    n = len(reply)
    s = 10.0 if 8 <= n <= 30 else 5.0
    if reply.count("\n") == 0:
        s += 3.0
    if "！" in reply or "~" in reply:
        s += 1.0
    return s


def load_pairs(path: str):
    with io.open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def llm_score_remote(reply: str, ctx_lines: list[str], api_base: str, key: str, model: str) -> int:
    prompt_ctx = "\n".join(ctx_lines)
    sys_p = (
        "你是角色人设评估员。角色「夕化炭」：QQ 群温柔姐姐型成员，短句、口语化、萌系、"
        "低频口癖（啃啃/呜嘿等）、不写长段、不打擦边、不参与争执。"
        "根据群内真实对话对，判断该回复是否符合人设。只输出一个 1 到 5 的整数。"
    )
    user_p = f"群内对话：\n{prompt_ctx}\n\n她的回复：{reply}\n评分："
    url = api_base.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": sys_p},
                     {"role": "user", "content": user_p}],
        "max_tokens": 4,
        "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "User-Agent": "select-examples/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read().decode("utf-8"))
    txt = (out.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    m = re.search(r"[1-5]", str(txt))
    return int(m.group(0)) if m else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", required=True, help="my_conversation_pairs.jsonl")
    ap.add_argument("--out", default="data_out/example_dialogs_candidates.json",
                    help="candidate json output")
    ap.add_argument("--per-bucket", type=int, default=4, help="offline winners per bucket")
    ap.add_argument("--llm-cap", type=int, default=48, help="max llm-scored candidates")
    ap.add_argument("--llm-score", action="store_true", help="run LLM persona scoring")
    ap.add_argument("--model", default="deepseek-v4-flash", help="scoring model")
    args = ap.parse_args()

    rows = list(load_pairs(args.data))
    print(f"total pairs: {len(rows)}")

    passed = [r for r in rows if rule_filter(r)]
    print(f"rule-filter passed: {len(passed)}")

    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in passed:
        reply = (r.get("reply_text") or "").strip()
        buckets[bucket_of(reply)].append(r)
    for name, items in sorted(buckets.items()):
        print(f"  bucket {name}: {len(items)}")

    winners: list[dict] = []
    for name, items in sorted(buckets.items()):
        items.sort(key=lambda r: (-offline_score((r.get("reply_text") or "").strip()),
                                  (r.get("reply_ts") or "")))
        for r in items[:args.per_bucket]:
            winners.append({
                "topic": name,
                "offline_score": offline_score((r.get("reply_text") or "").strip()),
                "source_ts": r.get("reply_ts", ""),
                "reply": (r.get("reply_text") or "").strip(),
                "context": [
                    {"name": (m.get("name") or "").strip(),
                     "text": (m.get("text") or "").strip()}
                    for m in (r.get("context") or [])[-3:]
                ],
                "llm_score": None,
            })
    print(f"offline winners: {len(winners)}")

    if args.llm_score:
        api_base = os.environ.get("OPENCODE_API_BASE", "")
        key = os.environ.get("OPENCODE_KEY", "")
        if not api_base or not key:
            cfg_path = "/opt/AstrBot/data/cmd_config.json"
            try:
                cfg = json.load(io.open(cfg_path, "r", encoding="utf-8-sig"))
                src = next(x for x in cfg.get("provider_sources", []) if x.get("id") == "opencode-go")
                api_base, key = src.get("api_base", ""), (src.get("key") or [""])[0]
            except Exception as e:
                print(f"cannot resolve gateway creds: {e}; set OPENCODE_API_BASE/OPENCODE_KEY")
                return 2
        # interleave buckets so the LLM cap covers all topics
        by_bucket: dict[str, list[dict]] = defaultdict(list)
        for w in winners:
            by_bucket[w["topic"]].append(w)
        ordered = []
        while any(by_bucket.values()):
            for name in sorted(by_bucket):
                if by_bucket[name]:
                    ordered.append(by_bucket[name].pop(0))
        for w in ordered[:args.llm_cap]:
            ctx_lines = [f"{m['name']}: {m['text']}" for m in w["context"]]
            w["llm_score"] = llm_score_remote(w["reply"], ctx_lines, api_base, key, args.model)
            print(f"  [{w['topic']}] llm={w['llm_score']} {w['reply'][:30]!r}")
        kept = [w for w in ordered[:args.llm_cap] if (w.get("llm_score") or 0) >= 4]
        print(f"llm kept (>=4): {len(kept)}")
        winners = kept

    out_path = args.out
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with io.open(out_path, "w", encoding="utf-8") as f:
        json.dump(winners, f, ensure_ascii=False, indent=2)
    print(f"candidates written: {out_path} ({len(winners)})")

    md_path = out_path.rsplit(".", 1)[0] + ".md"
    with io.open(md_path, "w", encoding="utf-8") as f:
        f.write("# 典型对话候选池（G14 抽样）\n\n")
        for w in winners:
            f.write(f"## [{w['topic']}] llm={w.get('llm_score', '-')} 源时间={w['source_ts']}\n")
            for m in w["context"]:
                f.write(f"- {m['name']}: {m['text']}\n")
            f.write(f"> {w['reply']}\n\n")
    print(f"markdown written: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())