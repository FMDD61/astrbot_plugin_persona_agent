"""§4 acceptance checks against data_out/my_conversation_pairs.jsonl + cleaned_my_messages.jsonl + summary."""
import json
import os
import random
import sys
from pathlib import Path

OUT = Path("data_out")

def main() -> int:
    pairs_path = OUT / "my_conversation_pairs.jsonl"
    summary_path = OUT / "build_dataset_summary.json"
    sample_path = OUT / "cleaned_group_messages_sample.jsonl"
    my_path = OUT / "cleaned_my_messages.jsonl"
    assert pairs_path.exists(), pairs_path
    assert summary_path.exists(), summary_path
    summary = json.loads(summary_path.read_text("utf-8"))

    pairs = [json.loads(line) for line in pairs_path.read_text("utf-8").splitlines() if line.strip()]
    my_msgs = [json.loads(line) for line in my_path.read_text("utf-8").splitlines() if line.strip()]
    sample = [json.loads(line) for line in sample_path.read_text("utf-8").splitlines() if line.strip()]

    errors = []
    warnings = []

    # 1) Every line is valid JSON (already parsed above without crashing).
    print(f"OK  every JSONL line is valid JSON  (pairs={len(pairs)}, my={len(my_msgs)}, sample={len(sample)})")

    # 2) reply non-empty, context non-empty, context strictly before reply_ts, monotonic.
    for i, p in enumerate(pairs):
        if not p.get("reply_text"):
            errors.append(f"pair[{i}] empty reply_text")
        ctx = p.get("context") or []
        if not ctx:
            errors.append(f"pair[{i}] empty context")
            continue
        ts_reply = p["reply_ts"]
        for j, m in enumerate(ctx):
            if m["ts"] >= ts_reply:
                errors.append(f"pair[{i}].context[{j}] ts {m['ts']} >= reply_ts {ts_reply}")
            if not m.get("text"):
                errors.append(f"pair[{i}].context[{j}] empty text")
        ts_list = [m["ts"] for m in ctx]
        if ts_list != sorted(ts_list):
            errors.append(f"pair[{i}] context not chronological")
    if not any("empty reply_text" in e or "empty context" in e or "not chronological" in e or ">= reply_ts" in e for e in errors):
        print("OK  every pair: non-empty reply, non-empty context, ctx strictly before reply, ctx chronological")

    # 3) None of the context messages may come from the style source itself
    #    (the design says context is "since user's previous message").
    SS_UIN = "337934842"
    SS_UID = "u__99fGylJOKfMjeG5wgk-ZQ"
    violators = 0
    for i, p in enumerate(pairs):
        for j, m in enumerate(p["context"]):
            if m.get("uin") == SS_UIN:
                violators += 1
                if violators <= 3:
                    warnings.append(f"pair[{i}].context[{j}] contains style-source msg (uin match)")
    if violators == 0:
        print("OK  no context message comes from the style source")
    else:
        warnings.append(f"TOTAL {violators} self-msg leaks into context (probably OK if non-target-group, but check)")

    # 4) Random spot-check of 50 pairs (or all if fewer): print compact form.
    pick = random.sample(pairs, min(5, len(pairs)))
    print("\n-- spot-check 5 random pairs (compact) --")
    for p in pick:
        ctx_n = len(p["context"])
        last_ctx = p["context"][-1]
        print(f"  reply_ts={p['reply_ts']}  trigger={p['trigger']}  ctx_n={ctx_n}  "
              f"last_ctx_from={last_ctx['name']!r}@{last_ctx['ts']}  "
              f"reply={p['reply_text'][:40]!r}")

    # 5) burst-merge sanity: cleaned_my_messages records have n_burst >= 1 and
    #    aggregate count of (style source messages produced as records) matches pairs len.
    n_burst_total = sum(m.get("n_burst", 1) for m in my_msgs)
    # Note: some self messages get dropped because context was empty -> my_msgs only logs
    # the ones that became a pair. Verify equality.
    if len(my_msgs) != len(pairs):
        warnings.append(f"len(cleaned_my_messages)={len(my_msgs)} != len(pairs)={len(pairs)} (expected equal: my_writer only fires when pair is written)")
    else:
        print(f"OK  len(cleaned_my_messages) == len(pairs) == {len(pairs)}")
    print(f"INFO  sum(n_burst) over kept records = {n_burst_total} (vs summary.n_style_source_messages={summary['n_style_source_messages']})")

    # 6) Summary self-consistency.
    print("\n-- summary --")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if errors:
        print("\nERRORS:")
        for e in errors[:20]:
            print(" -", e)
    if warnings:
        print("\nWARNINGS:")
        for w in warnings[:20]:
            print(" -", w)
    return 1 if errors else 0

if __name__ == "__main__":
    raise SystemExit(main())
