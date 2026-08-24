"""ab_judge_style — re-score existing A/B results with the style checklist.

Usage (desktop):
  python3 tools/ab_judge_style.py --in <results.json> [--out <scored.json>]
Adds "style_judge" (1-5) per row using the distilled 正反清单.
"""
import argparse, io, json, os, re, sys, time, urllib.request

STYLE_SYS = (
    "你是「夕化炭」人设风格评审。风格清单："
    "正面=极简(能短就短)、梗驱动、熟人暴力萌(下锅/啄醒类)、无知时胡言乱语、短句质感；"
    "禁止=安抚腔正能量(元气满满/好好休息/做个好梦/摸摸头/保佑)、标准对仗解释句、"
    "建议腔(记得补觉/早点休息吧)、钨钼钨钼滥用、谐音问候误触发(非早/午/晚好整词不回谐音)、百科科普腔。"
    "只输出 1-5 整数：5=高度符合风格，1=严重违背。"
)


def get_creds(cfg="/opt/AstrBot/data/cmd_config.json"):
    d = json.load(io.open(cfg, encoding="utf-8-sig"))
    src = next(x for x in d["provider_sources"] if x.get("id") == "opencode-go")
    return src.get("api_base", ""), (src.get("key") or [""])[0]


def chat(api_base, key, prompt):
    payload = {"model": "deepseek-v4-flash", "temperature": 0, "max_tokens": 8,
               "messages": [{"role": "system", "content": STYLE_SYS},
                            {"role": "user", "content": prompt}]}
    url = api_base.rstrip("/") + "/chat/completions"
    for attempt in range(1, 4):
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json",
            "User-Agent": "ab-judge-style/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=100) as r:
                out = json.loads(r.read().decode("utf-8"))
            return (out.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        except Exception:
            time.sleep(5 * attempt)
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="fin", required=True)
    ap.add_argument("--out", dest="fout", required=True)
    args = ap.parse_args()
    rows = json.load(io.open(args.fin, encoding="utf-8"))
    api_base, key = get_creds()
    for r in rows:
        if not r.get("reply"):
            r["style_judge"] = 0
            continue
        prompt = f"用户：{r['probe_text']}\n回复：{r['reply']}\n风格分："
        raw = chat(api_base, key, prompt)
        m = re.search(r"[1-5]", raw)
        r["style_judge"] = int(m.group(0)) if m else 0
        print(f"{r['phase']} {r['probe']}#{r['rep']}: style={r['style_judge']} {r['reply'][:40]!r}")
    with io.open(args.fout, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print("written:", args.fout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
