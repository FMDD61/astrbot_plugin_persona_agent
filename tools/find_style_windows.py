"""find_style_windows — A7④ 选样辅助：滑动窗口找风格源最活跃的一小时窗口 top-N。

Spec: docs/specs/a7-step4-replay-scene.md（离线测试台选样）。

动机：merge.json 全年 86 万条，人工盲找"风格源活跃的一小时"不可行；风格源
大量用图/表情，纯靠整点切窗会漏。本工具用滑动窗口 O(n) 找出任意起止的
1 小时窗口内风格源发言量最多的前 N 段，输出报告供人工挑选转场景。

算法（O(n)）：
  1. 流式（ijson）扫 merge.json 一遍，收集：
     - all_ts：目标群全部文本消息的 epoch 时间戳（天然按时间有序，float 列表，
       约 86 万项，用于窗口内群活跃度统计，bisect O(log n) 计数）
     - style_msgs：风格源消息 (ts, text)（全年体量小，直接全存）
  2. 在 style ts 上双指针滑窗：对每个左端点 i，右指针 j 单调右移到
     ts[j] - ts[i] < window_sec，窗口内风格源条数 = j - i；起点取消息时刻
     （最优 1h 区间必可左移到某条风格源消息时刻而不减计数）。双指针 O(m)。
  3. 每步候选进最小堆（heapq，容量 C=30），保持条数降序 —— 优先队列语义。
  4. 堆内按 count 降序贪心去重叠（与已选窗口起点间隔 < window_sec/2 的跳过），
     取前 --top 个，写可读报告（md）。

用法:
  python -m tools.find_style_windows \
      [--merge merge.json] [--group 123456789] [--style-uin 234567] \
      [--window-sec 3600] [--top 5] \
      [--out data_out/style_windows_report.md]

输出（stdout 摘要 + --out 报告）：
  #N <起止 UTC+8> | 风格源 X 条（文字 a / 图 b）| 窗口群消息总数 G
  逐条列出窗口内风格源消息（文字才列全文；图片/表情只标类型），供人工判断。
"""
from __future__ import annotations

import argparse
import bisect
import heapq
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

DEFAULT_GROUP_ID = "123456789"
DEFAULT_STYLE_UIN = "234567"
_TZ8 = timezone(timedelta(hours=8))


def _parse_iso(ts_str: str) -> Optional[float]:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _fmt_local(epoch: float) -> str:
    if isinstance(epoch, str):
        return epoch
    return datetime.fromtimestamp(epoch, tz=_TZ8).strftime("%Y-%m-%d %H:%M:%S")


def _kind(text: str) -> str:
    """文字 vs 图片 vs 表情占位（merge 导出把图/表情渲染成 [图片: …]/[表情N]）。"""
    t = (text or "").strip()
    if t.startswith("[图片:") or t.startswith("[图:"):
        return "img"
    if "[表情" in t or t.startswith("[表情"):
        return "face"
    if not t:
        return "empty"
    return "text"


def _scan(merge_path: Path, group_id: str, style_uin: str):
    """流式扫一遍：返回 (all_ts, style_msgs)。all_ts 有序（merge 按时间排列）。"""
    import ijson  # dev-only

    all_ts: list[float] = []
    style_msgs: list[tuple[float, str]] = []
    n_seen = 0
    with open(merge_path, "rb") as fh:
        for msg in ijson.items(fh, "messages.item"):
            n_seen += 1
            r = msg.get("receiver") or {}
            if not (r.get("type") == "group" and str(r.get("uid")) == group_id):
                continue
            if msg.get("messageType") != 2 or msg.get("isSystemMessage") or msg.get("isRecalled"):
                continue
            ts = _parse_iso(msg.get("timestamp") or "")
            if ts is None:
                continue
            c = msg.get("content") or {}
            text = (c.get("text") or "").strip()
            if not text:
                continue
            s = msg.get("sender") or {}
            all_ts.append(ts)
            if str(s.get("uin")) == style_uin:
                style_msgs.append((ts, text[:300]))
    return all_ts, style_msgs


