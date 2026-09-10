"""replay_scene — A7④ 离线测试台：场景抽取（extract）+ 真实重放（run）。

Spec: docs/specs/a7-step4-replay-scene.md（执行设计，代码与文档同源）。

两个子命令：

  extract（开发机，两级选样）
    第一级 时间窗粗抽:  ijson 流式扫 merge.json（生产群 123456789）抽指定
                        UTC+8 时间窗 → 小文件 scene_draft.txt，每行
                        `[2025-10-07 20:03:12][小红]: 今天好累啊`
                        （发言人显示名；风格源就是普通群友，无任何特殊标记）
    第二级 条目范围细选: 按小文件行号起止截取 → 场景 JSON scene.json
                        {scene_id, group_id, messages:[{ts,uin,name,text}], meta}
    extract 只在开发机跑，不依赖 chromadb/BGE/任何 services —— 纯 stdlib + ijson。

  run（部署机，真 services 重放）
    读 scene.json + 部署机生产插件数据目录（风格/chromadb/知识库）+ AstrBot
    cmd_config（凭据/模型跟随，不硬编码 provider id）→ 自组真实 services
    （import services/*，不 import main.py，不触 AstrBot）→ 按场景时间线
    每条消息喂 PersonaPipeline（时钟注入到消息时刻，硬闸/Gate/情绪/RAG recency
    全部按"当时"决策）→ 输出 dsh 风格可读日志（--out）。
    隔离红线：重放只用独立临时 session/usages/logs（tmp 目录），只读生产
    风格/chroma/知识库；绝不写生产 session/决策日志/usages。key 只内存用，
    绝不写日志/场景/输出文件。

用法:
  python -m tools.replay_scene extract --merge merge.json --group 123456789 \
      --start "2025-10-07 20:00" --end "2025-10-07 21:00" [--tz +8] \
      --out data_out/scene_draft_20251007_20.txt
  python -m tools.replay_scene extract --from-draft data_out/scene_draft_20251007_20.txt \
      --range "5..120" --out data_out/scene_123456789_20251007_20.json

  python -m tools.replay_scene run --scene data_out/scene_xxx.json \
      --data-dir /opt/AstrBot/data/plugin_data/astrbot_plugin_persona_agent \
      [--cmd-config /opt/AstrBot/data/cmd_config.json] \
      [--model xxx | 环境变量 PERSONA_TEST_MODEL] \
      [--rag-off] [--no-gate] [--gate-enabled] \
      [--out replay_log.md]

Exit codes: 0 ok; 1 user/arg error; 2 runtime error.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

DEFAULT_GROUP_ID = "123456789"
DEFAULT_TZ_HOURS = 8

# merge.json 行式草稿的 ts 显示格式
_DRAFT_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _utc8tz() -> timezone:
    return timezone(timedelta(hours=DEFAULT_TZ_HOURS))


def _parse_iso(ts_str: str) -> Optional[float]:
    """"2025-01-01T07:42:13.000Z" → epoch；失败 None。"""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _extract_text(msg: dict) -> str:
    """Prefer content.text; fall back to joined rawMessage.elements textElement."""
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


def _msg_in_window(ts: float, start_epoch: float, end_epoch: float) -> bool:
    return start_epoch <= ts < end_epoch


def _draft_line_text(text: str) -> str:
    """多行消息压成单行（内部换行 → ⏎），便于草稿行阅读。首尾空白先剥。"""
    text = text.strip()
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ⏎ ").strip()


def format_ts_utc8(epoch: float, fmt: str = _DRAFT_TS_FMT) -> str:
    """epoch → UTC+8 显示串。"""
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(_utc8tz())
    return dt.strftime(fmt)


def _msg_name(msg: dict) -> str:
    return str((msg.get("sender") or {}).get("name") or "")


def _msg_uin(msg: dict) -> str:
    return str((msg.get("sender") or {}).get("uin") or "")


def _import(mod: str):
    """服务导入双路径（同 smoke_rag）：
      python -m astrbot_plugin_persona_agent.tools.replay_scene（相对，包内）
      python -m tools.replay_scene / 直接脚本（顶层 services 包）"""
    try:
        return __import__(f"..services.{mod}", fromlist=["*"])
    except ImportError:
        return __import__(f"services.{mod}", fromlist=["*"])


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------


def extract_main(args: argparse.Namespace) -> int:
    """第一级（时间窗）与第二级（--range 细选）都走这里。"""
    if getattr(args, "from_draft", ""):
        return _extract_range(args)
    return _extract_window(args)


def _extract_window(args: argparse.Namespace) -> int:
    merge_path = Path(args.merge)
    if not merge_path.exists():
        print(f"error: merge.json not found: {merge_path}", file=sys.stderr)
        return 2
    group = str(args.group or DEFAULT_GROUP_ID)
    tz_h = int(args.tz or DEFAULT_TZ_HOURS)
    start_epoch = _window_epoch(args.start, tz_h)
    end_epoch = _window_epoch(args.end, tz_h)
    if start_epoch is None or end_epoch is None or start_epoch >= end_epoch:
        print("error: --start/--end 需为 'YYYY-MM-DD HH:MM' 且 start < end", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")

    # 统计
    stats = {"seen": 0, "group": 0, "window": 0, "text": 0, "skipped_empty": 0}
    t0 = time.time()

    import ijson  # dev-only; import 放函数内避免 run 在部署机无 ijson 时炸

    seen_disp = 0
    with open(merge_path, "rb") as fh, open(tmp, "w", encoding="utf-8", newline="\n") as fout:
        for msg in ijson.items(fh, "messages.item"):
            stats["seen"] += 1
            if stats["seen"] % 200_000 == 0:
                print(f"  scanned {stats['seen']:,} …", file=sys.stderr)
            r = msg.get("receiver") or {}
            if not (r.get("type") == "group" and str(r.get("uid")) == group):
                continue
            stats["group"] += 1
            if msg.get("messageType") != 2 or msg.get("isSystemMessage") or msg.get("isRecalled"):
                continue
            ts = _parse_iso(msg.get("timestamp") or "")
            if ts is None or not _msg_in_window(ts, start_epoch, end_epoch):
                continue
            text = _extract_text(msg).strip()
            if not text:
                stats["skipped_empty"] += 1
                continue
            stats["text"] += 1
            # [2025-10-07 20:03:12][小红]: 今天好累啊
            fout.write(f"[{format_ts_utc8(ts)}][{_msg_name(msg)}]: {_draft_line_text(text)}\n")
            seen_disp += 1
    os.replace(tmp, out)

    dt = time.time() - t0
    print(
        f"[extract] window {args.start} → {args.end} (UTC{tz_h:+d}), group {group}\n"
        f"  messages in window (text): {stats['text']}  (scanned {stats['seen']:,}, "
        f"group {stats['group']:,}, empty-skipped {stats['skipped_empty']})\n"
        f"  elapsed {dt:.1f}s\n"
        f"  written {out}"
    )
    return 0


def _window_epoch(spec: str, tz_h: int) -> Optional[float]:
    """'YYYY-MM-DD HH:MM'（本地 tz_h）→ epoch。失败 None。"""
    try:
        dt = datetime.strptime(spec, "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone(timedelta(hours=tz_h))).timestamp()


def _parse_draft_local(ts_s: str) -> Optional[float]:
    """'2025-10-07 20:03:12'（草稿显示为 UTC+8）→ epoch UTC。"""
    try:
        dt = datetime.strptime(ts_s, _DRAFT_TS_FMT)
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=_utc8tz()).timestamp()


def _extract_range(args: argparse.Namespace) -> int:
    """第二级：草稿小文件按行号起止截取 → 场景 JSON。

    无 --merge：仅解析 draft 文本行（uin 为空，run 端按 name 尽力反查）。
    有 --merge：先读 draft 取起止行的时间戳作为时间边界，再流式扫 merge.json
    重建该区间真实消息（带 sender.uin/name、message_id）——发言人识别精确，
    风格源等昵称变体也能正确对到 QQ 号。行号范围与 merge 重建按 ts 对齐：
    重建取 [首行 ts, 末行 ts] 闭区间（draft 是 merge 消息的子集投影，行号即
    ts 有序，区间内 merge 消息数与 draft 行数一致——除非 draft 漏了非文本）。
    """
    draft = Path(args.from_draft)
    if not draft.exists():
        print(f"error: draft not found: {draft}", file=sys.stderr)
        return 2
    m = re.match(r"^(\d+)\.\.(\d+)$", (args.range or "").strip())
    if not m:
        print("error: --range 需为 'N..M'（如 5..120）", file=sys.stderr)
        return 1
    start_n, end_n = int(m.group(1)), int(m.group(2))
    if start_n < 1 or end_n < start_n:
        print("error: --range 需 1 <= N <= M", file=sys.stderr)
        return 1

    lines = draft.read_text("utf-8").splitlines()
    total = len(lines)
    if end_n > total:
        print(f"warning: draft 只有 {total} 行，截取到 {total}", file=sys.stderr)
        end_n = total
    if start_n > total:
        print(f"error: draft 只有 {total} 行，起止 {start_n} 越界", file=sys.stderr)
        return 1

    scene_id = args.scene_id or f"{args.group or DEFAULT_GROUP_ID}-{Path(draft).stem}"
    meta = {
        "range": f"{start_n}..{end_n}",
        "source_draft": str(draft),
    }

    merge_opt = getattr(args, "merge", "") or ""
    if merge_opt:
        return _extract_range_from_merge(args, draft, lines, start_n, end_n,
                                         scene_id, meta)

    picked = lines[start_n - 1: end_n]
    messages = []
    for ln in picked:
        mm = re.match(r"^\[([0-9-]+ [0-9:]+)\]\[([^\]]*)\]: (.*)$", ln)
        if not mm:
            continue  # 跳过空行/无法解析行
        ts_s, name, text = mm.group(1), mm.group(2), mm.group(3).strip()
        epoch = _parse_draft_local(ts_s)
        messages.append(
            {
                "ts": epoch if epoch is not None else ts_s,
                "uin": "",  # 无 --merge 时未知 uin；run 以 name 尽力反查成员
                "name": name,
                "text": text,
            }
        )
    meta["n_msgs"] = len(messages)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps({
        "scene_id": scene_id,
        "group_id": str(args.group or DEFAULT_GROUP_ID),
        "messages": messages,
        "meta": meta,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, out)
    print(f"[extract] range {start_n}..{end_n} of {total} draft lines → {len(messages)} msgs")
    print(f"  written {out}")
    return 0


def _extract_range_from_merge(args, draft, lines, start_n, end_n,
                              scene_id: str, meta: dict) -> int:
    """有 --merge 的重建：以 draft 首末行 ts 为界流式扫 merge 取真实消息（带 uin）。"""
    import ijson

    def _line_ts(ln: str):
        mm = re.match(r"^\[([0-9-]+ [0-9:]+)\]", ln)
        return _parse_draft_local(mm.group(1)) if mm else None

    first_ts = _line_ts(lines[start_n - 1])
    last_ts = _line_ts(lines[end_n - 1])
    if first_ts is None or last_ts is None:
        print("error: 起止行时间解析失败（draft 行格式异常）", file=sys.stderr)
        return 1
    if last_ts < first_ts:
        print("error: 末行时间早于首行（draft 应按时间升序）", file=sys.stderr)
        return 1

    group = str(args.group or DEFAULT_GROUP_ID)
    messages = []
    with open(Path(args.merge), "rb") as fh:
        for msg in ijson.items(fh, "messages.item"):
            r = msg.get("receiver") or {}
            if not (r.get("type") == "group" and str(r.get("uid")) == group):
                continue
            if msg.get("messageType") != 2 or msg.get("isSystemMessage") or msg.get("isRecalled"):
                continue
            ts = _parse_iso(msg.get("timestamp") or "")
            if ts is None or ts < first_ts:
                continue
            if ts > last_ts:
                break  # merge.json 时间有序，可早停
            text = _extract_text(msg).strip()
            if not text:
                continue
            s = msg.get("sender") or {}
            messages.append({
                "ts": round(ts, 3),
                "uin": str(s.get("uin") or ""),
                "name": str(s.get("name") or ""),
                "text": text,
                "message_id": str(msg.get("messageId") or ""),
            })

    meta["n_msgs"] = len(messages)
    meta["range_ts"] = [first_ts, last_ts]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps({
        "scene_id": scene_id,
        "group_id": group,
        "messages": messages,
        "meta": meta,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, out)
    print(f"[extract] merge-rebuild {start_n}..{end_n} → {len(messages)} msgs (带 uin/message_id)")
    print(f"  written {out}")
    return 0


# ---------------------------------------------------------------------------
# shared (run)
# ---------------------------------------------------------------------------
def _load_scene(path: Path) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: cannot read scene {path}: {e}", file=sys.stderr)
        raise SystemExit(2)
    msgs = data.get("messages") or []
    if not isinstance(msgs, list) or not msgs:
        print(f"error: scene {path} 无 messages", file=sys.stderr)
        raise SystemExit(2)
    return data


def _cmd_config(args) -> dict:
    """从 AstrBot cmd_config.json 找第一个 enable 的 chat provider（api_base/key）。"""
    cfg_path = Path(args.cmd_config)
    try:
        cfg = json.loads(cfg_path.read_text("utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: cannot read cmd_config {cfg_path}: {e}", file=sys.stderr)
        raise SystemExit(2)
    for src in cfg.get("provider_sources") or []:
        if not src.get("enable"):
            continue
        api_base = src.get("api_base") or ""
        key = (src.get("key") or [None])[0] or ""
        if api_base and key:
            return {"api_base": api_base, "key": key, "id": src.get("id", "")}
    print("error: cmd_config 无 enable 且带 api_base/key 的 provider", file=sys.stderr)
    raise SystemExit(2)


def _local_hour_utc8(epoch: float) -> int:
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(_utc8tz())
    return dt.hour


class ReplayRuntime:
    """组装真 services + 时间线推进 + 逐条重放。只读生产数据，写 tmp 隔离。"""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self._scene = _load_scene(Path(args.scene))
        self._group_id = str(self._scene.get("group_id") or DEFAULT_GROUP_ID)
        self._data_dir = Path(args.data_dir)
        self._tmp_dir = Path(args.tmp) if getattr(args, "tmp", "") else None
        self._virtual_now = 0.0  # 当前虚拟场景时刻（epoch UTC）
        # 由 clock() 提供给各服务
        self._frozen = False

    def clock(self) -> float:
        """虚拟时钟：未推到时返回真实时间，推到时返回场景时刻。"""
        return self._virtual_now if self._virtual_now else time.time()

    def build_services(self):
        """构造 StyleProfile/Rag/Session/Interjection/Buffer/Memory/KG/Emotion/Gate/Pipeline。

        镜像 main.initialize（防漂移），仅把写路径指到隔离 tmp、时钟指到虚拟时间。
        """
        style_profile = _import("style_profile")
        rag_service = _import("rag_service")
        session_manager = _import("session_manager")
        context_buffer = _import("context_buffer")
        interjection_mod = _import("interjection")
        memory_store = _import("memory_store")
        kg_provider = _import("kg_provider")
        emotion_mod = _import("emotion")
        gate_mod = _import("gate")
        pipeline_mod = _import("pipeline")

        StyleProfile = style_profile.StyleProfile
        RagService = rag_service.RagService
        SessionManager = session_manager.SessionManager
        ContextBuffer = context_buffer.ContextBuffer
        InterjectionManager = interjection_mod.InterjectionManager
        MemoryStore = memory_store.MemoryStore
        MultiSignalKGProvider = kg_provider.MultiSignalKGProvider
        LLMEmotionProvider = emotion_mod.LLMEmotionProvider
        DefaultEmotionProvider = emotion_mod.DefaultEmotionProvider
        GateService = gate_mod.GateService
        PersonaPipeline = pipeline_mod.PersonaPipeline

        args = self.args
        data_dir = self._data_dir
        tmp_dir = self._tmp_dir or (Path(args.out).parent / ("replay_tmp_" + self._scene.get("scene_id", "s")))

        # 数据目录写隔离：session/usages/logs 一律进 tmp，风格/chroma/知识库只读自生产
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        self.style = StyleProfile(str(data_dir))
        self.rag = RagService(str(data_dir))  # 读生产 chromadb/

        ij_cfg = (self._cfg("interjection") or {})
        topic_cfg = self._cfg("topic_bank") or {}
        self.interjection = InterjectionManager(
            self.style,
            data_dir=str(tmp_dir),  # usages 写到 tmp
            active_interjection=int(self._cfg("active_interjection") or 0),
            reply_on_at=int(self._cfg("reply_on_at") or 1),
            topic_bank_enabled=int(topic_cfg.get("enabled", 0)),
            rag_score_threshold=float((self._cfg("rag") or {}).get("score_threshold", 0.55)),
            cold_start_threshold_sec=float(ij_cfg.get("cold_start_threshold_sec", 600)),
            min_gap_sec=float(ij_cfg.get("min_gap_sec", 25)),
            at_cooldown_sec=float(ij_cfg.get("at_cooldown_sec", 8)),
            silence_cap_sec=float(ij_cfg.get("silence_cap_sec", 120)),
            local_tz_offset_hours=int(ij_cfg.get("local_tz_offset_hours", 8)),
        )
        cb_cfg = self._cfg("context_buffer") or {}
        self.buffer = ContextBuffer(
            str(tmp_dir),
            max_messages=int(cb_cfg.get("max_messages", 200)),
            max_age_sec=int(cb_cfg.get("max_age_sec", 3600)),
            persist_jsonl=False,
        )
        self.session_mgr = SessionManager(
            data_dir=str(tmp_dir),  # session 文件写 tmp
            max_messages=None,
            rotation_hour=2,
            tz_offset_hours=8,
        )
        # 知识库（MemoryStore sqlite）：读生产图谱但零写生产 —— 把生产
        # memory_store.db（含 -wal/-shm 若存在）整体复制到 tmp 再打开。重放内
        # 对该副本可安全 ingest（本次重放自积累，不污染生产）；查询结果与生产
        # 一致（除非重放中 ingest 补充了新边——符合"重放过程机器人视角积累"）。
        import shutil as _shutil
        for suffix in ("", "-wal", "-shm"):
            src = data_dir / f"memory_store.db{suffix}"
            if src.exists():
                _shutil.copy2(src, tmp_dir / f"memory_store.db{suffix}")
        self.memory_store = MemoryStore(str(tmp_dir))
        rag_cfg = self._cfg("rag") or {}
        rag_on = int(rag_cfg.get("enabled", 1)) == 1
        if getattr(args, "rag_off", False):
            rag_on = False
        self.kg_provider = MultiSignalKGProvider(
            rag=self.rag,
            store=self.memory_store,
            k_retrieve=int(rag_cfg.get("k_retrieve", 8)),
            top_n_final=int(rag_cfg.get("top_n_final", 3)),
            max_chars=int(rag_cfg.get("max_example_chars", 400)),
            dense_enabled=rag_on,
        )
        emotion_cfg = self._cfg("emotion") or {}
        if int(emotion_cfg.get("enabled", 1)) == 1:
            self.emotion = LLMEmotionProvider(
                self._llm_emotion,
                timeout=float(emotion_cfg.get("timeout_sec", 3)),
                cache_ttl=float(emotion_cfg.get("cache_ttl_sec", 30)),
                now_utc_fn=self.clock,
            )
        else:
            self.emotion = DefaultEmotionProvider()

        gate_cfg = self._cfg("gate") or {}
        gate_enabled = int(gate_cfg.get("enabled", 0)) == 1
        if getattr(args, "no_gate", False):
            gate_enabled = False
        elif getattr(args, "gate_enabled", False):
            gate_enabled = True
        self.gate = None
        if gate_enabled:
            self.gate = GateService(
                self._llm_gate,
                timeout=float(gate_cfg.get("timeout_sec", 3.0)),
                decide_cooldown_sec=float(gate_cfg.get("decide_cooldown_sec", 8.0)),
                recent_n=int(gate_cfg.get("recent_n", 15)),
                max_rag_hits=int(gate_cfg.get("max_rag_hits", 3)),
                now_utc_fn=self.clock,
            )

        self.pipeline = PersonaPipeline(
            style=self.style,
            rag=self.rag if rag_on else None,
            interjection=self.interjection,
            emotion=self.emotion,
            gate=self.gate,
            session_mgr=self.session_mgr,
            kg_provider=self.kg_provider,
            buffer=self.buffer,
            generate=self._llm_generate,
            examples_block=self._examples_block,
            postprocess=self._postprocess,
            temperature_for=self._temperature_for,
            rag_k=int(rag_cfg.get("k_retrieve", 8)),
            rag_top_n=int(rag_cfg.get("top_n_final", 3)),
            gate_recent_n=int(gate_cfg.get("recent_n", 15)),
            debounce_sec=0.0,  # 离线重放不做 500ms 模拟等待
            rag_enabled=rag_on,
            now_utc_fn=self.clock,
        )

    def _cfg(self, key: str, default=None):
        """读取插件运行配置（跟随部署机 AstrBot data/config 的插件 config）。

        解析顺序：
          1. 显式 --plugin-config 指定文件；
          2. data_dir 同级 ../config/astrbot_plugin_persona_agent_config.json
             （/opt/AstrBot/data/config/... 与 data/plugin_data/... 同层）；
          3. /opt/AstrBot/data/config/ 默认路径；
          4. 都不可用 → 空配置（用代码默认值）。
        本工具不修改配置；读不到键时返回 default。
        """
        cfg = getattr(self, "_config", None)
        if cfg is None:
            cfg_path = None
            explicit = getattr(self.args, "plugin_config", "")
            if explicit:
                p = Path(explicit)
                if p.exists():
                    cfg_path = p
            for c in (
                self._data_dir.parent / "config" / "astrbot_plugin_persona_agent_config.json",
                Path("/opt/AstrBot/data/config/astrbot_plugin_persona_agent_config.json"),
            ):
                if cfg_path is None and c.exists():
                    cfg_path = c
            try:
                cfg = json.loads(cfg_path.read_text("utf-8-sig")) if cfg_path else {}
            except Exception:
                cfg = {}
            self._config = cfg
        v = cfg.get(key)
        return v if v is not None else (default if default is not None else {})

    def _examples_block(self) -> str:
        examples_mod = _import("examples")
        try:
            cfg = self._cfg("examples") or {}
            if int(cfg.get("enabled", 1)) != 1:
                return ""
            block, _ = examples_mod.load_examples_block(
                self._data_dir / "example_dialogs.json",
                max_entries=int(cfg.get("max_entries", 12)),
            )
            return block
        except Exception:
            return ""

    @staticmethod
    def _postprocess(text: str) -> str:
        text_style = _import("text_style")
        try:
            return text_style.postprocess(text)
        except Exception:
            return text.strip()

    def _temperature_for(self, trigger: str) -> Optional[float]:
        ij_mod = _import("interjection")
        t = (self._cfg("llm") or {}).get("temperature") or {}
        try:
            if trigger == ij_mod.TRIGGER_AT:
                return float(t.get("at_reply", 0.8))
            if trigger == ij_mod.TRIGGER_RAG:
                return float(t.get("active_interjection", 1.0))
            if trigger == ij_mod.TRIGGER_COLD:
                return float(t.get("cold_start", 1.1))
        except (TypeError, ValueError):
            return None
        return None

    # ---- 网关直连（跟随 cmd_config；model 可覆盖）----
    def _creds(self) -> dict:
        if not hasattr(self, "_cred"):
            self._cred = _cmd_config(self.args)
        return self._cred

    def _chat(self, messages: list, temperature: Optional[float], reasoning: Optional[str],
              max_tokens: int = 512, timeout: float = 120.0) -> str:
        """OpenAI 兼容直连网关。key 仅内存，绝不落盘/打印。

        reasoning 为 None 时**不发送** reasoning_effort（网关只认 low/medium/
        high/max；"none"/"off"/false 全部 HTTP 400 —— 2026-09-10 实测，
        详见 services/llm_params.py 的说明）。
        """
        cred = self._creds()
        model = getattr(self.args, "model", "") or os.environ.get("PERSONA_TEST_MODEL", "")
        if not model:
            model = "deepseek/deepseek-v4.1-flash"  # fallback；一般由 CLI/cmd_config 覆盖
        payload: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if temperature is not None:
            payload["temperature"] = temperature
        if reasoning:
            payload["reasoning_effort"] = reasoning
        url = cred["api_base"].rstrip("/") + "/chat/completions"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {cred['key']}",
                "Content-Type": "application/json",
                "User-Agent": "persona-replay-scene/1.0",
            },
        )
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    out = json.loads(r.read().decode("utf-8"))
                return (out.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
            except Exception as e:
                if attempt == 2:
                    raise RuntimeError(f"gateway call failed: {type(e).__name__}: {e}")
                time.sleep(1.0)
        return ""

    async def _llm_emotion(self, prompt: str) -> str:
        emotion_mod = _import("emotion")
        ecfg = self._cfg("emotion") or {}
        return await asyncio.to_thread(
            self._chat,
            [{"role": "system", "content": emotion_mod.EMOTION_SYSTEM_PROMPT},
             {"role": "user", "content": prompt}],
            float(ecfg.get("temperature", 0.2)),
            _import("llm_params").reasoning_value(ecfg.get("reasoning_effort", "off")),
            120,
            20.0,
        )

    async def _llm_gate(self, prompt: str) -> str:
        gate_mod = _import("gate")
        gcfg = self._cfg("gate") or {}
        return await asyncio.to_thread(
            self._chat,
            [{"role": "system", "content": gate_mod.GATE_SYSTEM_PROMPT},
             {"role": "user", "content": prompt}],
            float(gcfg.get("temperature", 0.2)),
            _import("llm_params").reasoning_value(gcfg.get("reasoning_effort", "off")),
            80,
            20.0,
        )

    async def _llm_generate(self, user_text, contexts, emotion, temperature, sender_uin, umo):
        """RP 生成。风格 system_prompt + 情绪尾巴 + speaker 行与线上一致。"""
        llm_params = _import("llm_params")
        lcfg = self._cfg("llm") or {}
        local_hour = _local_hour_utc8(self.clock())
        sys_prompt = self.style.system_prompt(local_hour=local_hour) if self.style else ""
        if emotion and emotion.current_mood:
            sys_prompt = f"{sys_prompt}\n\n{emotion.current_mood}"
        # speaker hint（同 main._generate_reply：插在 contexts 倒数 system 前）
        alias_txt = ""
        if self.style is not None:
            alias_txt = self.style.preferred_alias(sender_uin) or ""
        if not alias_txt:
            alias_txt = f"群友{sender_uin}"
        speaker_line = (
            f"【当前说话人】与本消息对应的发话人：QQ {sender_uin}，群内别名「{alias_txt}」。"
            "请始终用该别名称呼 TA；无法确认时不要臆造其他群友的别名。"
        )
        ctx = list(contexts)
        if ctx and ctx[-1].get("role") == "system":
            ctx.insert(-1, {"role": "system", "content": speaker_line})
        else:
            ctx.append({"role": "system", "content": speaker_line})
        messages = [{"role": "system", "content": sys_prompt}] + ctx
        return await asyncio.to_thread(
            self._chat,
            messages,
            temperature,
            llm_params.reasoning_value(lcfg.get("reasoning_effort", "off")),
            int(lcfg.get("max_tokens", 512)),
            120.0,
        )

    # ---- 重放主循环 ----
    async def replay(self) -> int:
        pipeline_mod = _import("pipeline")
        ij_mod = _import("interjection")
        text_style = _import("text_style")

        scene = self._scene
        msgs = scene["messages"]
        lines: list[str] = []
        w = lines.append

        w(f"# replay: scene={scene.get('scene_id','')} group={self._group_id} n={len(msgs)}")
        w(f"model={getattr(self.args,'model','') or os.environ.get('PERSONA_TEST_MODEL','(cmd_config)')} "
          f"rag={'on' if self.pipeline.rag_enabled else 'off'} "
          f"gate={'on' if self.gate else 'off'}")
        w("")

        # 记忆入库：与线上 on_group_message 一致（异步 to_thread fire-and-forget
        # ingest 到 MemoryStore）。目标 store 是生产 memory_store.db 的 tmp 副本
        # （build_services 复制），写副本零污染生产 —— 重放同时模拟机器人当天
        # 的知识积累（KG relation/BM25 随重放推进），与线上经历一致。
        memory_store = _import("memory_store")
        for i, m in enumerate(msgs, 1):
            ts = m.get("ts")
            if isinstance(ts, (int, float)):
                self._virtual_now = float(ts)
            name = str(m.get("name") or "群友")
            uin = str(m.get("uin") or "")
            text = str(m.get("text") or "").strip()
            if not text:
                continue
            # 发言人解析（与线上 on_group_message 对齐）：
            #   场景带 uin → style.preferred_alias(uin)（别名）否则 name
            #   场景无 uin → 先 name 反查 member_relations（resolve_uin_from_name）；
            #                 反查不到但 name 是纯数字 → 视作 QQ 号（uin=name，
            #                 显示名用 name——merge 导出对无昵称用户显示数字 QQ）；
            #                 否则保留 name（未知成员，run 不自动加群友条目）
            alias = name
            if uin:
                if self.style is not None:
                    alias = self.style.preferred_alias(uin) or name
            else:
                resolved = self.style.resolve_uin_from_name(name) if self.style else ""
                if resolved:
                    uin = resolved
                    alias = self.style.preferred_alias(uin) or name
                elif name.isdigit():
                    uin = name
                else:
                    uin = name
            if uin and uin != self._cfg("bot_qq") and alias:
                asyncio.create_task(asyncio.to_thread(
                    self.memory_store.ingest,
                    memory_store.MemoryEvent(speaker_alias=alias, text=text,
                                group_id=self._group_id,
                                ts=float(ts) if isinstance(ts, (int, float)) else time.time()),
                ))

            w(f"━━━ [#{i}] {format_ts_utc8(float(ts) if isinstance(ts,(int,float)) else time.time())} {alias}: {text} ━━━")

            # session 入队（同线上：先入 session 再 pipeline）
            self.session_mgr.append(self._group_id, "user", text, name=alias)
            # buffer 记录（含真实 ts → last_group_msg_ts/silence 语义正确）
            self.buffer.add(
                ts=float(ts) if isinstance(ts, (int, float)) else time.time(),
                group_id=self._group_id,
                sender_id=uin or "unknown",
                sender_name=alias,
                text=text,
                message_id=str(m.get("message_id") or ""),
                message_type="group",
            )

            intent = await self.pipeline.run(pipeline_mod.PipelineInput(
                group_id=self._group_id,
                text=text,
                is_at=False,  # 重放群友消息流不模拟 @ 机器人（与设计一致）
                sender_uin=uin or "unknown",
                sender_alias=alias,
            ))
            trace = intent.trace or {}

            # 决策渲染
            hg = trace.get("hard_gate") or {}
            gate_d = trace.get("gate") or {}
            rag_hits = trace.get("rag") or []

            if intent.action == ij_mod.ACTION_SILENT:
                reason = intent.silent_reason or hg.get("reason", "")
                w(f"【决策】silent — {reason}"
                  + (f" | gate={gate_d.get('reply')}, conflict={gate_d.get('conflict')}: {gate_d.get('reason','')}" if gate_d else "")
                  + (f" | rag_score={rag_hits[0].get('score') if rag_hits else '—'}" if rag_hits else ""))
            elif intent.action == ij_mod.ACTION_TOPIC:
                w(f"【决策】topic — {hg.get('reason','')}")
            elif intent.action == ij_mod.ACTION_REPLY:
                top = rag_hits[0].get("score") if rag_hits else None
                w(f"【决策】reply — {hg.get('reason','')}"
                  + (f" | gate reply={gate_d.get('reply')}, conflict={gate_d.get('conflict')}" if gate_d else ""))
                if top is not None:
                    w(f"【RAG top】{rag_hits[0].get('document','')[:120]} [{top:.2f}]")
                if gate_d and gate_d.get("reason"):
                    w(f"【Gate】{gate_d.get('reason','')}")
                w(f"【回复】{intent.text}")
            else:
                w(f"【决策】{intent.action} — {intent.silent_reason or hg.get('reason','')}")

            # 副作用与线上一致：真回复才 register_reply + session.append assistant
            if intent.action == ij_mod.ACTION_REPLY and intent.text:
                self.interjection.register_reply(
                    group_id=self._group_id,
                    now_utc=float(ts) if isinstance(ts, (int, float)) else time.time(),
                    trigger=str(hg.get("trigger", "")),
                    sender_uin=uin or "unknown",
                )
                self.session_mgr.append(self._group_id, "assistant", intent.text)
                self.buffer.add(
                    ts=float(ts) if isinstance(ts, (int, float)) else time.time(),
                    group_id=self._group_id,
                    sender_id=self._cfg("bot_qq") or "bot",
                    sender_name="<bot>",
                    text=intent.text,
                    message_id="",
                    message_type="bot",
                )

        # 写 --out
        out = Path(self.args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, out)
        print(f"[replay] {len(msgs)} msgs → {out}")
        return 0


def run_main(args: argparse.Namespace) -> int:
    """run 子命令：同步壳，内部 async。"""
    import asyncio
    rt = ReplayRuntime(args)
    rt.build_services()
    # 冷启动：RAG warmup（部署机有 BGE/chroma；失败不致命）
    try:
        rt.rag.warmup()
    except Exception:
        pass
    return asyncio.run(rt.replay())


# ---------------------------------------------------------------------------
def _add_extract_parser(sp) -> None:
    sp.add_argument("--merge", default="", help="merge.json 路径（第二级给则按 ts 重建场景带 uin）")
    sp.add_argument("--group", default=DEFAULT_GROUP_ID, help="目标群 uid")
    sp.add_argument("--start", help="窗口起点 'YYYY-MM-DD HH:MM'（本地 tz）")
    sp.add_argument("--end", help="窗口终点 'YYYY-MM-DD HH:MM'")
    sp.add_argument("--tz", default=DEFAULT_TZ_HOURS, help="窗口时间解释为 UTC+?（默认 +8）")
    sp.add_argument("--from-draft", help="第二级：草稿小文件路径")
    sp.add_argument("--range", help="第二级：行号起止 'N..M'（如 5..120）")
    sp.add_argument("--scene-id", dest="scene_id", default="", help="场景 ID（默认自动）")
    sp.add_argument("--out", required=True, help="输出文件（草稿 txt 或场景 json）")
    sp.set_defaults(func=extract_main)


def _add_run_parser(sp) -> None:
    sp.add_argument("--scene", required=True, help="场景 JSON")
    sp.add_argument("--data-dir", required=True,
                    help="部署机生产插件数据目录（风格/chromadb/知识库）")
    sp.add_argument("--cmd-config", default="/opt/AstrBot/data/cmd_config.json",
                    help="AstrBot cmd_config.json（找 enable provider 的 api_base/key）")
    sp.add_argument("--plugin-config", default="",
                    help="插件 config JSON（默认自动探测 data/config/…）")
    sp.add_argument("--model", default="", help="模型名覆盖（默认 cmd_config/环境 PERSONA_TEST_MODEL）")
    sp.add_argument("--rag-off", action="store_true", help="强制 rag.enabled=0")
    sp.add_argument("--no-gate", action="store_true", help="强制 gate.enabled=0")
    sp.add_argument("--gate-enabled", action="store_true", help="强制 gate.enabled=1")
    sp.add_argument("--tmp", default="", help="隔离写目录（默认 <out 同目录>/replay_tmp_<scene_id>）")
    sp.add_argument("--out", required=True, help="dsh 风格日志输出文件（.md/.txt）")
    sp.set_defaults(func=run_main)


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    _add_extract_parser(sub.add_parser("extract", help="从 merge.json 抽场景（两级选样）"))
    _add_run_parser(sub.add_parser("run", help="真 services 重放场景 → dsh 日志"))
    args = ap.parse_args(argv)
    if args.cmd == "extract" and not args.from_draft and not (args.start and args.end):
        ap.error("extract 需 --start+--end（时间窗）或 --from-draft+--range（细选）")
    if args.cmd == "extract" and args.from_draft and not args.range:
        ap.error("--from-draft 需配合 --range 'N..M'")
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
