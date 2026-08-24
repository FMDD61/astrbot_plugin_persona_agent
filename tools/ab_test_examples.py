"""ab_test_examples — offline A/B harness for the G14 example-dialog injection.

Replays the plugin's exact request construction WITHOUT QQ:
  system_prompt (deployed fragments) + user probe (name=焦糖)
  + [ON/OFF] examples block (deployed example_dialogs.json + guard rules)
  + current-speaker line   (mirrors main.py ordering: session/examples/speaker)
then calls the same opencode gateway (deepseek-v4-flash, temperature=0.8)
and postprocesses like the plugin (text_style.postprocess).

Run on the desktop (reads api key from AstrBot cmd_config.json; needs the
plugin repo pulled to the running commit).

Usage:
  python3 tools/ab_test_examples.py \
      --data-dir /opt/AstrBot/data/plugin_data/astrbot_plugin_persona_agent \
      --out /opt/AstrBot/data/plugin_data/astrbot_plugin_persona_agent/ab_test_results.json \
      [--reps 2] [--judge] [--hour 3]
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import re
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROBES = [
    ("P1", "早上好", "谐音问候"),
    ("P2", "原来签到和等级是分开算的啊", "肯定/恍然大悟"),
    ("P3", "又加了一晚上班，烦死了", "共情"),
    ("P4", "我回来啦", "贴贴"),
    ("P5", "中午吃什么好", "短句/梗"),
    ("P6", "你鸡都没醒你就醒了", "反问怼人"),
    ("P7", "你知道为什么猫会踩奶吗", "无知应对"),
    ("P8", "我和花鱼吵架你帮谁！", "两难"),
    ("P9", "睡了睡了", "深夜收尾"),
    ("P10", "抽卡又歪了，呜呜", "安慰/彩蛋"),
]
EGGCORN = re.compile(r"枣商蚝|种物蚝|霞梧蚝|汪尚蚝")
MARKER = re.compile(r"\[图片|\[ComponentType")
KOUPIS = ("钨钼钨钼", "搓搓", "啃啃", "呜嘿", "bakabaka", "捏猫猫的", "嗷呜")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def get_creds(cmd_config="/opt/AstrBot/data/cmd_config.json"):
    d = json.load(io.open(cmd_config, encoding="utf-8-sig"))
    src = next(x for x in d["provider_sources"] if x.get("id") == "opencode-go")
    return src.get("api_base", ""), (src.get("key") or [""])[0]


def chat(api_base, key, model, system_prompt, msgs, temperature=0.8, max_tokens=256):
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}] + msgs,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    url = api_base.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "User-Agent": "ab-test-examples/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        out = json.loads(r.read().decode("utf-8"))
    return (out.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""


def speaker_line(uin, alias, is_src):
    return (f"【当前说话人】与本消息对应的发话人：QQ {uin}，群内别名「{alias}」"
            f"{'（风格源 QQ）' if is_src else ''}。"
            "请始终用该别名称呼 TA；无法确认时不要臆造其他群友的别名。")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--judge", action="store_true", help="LLM relevance scoring")
    ap.add_argument("--hour", type=int, default=3)
    ap.add_argument("--model", default="deepseek-v4-flash")
    ap.add_argument("--temperature", type=float, default=0.8)
    args = ap.parse_args()

    ts_mod = load_module("text_style_mod", os.path.join(REPO, "services/text_style.py"))
    ex_mod = load_module("examples_mod", os.path.join(REPO, "services/examples.py"))
    sp_mod = load_module("style_profile_mod", os.path.join(REPO, "services/style_profile.py"))

    sp = sp_mod.StyleProfile(args.data_dir)
    sys_prompt = sp.system_prompt(local_hour=args.hour)
    ex_path = os.path.join(args.data_dir, "example_dialogs.json")
    api_base, key = get_creds()

    results = []
    for phase in ("OFF", "ON"):
        for rep in range(args.reps):
            for pid, text, topic in PROBES:
                msgs = [{"role": "user", "content": text, "name": "焦糖"}]
                if phase == "ON":
                    block, _ = ex_mod.load_examples_block(ex_path)
                    if block:
                        msgs.append({"role": "system", "content": block})
                msgs.append({"role": "system", "content": speaker_line("337934842", "焦糖", True)})
                raw = chat(api_base, key, args.model, sys_prompt, msgs,
                           temperature=args.temperature)
                reply = ts_mod.postprocess(raw)
                results.append({
                    "phase": phase, "rep": rep, "probe": pid, "topic": topic,
                    "probe_text": text, "reply": reply, "chars": len(reply),
                })
                print(f"[{phase} r{rep}] {pid} {topic}: {reply[:60]!r}")

    if args.judge:
        judge_sys = ("你是对话评测员。只输出一个 1-5 整数：回复是否贴合用户消息的话题与情绪"
                     "（5=完全贴合并自然，1=跑题或奇怪）。")
        for r in results:
            prompt = f"用户：{r['probe_text']}\n回复：{r['reply']}\n得分："
            try:
                raw = chat(api_base, key, args.model, judge_sys,
                           [{"role": "user", "content": prompt}], temperature=0)
                m = re.search(r"[1-5]", raw)
                r["judge"] = int(m.group(0)) if m else 0
            except Exception:
                r["judge"] = 0
            print(f"  judge {r['phase']} {r['probe']}: {r['judge']}")

    for r in results:
        r["eggcorn"] = bool(EGGCORN.search(r["reply"]))
        r["marker_leak"] = bool(MARKER.search(r["reply"]))
        r["tungsten"] = r["reply"].count("钨钼钨钼")
        r["cuocuo"] = r["reply"].count("搓搓")
    for r in results:
        r["koupi"] = {k: r["reply"].count(k) for k in KOUPIS}

    with io.open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    # summary
    from collections import defaultdict
    agg = defaultdict(lambda: defaultdict(list))
    for r in results:
        agg[r["phase"]][r["probe"]].append(r)
    lines = ["# G14 A/B 离线重测摘要\n"]
    for phase in ("OFF", "ON"):
        rs = [r for r in results if r["phase"] == phase]
        judge = [r.get("judge", 0) for r in rs if r.get("judge")]
        tungsten = sum(r["tungsten"] for r in rs)
        egg_hits = sum(1 for r in rs if r["eggcorn"])
        lines.append(f"## {phase}  (n={len(rs)} 平均judge={sum(judge)/len(judge) if judge else '-'} "
                     f"钨钼总量={tungsten} 谐音出现={egg_hits})")
        for r in rs:
            lines.append(f"- [{r['probe']}/{r['topic']}#{r['rep']}] {r['reply']}  "
                         f"(judge={r.get('judge','-')})")
    md = "\n".join(lines)
    md_path = args.out.rsplit(".", 1)[0] + ".md"
    with io.open(md_path, "w", encoding="utf-8") as f:
        f.write(md + "\n")
    print("\n" + md)
    print("written:", args.out, md_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())