def _find_windows(style_msgs, window_sec: float, top: int):
    """双指针滑窗 + 最小堆 top-K（去重叠后 top 个）。

    窗口价值 = (窗口内风格源**文字**条数, 总条数) 字典序 —— 避免纯发图刷屏
    窗口排前（风格源大量用图，纯条数会把"表情连发"当最活跃）。
    返回 [{start, end, count, text_cnt, img_cnt, style_items}]，按价值降序。
    """
    ts_arr = [m[0] for m in style_msgs]
    texts = [m[1] for m in style_msgs]
    m = len(ts_arr)
    # 前缀和：任意窗口内文字条数 O(1) 查询（_kind(text)=="text"）
    pref_text = [0] * (m + 1)
    for i in range(m):
        pref_text[i + 1] = pref_text[i] + (1 if _kind(texts[i]) == "text" else 0)

    heap: list[tuple[int, int, int]] = []  # (text_cnt, count, start_i) 最小堆
    cand_cap = max(top * 8, 40)
    j = 0
    for i in range(m):
        if j < i:
            j = i
        while j < m and ts_arr[j] - ts_arr[i] < window_sec:
            j += 1
        count = j - i
        text_cnt = pref_text[j] - pref_text[i]
        key = (text_cnt, count)
        if len(heap) < cand_cap:
            heapq.heappush(heap, (key[0], key[1], i))
        elif key[0] > heap[0][0] or (key[0] == heap[0][0] and key[1] > heap[0][1]):
            heapq.heapreplace(heap, (key[0], key[1], i))

    # 去重叠：价值降序贪心，与已选窗口起点间隔 < window_sec/2 的丢弃
    cands = sorted(heap, reverse=True)  # (text, count, i)
    picked: list[dict] = []
    for text_cnt, count, i in cands:
        start = ts_arr[i]
        if any(abs(start - p["start"]) < window_sec / 2 for p in picked):
            continue
        end = start + window_sec
        style_in = [m for m in style_msgs if start <= m[0] < end]
        img_cnt = sum(1 for _, t in style_in if _kind(t) == "img")
        picked.append({
            "start": start,
            "end": end,
            "count": count,
            "text_cnt": text_cnt,
            "img_cnt": img_cnt,
            "style_items": style_in,
        })
        if len(picked) >= top:
            break
    picked.sort(key=lambda w: (-w["text_cnt"], -w["count"]))
    return picked


def _group_count(all_ts, start: float, end: float) -> int:
    return bisect.bisect_left(all_ts, end) - bisect.bisect_left(all_ts, start)


def _render(windows: list[dict], all_ts, style_uin: str) -> str:
    lines: list[str] = []
    lines.append("# 风格源活跃窗口 top（滑动窗口 1h）")
    lines.append("")
    for n, w in enumerate(windows, 1):
        g = _group_count(all_ts, w["start"], w["end"])
        lines.append(
            f"## #{n} {_fmt_local(w['start'])} → {_fmt_local(w['end'])}（UTC+8）"
            f" | 风格源 {w['count']} 条（文字 {w['text_cnt']} / 图 {w['img_cnt']}）"
            f" | 窗口群消息 {g}"
        )
        lines.append("")
        if not w["style_items"]:
            lines.append("（窗口内无风格源消息，异常）")
            continue
        for ts, text in w["style_items"]:
            k = _kind(text)
            if k == "text":
                lines.append(f"  [{_fmt_local(ts)}] {text}")
            elif k == "img":
                lines.append(f"  [{_fmt_local(ts)}] [图片]")
            elif k == "face":
                lines.append(f"  [{_fmt_local(ts)}] {text[:40]}")
        lines.append("")
    lines.append(f"风格源 QQ: {style_uin}")
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--merge", default="merge.json")
    ap.add_argument("--group", default=DEFAULT_GROUP_ID)
    ap.add_argument("--style-uin", default=DEFAULT_STYLE_UIN, help="风格源 QQ")
    ap.add_argument("--window-sec", type=float, default=3600.0, help="窗口长度秒（默认 1h）")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--out", default="data_out/style_windows_report.md")
    args = ap.parse_args(argv)

    merge_path = Path(args.merge)
    if not merge_path.exists():
        print(f"error: {merge_path} not found", file=sys.stderr)
        return 2

    all_ts, style_msgs = _scan(merge_path, args.group, args.style_uin)
    if not style_msgs:
        print(f"error: 无风格源({args.style_uin})消息", file=sys.stderr)
        return 2
    print(f"scanned group msgs={len(all_ts):,} style msgs={len(style_msgs)} "
          f"span={_fmt_local(style_msgs[0][0])} ~ {_fmt_local(style_msgs[-1][0])}")

    windows = _find_windows(style_msgs, args.window_sec, args.top)
    md = _render(windows, all_ts, args.style_uin)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"written: {out}")

    for n, w in enumerate(windows, 1):
        g = _group_count(all_ts, w["start"], w["end"])
        print(f"  #{n} {_fmt_local(w['start'])} ~ {_fmt_local(w['end'])} "
              f"style={w['count']} (text {w['text_cnt']}/img {w['img_cnt']}) group={g}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
