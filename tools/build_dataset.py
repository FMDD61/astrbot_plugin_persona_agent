"""§4 build_dataset.py — extract style-source conversation pairs from merge.json.

Streams merge.json (UTF-8, ~1.8 GB) via ijson and produces three JSONL artifacts:

  data_out/my_conversation_pairs.jsonl
      {"reply_ts", "reply_text", "context": [{"ts","name","uin","text"}, ...],
       "trigger": "reply"|"window", "reply_to_name"|null}
  data_out/cleaned_my_messages.jsonl
      Style-source's own valid text messages, deduped of burst merges.
  data_out/cleaned_group_messages_sample.jsonl
      A reservoir sample of all valid group messages (cap controllable).

Design (locked in AGENTS.md; do not re-debate):
  - Target group:  receiver.uid == "881438753" AND receiver.type == "group"
  - Style source:  sender.uin == "337934842" OR sender.uid == "u__99fGylJOKfMjeG5wgk-ZQ"
  - Message text:  content.text first; fallback to joining rawMessage.elements[*].textElement.content
  - Filters:       messageType == 2; not isSystemMessage; not isRecalled; text non-empty
  - Burst merge:   consecutive messages by the same sender within 60s -> joined with "\n"
  - Reply detect:  style-source text startswith "@" AND content.mentions non-empty
                    -> take mentions[0].name, search buffer backwards for the most recent
                       message whose sender.name matches; if found, context = messages
                       from that anchor (inclusive) up to (excluding) current reply.
                    -> fallback: since-last-self window, capped at N=30 msgs OR T=30 min.
  - Streaming:     never load entire file; ring buffer capped at MAX_BUFFER.
  - Output:        UTF-8, ensure_ascii=False, atomic .tmp -> rename.

Run:
  python -m astrbot_plugin_persona_agent.tools.build_dataset --limit 50000
  python -m astrbot_plugin_persona_agent.tools.build_dataset            # full
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

import ijson


# --- Locked constants ----------------------------------------------------
TARGET_GROUP_UID = "881438753"
STYLE_SOURCE_UIN = "337934842"
STYLE_SOURCE_UID = "u__99fGylJOKfMjeG5wgk-ZQ"
BURST_MERGE_SECONDS = 60
CONTEXT_MAX_MSGS = 30
CONTEXT_MAX_MINUTES = 30
RING_BUFFER_SIZE = 300        # > CONTEXT_MAX_MSGS, gives reply lookback room
GROUP_SAMPLE_CAP = 50_000     # reservoir size for sample export
PROGRESS_EVERY = 50_000


# --- Data structures -----------------------------------------------------
@dataclass
class Msg:
    ts: float            # epoch seconds
    ts_iso: str          # original iso string
    uid: str
    uin: str
    name: str
    text: str
    message_id: str

    def to_context(self) -> dict:
        return {"ts": self.ts_iso, "name": self.name, "uin": self.uin, "text": self.text}


@dataclass
class PairWriter:
    out_path: Path
    fh: object = None
    n: int = 0

    def open(self) -> None:
        self.tmp_path = self.out_path.with_suffix(self.out_path.suffix + ".tmp")
        self.fh = open(self.tmp_path, "w", encoding="utf-8")

    def write(self, obj: dict) -> None:
        self.fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.n += 1

    def close(self) -> None:
        if self.fh:
            self.fh.flush()
            os.fsync(self.fh.fileno())
            self.fh.close()
            self.fh = None
            if self.out_path.exists():
                self.out_path.replace(self.out_path.with_suffix(self.out_path.suffix + ".bak"))
            self.tmp_path.replace(self.out_path)


# --- Parsing helpers -----------------------------------------------------
def parse_ts(ts_str: str) -> Optional[float]:
    if not ts_str:
        return None
    try:
        # "2025-01-01T07:42:13.000Z"
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def extract_text(msg: dict) -> str:
    """Prefer content.text; fall back to joined rawMessage.elements[].textElement.content."""
    text = (msg.get("content") or {}).get("text") or ""
    if text:
        return text
    parts = []
    for el in (msg.get("rawMessage") or {}).get("elements") or []:
        te = el.get("textElement") or {}
        c = te.get("content")
        if c:
            parts.append(c)
    return "".join(parts)


def is_style_source(msg: dict) -> bool:
    sender = msg.get("sender") or {}
    return sender.get("uin") == STYLE_SOURCE_UIN or sender.get("uid") == STYLE_SOURCE_UID


def is_target_group(msg: dict) -> bool:
    r = msg.get("receiver") or {}
    return r.get("uid") == TARGET_GROUP_UID and r.get("type") == "group"


def is_reply_text(text: str, mentions: list) -> bool:
    return bool(text) and text.startswith("@") and bool(mentions)


def first_mention_name(mentions: list) -> Optional[str]:
    for m in mentions:
        n = (m or {}).get("name")
        if n:
            return n
    return None


# --- Core extractor ------------------------------------------------------
class Extractor:
    def __init__(self, limit: int, out_dir: Path) -> None:
        self.limit = limit
        self.out_dir = out_dir

        self.buffer: deque[Msg] = deque(maxlen=RING_BUFFER_SIZE)
        # Pending burst-merge state for the style source:
        self.pending: list[Msg] = []

        # Stats
        self.n_seen = 0
        self.n_target_group = 0
        self.n_text = 0
        self.n_self = 0
        self.n_pairs = 0
        self.n_pairs_reply = 0
        self.n_pairs_window = 0
        self.n_self_burst_merges = 0
        self.t0 = time.time()

        # Reservoir sample state
        self.group_sample: list[dict] = []
        self.group_sample_seen = 0

        self.pair_writer = PairWriter(out_dir / "my_conversation_pairs.jsonl")
        self.my_writer = PairWriter(out_dir / "cleaned_my_messages.jsonl")
        self.sample_writer = PairWriter(out_dir / "cleaned_group_messages_sample.jsonl")

    # ----- buffer / pending helpers -----
    def _flush_pending_pair(self) -> None:
        """Emit a conversation pair from accumulated self-burst messages."""
        if not self.pending:
            return
        anchor = self.pending[0]
        reply_text = "\n".join(m.text for m in self.pending)
        reply_ts = anchor.ts_iso

        # Try reply-based context first using the FIRST message's mention.
        # (We re-read content.mentions via stored marker on Msg; but Msg only carries
        # text. Re-detect from text prefix: cheap heuristic that matches our writer.)
        anchor_mention_name = getattr(anchor, "_mention_name", None)
        ctx_msgs, trigger, reply_to = self._build_context(anchor, anchor_mention_name)

        # Drop pairs with empty context (spec §4 requires non-empty context).
        if not ctx_msgs:
            self.pending.clear()
            return

        self.pair_writer.write({
            "reply_ts": reply_ts,
            "reply_text": reply_text,
            "context": [m.to_context() for m in ctx_msgs],
            "trigger": trigger,
            "reply_to_name": reply_to,
        })
        self.n_pairs += 1
        if trigger == "reply":
            self.n_pairs_reply += 1
        else:
            self.n_pairs_window += 1

        # Burst-merge bookkeeping for cleaned_my_messages: dump merged text.
        self.my_writer.write({
            "ts": reply_ts,
            "text": reply_text,
            "n_burst": len(self.pending),
        })
        if len(self.pending) > 1:
            self.n_self_burst_merges += 1

        self.pending.clear()

    def _build_context(self, anchor: Msg, mention_name: Optional[str]):
        """Return (context_msgs, trigger, reply_to_name).

        Context must consist of messages strictly older than the burst anchor.
        If mention_name resolves to a recent buffer message by sender.name, take
        all buffer messages from that point up to (but excluding) anchor.
        Otherwise apply since-last-self sliding window capped at
        CONTEXT_MAX_MSGS and CONTEXT_MAX_MINUTES.
        """
        # Hard cut: only keep buffer messages strictly before the burst anchor.
        # Otherwise non-self messages that arrived during the burst window leak in.
        buf = [m for m in self.buffer if m.ts < anchor.ts]

        if mention_name:
            # Search backwards for most recent non-self message with matching name.
            for i in range(len(buf) - 1, -1, -1):
                m = buf[i]
                if m.uin == STYLE_SOURCE_UIN or m.uid == STYLE_SOURCE_UID:
                    continue
                if m.name == mention_name:
                    ctx = buf[i:]
                    # Cap context length even for reply trigger (sanity).
                    if len(ctx) > CONTEXT_MAX_MSGS * 2:
                        ctx = ctx[-CONTEXT_MAX_MSGS * 2:]
                    return ctx, "reply", mention_name
            # mention name unresolved -> fall through to window

        # since-last-self window
        cutoff_ts = anchor.ts - CONTEXT_MAX_MINUTES * 60
        ctx = []
        for m in reversed(buf):
            if m.uin == STYLE_SOURCE_UIN or m.uid == STYLE_SOURCE_UID:
                break  # earlier self message terminates the window
            if m.ts < cutoff_ts:
                break
            ctx.append(m)
            if len(ctx) >= CONTEXT_MAX_MSGS:
                break
        ctx.reverse()
        return ctx, "window", None

    # ----- main feed -----
    def feed(self, msg: dict) -> None:
        self.n_seen += 1
        if self.n_seen % PROGRESS_EVERY == 0:
            self._progress()

        if not is_target_group(msg):
            return
        self.n_target_group += 1

        if msg.get("messageType") != 2:
            return
        if msg.get("isSystemMessage") or msg.get("isRecalled"):
            return
        text = extract_text(msg).strip()
        if not text:
            return
        self.n_text += 1

        sender = msg.get("sender") or {}
        ts = parse_ts(msg.get("timestamp") or "")
        if ts is None:
            return

        m = Msg(
            ts=ts,
            ts_iso=msg.get("timestamp") or "",
            uid=sender.get("uid") or "",
            uin=sender.get("uin") or "",
            name=sender.get("name") or "",
            text=text,
            message_id=msg.get("messageId") or "",
        )

        # Reservoir sample (Algorithm R) on all valid target-group text msgs.
        self.group_sample_seen += 1
        if len(self.group_sample) < GROUP_SAMPLE_CAP:
            self.group_sample.append(m.to_context())
        else:
            j = random.randint(0, self.group_sample_seen - 1)
            if j < GROUP_SAMPLE_CAP:
                self.group_sample[j] = m.to_context()

        if is_style_source(msg):
            self.n_self += 1
            mentions = (msg.get("content") or {}).get("mentions") or []
            if is_reply_text(text, mentions):
                m._mention_name = first_mention_name(mentions)  # type: ignore[attr-defined]
            else:
                m._mention_name = None  # type: ignore[attr-defined]

            # Burst-merge: if last pending is also self and gap <= 60s, accumulate.
            if self.pending and (m.ts - self.pending[-1].ts) <= BURST_MERGE_SECONDS:
                self.pending.append(m)
            else:
                # Flush any prior pending burst (different burst), then start fresh.
                if self.pending:
                    self._flush_pending_pair()
                self.pending.append(m)
            # NOTE: do NOT push self messages into self.buffer until we flush,
            # otherwise the next pair's context would include our own current reply.
        else:
            # On any non-self message we know the current self-burst (if any) is
            # over only when gap exceeds threshold. Cheap rule: flush whenever a
            # non-self message arrives more than BURST_MERGE_SECONDS after the
            # last pending self message.
            if self.pending and (m.ts - self.pending[-1].ts) > BURST_MERGE_SECONDS:
                self._flush_pending_pair()
            self.buffer.append(m)

    def finalize(self) -> None:
        if self.pending:
            self._flush_pending_pair()
        # Sort sample chronologically for human readability.
        self.group_sample.sort(key=lambda d: d["ts"])
        for d in self.group_sample:
            self.sample_writer.write(d)

    def _progress(self) -> None:
        dt = time.time() - self.t0
        rate = self.n_seen / dt if dt > 0 else 0.0
        sys.stderr.write(
            f"[build_dataset] seen={self.n_seen:,} group={self.n_target_group:,} "
            f"text={self.n_text:,} self={self.n_self:,} pairs={self.n_pairs:,} "
            f"({rate:,.0f} msg/s)\n"
        )
        sys.stderr.flush()


# --- Main ----------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Extract style-source conversation pairs.")
    parser.add_argument("--merge", default="merge.json", help="path to merge.json")
    parser.add_argument("--out", default="data_out", help="output directory")
    parser.add_argument("--limit", type=int, default=0,
                        help="parse at most N messages (0 = no limit)")
    parser.add_argument("--seed", type=int, default=20260524, help="reservoir RNG seed")
    args = parser.parse_args()

    random.seed(args.seed)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    merge_path = Path(args.merge)
    if not merge_path.exists():
        sys.stderr.write(f"merge.json not found at {merge_path}\n")
        return 2

    ex = Extractor(limit=args.limit, out_dir=out_dir)
    ex.pair_writer.open()
    ex.my_writer.open()
    ex.sample_writer.open()
    try:
        with open(merge_path, "rb") as fh:
            for msg in ijson.items(fh, "messages.item"):
                ex.feed(msg)
                if args.limit and ex.n_seen >= args.limit:
                    break
        ex.finalize()
    finally:
        ex.pair_writer.close()
        ex.my_writer.close()
        ex.sample_writer.close()

    dt = time.time() - ex.t0
    summary = {
        "elapsed_s": round(dt, 2),
        "n_messages_seen": ex.n_seen,
        "n_target_group_messages": ex.n_target_group,
        "n_text_messages": ex.n_text,
        "n_style_source_messages": ex.n_self,
        "n_pairs": ex.n_pairs,
        "n_pairs_reply": ex.n_pairs_reply,
        "n_pairs_window": ex.n_pairs_window,
        "n_self_burst_merges": ex.n_self_burst_merges,
        "n_group_sample": len(ex.group_sample),
        "sample_seen": ex.group_sample_seen,
        "limit": args.limit,
    }
    with open(out_dir / "build_dataset_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    sys.stderr.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
