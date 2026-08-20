"""End-to-end smoke test for sub-agent C (style_profile + rag + interjection).

Runs on the Windows workspace without chromadb / sentence-transformers /
torch installed. We inject:

  - a FakeEmbedding backend (hashes tokens into a low-dim vector),
  - a FakeChroma collection that just returns a hand-curated set of
    "historical pairs" with synthesized distances/metadata,

then walk a tiny scripted group transcript through the pipeline:

  1. Build StyleProfile from data_out/ (real §5 artefacts).
  2. Pull RAG hits for each live context.
  3. Feed top_rag_score + @-flag + silence into InterjectionManager.
  4. Print a compact decision log.

默认用 Fake 后端（离线，不需真实依赖），也可用 `--real` 对预构建 chromadb 产物做真实 RAG 查询验证。

运行方式（两种等价）:

    1) 在插件目录（repo 根）内直接以工具包运行:
        python -m tools.smoke_rag
        python -m tools.smoke_rag --data-dir /opt/AstrBot/data/plugin_data/astrbot_plugin_persona_agent

    2) 在插件父目录以完整包路径运行:
        python -m astrbot_plugin_persona_agent.tools.smoke_rag --data-dir <数据目录>

真实 RAG 链路验证（读取预构建 chromadb 产物 + BGE 嵌入）:
        python -m tools.smoke_rag --real --data-dir <数据目录>

参数:
    --data-dir  数据目录（含风格 JSON 与 chromadb/ 产物；默认开发工作区 <插件父目录>/data_out）
    --real      使用真实 chromadb + BGE 后端做 RAG 查询（默认用 Fake 后端离线跑）

Exit code 0 means all assertions passed.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
from pathlib import Path

# 同时支持两种运行方式：
#   - `python -m astrbot_plugin_persona_agent.tools.smoke_rag`（插件父目录，相对导入生效）
#   - `python -m tools.smoke_rag`（repo 根目录，回退到顶层 services 包）
try:
    from ..services.style_profile import StyleProfile
    from ..services.rag_service import RagService
    from ..services.interjection import (
        InterjectionManager,
        TRIGGER_AT,
        TRIGGER_RAG,
        TRIGGER_SILENT,
        ACTION_REPLY,
        ACTION_SILENT,
    )
except ImportError:
    from services.style_profile import StyleProfile
    from services.rag_service import RagService
    from services.interjection import (
        InterjectionManager,
        TRIGGER_AT,
        TRIGGER_RAG,
        TRIGGER_SILENT,
        ACTION_REPLY,
        ACTION_SILENT,
    )


DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data_out"


# ---------------- fakes ----------------

class FakeEmbedding:
    """Hash tokens -> 16-d unit vector. Deterministic, cheap."""
    dim = 16

    def encode(self, texts):
        import hashlib
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for tok in t.split():
                h = hashlib.md5(tok.encode("utf-8")).digest()
                for i in range(self.dim):
                    v[i] += (h[i] - 128) / 128.0
            n = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / n for x in v])
        return out


class FakeChroma:
    """Returns a fixed set of candidate hits regardless of the query vector,
    with hand-picked distances/metadata so we can assert ranking."""

    def __init__(self, fixture: list[dict]):
        self._fixture = fixture

    def count(self) -> int:
        return len(self._fixture)

    def query(self, query_embeddings, n_results):
        items = self._fixture[:n_results]
        return {
            "ids":        [[x["id"] for x in items]],
            "documents":  [[x["document"] for x in items]],
            "metadatas":  [[x["metadata"] for x in items]],
            "distances":  [[x["distance"] for x in items]],
        }


# ---------------- fixtures ----------------

NOW = datetime(2026, 5, 24, 14, 0, 0, tzinfo=timezone.utc).timestamp()  # local 22:00 (UTC+8)
ONE_DAY = 86400.0

FIXTURE = [
    {
        "id": "pair_0001",
        "document": "Alice: 今天打瓦了吗\nBob: 没\nAlice: 来一把",
        "distance": 0.10,  # dense sim 0.90
        "metadata": {
            "reply_text": "来 我开黑",
            "reply_ts": _iso_utc(NOW - 7 * ONE_DAY),
            "reply_hour_utc": 14,
            "reply_n_burst": 1,
            "trigger": "since_last_self",
            "context_n": 3,
        },
    },
    {
        "id": "pair_0002",
        "document": "Alice: 老婆呢\nBob: 又跑了",
        "distance": 0.30,  # dense sim 0.70
        "metadata": {
            "reply_text": "换 这就换",
            "reply_ts": _iso_utc(NOW - 30 * ONE_DAY),
            "reply_hour_utc": 13,
            "reply_n_burst": 1,
            "trigger": "reply",
            "context_n": 2,
        },
    },
    {
        "id": "pair_0003",
        "document": "Alice: 投食\nBob: 还差几个",
        "distance": 0.50,  # dense sim 0.50
        "metadata": {
            "reply_text": "我来",
            "reply_ts": _iso_utc(NOW - 200 * ONE_DAY),
            "reply_hour_utc": 5,
            "reply_n_burst": 1,
            "trigger": "since_last_self",
            "context_n": 2,
        },
    },
]


# ---------------- scenarios ----------------

def scn(label: str, **kw):
    return {"label": label, **kw}


SCENARIOS = [
    scn("AT-active-cold",        is_at_me=True,  active=0, silence=300, top_rag_score=0.0,  expect=ACTION_REPLY,  trigger=TRIGGER_AT),
    scn("AT-but-disabled",       is_at_me=True,  active=0, silence=300, top_rag_score=0.0,  reply_on_at=0,         expect=ACTION_SILENT, trigger=TRIGGER_SILENT),
    scn("active=0-no-rag",       is_at_me=False, active=0, silence=10,  top_rag_score=0.9,  expect=ACTION_SILENT, trigger=TRIGGER_SILENT),
    scn("active=1-rag-hits",     is_at_me=False, active=1, silence=10,  top_rag_score=0.9,  expect=ACTION_REPLY,  trigger=TRIGGER_RAG),
    scn("active=1-rag-too-low",  is_at_me=False, active=1, silence=10,  top_rag_score=0.30, expect=ACTION_SILENT, trigger=TRIGGER_SILENT),
    scn("active=1-cold-no-bank", is_at_me=False, active=1, silence=900, top_rag_score=0.0,  expect=ACTION_SILENT, trigger=TRIGGER_SILENT),
]


def _run_fake_rag(data_dir: Path) -> None:
    """离线冒烟：Fake 嵌入 + Fake 集合，验证 RAG 排序与格式（不需真实依赖）。"""
    rag = RagService(
        data_dir,
        backend=FakeEmbedding(),
        collection=FakeChroma(FIXTURE),
    )
    hits = rag.query("今天打瓦", k=8, now_utc=NOW, top_n_final=3)
    assert hits, "RAG returned 0 hits"
    assert hits[0]["id"] == "pair_0001", f"expected pair_0001 first, got {hits[0]['id']}"
    assert hits[0]["score"] > hits[-1]["score"], "scores not descending"
    block = rag.format_examples(hits, max_chars=600)
    assert "示例1" in block and "来 我开黑" in block, "example block missing content"
    print(f"[ok] RAG(fake): top_score={hits[0]['score']:.3f} dense={hits[0]['dense']:.2f} "
          f"recency={hits[0]['recency']:.2f} hour={hits[0]['hour_match']:.2f}")
    print(f"[ok] format_examples len={len(block)}")


def _run_real(data_dir: Path) -> None:
    """真实冒烟：读取预构建 chromadb 产物 + BGE 嵌入做真实 RAG 查询。"""
    print("[real] 加载真实 chromadb + BGE 后端（首次会加载模型，较慢）...")
    rag = RagService(data_dir)  # 未注入 mock → 惰性加载 sentence-transformers + chromadb
    hits = rag.query("今天来打瓦吗 来一把", k=8, now_utc=None, top_n_final=3)
    assert hits, "real RAG returned 0 hits"
    try:
        import chromadb  # noqa: PLC0415
        coll = chromadb.PersistentClient(
            path=str(data_dir / "chromadb")
        ).get_collection("persona_pairs")
        count = coll.count()
    except Exception as exc:  # noqa: BLE001
        print(f"[real] (collection count skipped: {exc})")
        count = None
    block = rag.format_examples(hits, max_chars=600)
    assert "示例1" in block, "real format_examples missing 示例1"
    print(f"[ok] real RAG: count={count} hits={len(hits)} top_id={hits[0]['id']} "
          f"top_score={hits[0]['score']:.3f} block_len={len(block)}")
    for h in hits[:3]:
        print(f"      {h['id']:<14} dense={h['dense']:.2f} recency={h['recency']:.2f} "
              f"score={h['score']:.3f}")
    print("[ok] real format_examples ok")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="persona_agent 冒烟测试：风格文件加载 + RAG 排序 + 插话决策"
    )
    parser.add_argument(
        "--data-dir",
        default=str(DEFAULT_DATA_DIR),
        help="数据目录（含风格 JSON 与 chromadb/ 产物）。默认开发工作区 <插件父目录>/data_out。",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="用真实 chromadb+BGE 后端跑 RAG 查询验证（默认用 Fake 后端离线跑）。",
    )
    args = parser.parse_args()
    data_dir = Path(args.data_dir)

    if not data_dir.exists():
        print(f"[FATAL] data dir missing: {data_dir}", file=sys.stderr)
        return 2

    sp = StyleProfile(data_dir)

    # ---- StyleProfile sanity ----
    sys_prompt = sp.system_prompt(local_hour=22)
    assert sys_prompt and "本地时间 22 时" in sys_prompt, "system_prompt missing local hour"
    h22_budget = sp.hourly_budget(22)
    peaks = sp.peak_hours()
    print(f"[ok] StyleProfile: prompt_len={len(sys_prompt)} budget(22h)={h22_budget:.2f} peak_count={len(peaks)}")

    # ---- RAG backend（Fake 离线 or 真实）----
    if args.real:
        _run_real(data_dir)
    else:
        _run_fake_rag(data_dir)

    # ---- StyleProfile hot reload ----
    frag_path = data_dir / "system_prompt_fragments.json"
    original = frag_path.read_text("utf-8")
    print("[note] 热重载测试会临时改写 system_prompt_fragments.json 并自动还原")
    try:
        bumped = json.loads(original)
        marker = bumped.get("identity", "") + "  // smoke"
        bumped["identity"] = marker
        frag_path.write_text(json.dumps(bumped, ensure_ascii=False, indent=2), "utf-8")
        time.sleep(0.05)  # mtime resolution
        new_prompt = sp.system_prompt(local_hour=22)
        assert "// smoke" in new_prompt, "hot-reload did not pick up new identity"
        print("[ok] StyleProfile hot-reload picked up edit")
    finally:
        frag_path.write_text(original, "utf-8")

    # ---- Interjection scenarios ----
    print("\n[scenarios]")
    failed = 0
    for sc in SCENARIOS:
        mgr = InterjectionManager(
            sp,
            active_interjection=sc["active"],
            reply_on_at=sc.get("reply_on_at", 1),
            topic_bank_enabled=0,
            rag_score_threshold=0.55,
            cold_start_threshold_sec=600.0,
            min_gap_sec=0.0,      # disable gap for clean scenarios
            at_cooldown_sec=0.0,
        )
        d = mgr.decide(
            now_utc=NOW,
            is_at_me=sc["is_at_me"],
            sender_uin="337934842",
            last_group_msg_ts=NOW - sc["silence"],
            top_rag_score=sc["top_rag_score"],
        )
        ok = (d.action == sc["expect"] and d.trigger == sc["trigger"])
        mark = "ok " if ok else "FAIL"
        print(f"  [{mark}] {sc['label']:24s} -> action={d.action:7s} trigger={d.trigger:10s} reason={d.reason}")
        if not ok:
            failed += 1

    # ---- Budget + cooldown integration ----
    mgr2 = InterjectionManager(
        sp,
        active_interjection=1,
        reply_on_at=1,
        topic_bank_enabled=0,
        rag_score_threshold=0.55,
        min_gap_sec=30.0,
    )
    d1 = mgr2.decide(now_utc=NOW, is_at_me=False, last_group_msg_ts=NOW - 5, top_rag_score=0.9)
    assert d1.action == ACTION_REPLY, f"first reply should pass, got {d1}"
    mgr2.register_reply(now_utc=NOW, trigger=TRIGGER_RAG)
    d2 = mgr2.decide(now_utc=NOW + 5, is_at_me=False, last_group_msg_ts=NOW + 4, top_rag_score=0.9)
    assert d2.action == ACTION_SILENT and "min_gap_sec" in d2.reason, f"min_gap should block, got {d2}"
    print(f"[ok] min_gap blocks: cooldown_left={d2.cooldown_left_sec}s")

    # Drain the hourly budget
    mgr3 = InterjectionManager(
        sp, active_interjection=1, reply_on_at=1,
        rag_score_threshold=0.55, min_gap_sec=0.0,
    )
    budget = sp.hourly_budget(mgr3._local_hour(NOW))
    for _ in range(int(budget) + 1):
        d = mgr3.decide(now_utc=NOW, is_at_me=False, last_group_msg_ts=NOW - 5, top_rag_score=0.9)
        if d.action == ACTION_REPLY:
            mgr3.register_reply(now_utc=NOW)
    d_after = mgr3.decide(now_utc=NOW, is_at_me=False, last_group_msg_ts=NOW - 5, top_rag_score=0.9)
    assert d_after.action == ACTION_SILENT and "budget" in d_after.reason, f"budget should exhaust, got {d_after}"
    print(f"[ok] hourly budget exhausts: used={d_after.hourly_used:.2f}/{d_after.hourly_budget:.2f}")

    print(f"\n[summary] scenarios_failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

