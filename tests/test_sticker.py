# -*- coding: utf-8 -*-
"""S3②：贴纸选择器 + 离线入库工具的测试。

不依赖 BGE / 网关：嵌入用确定性假函数，视觉描述路径不测（那是 I/O 薄壳）。
"""
import asyncio
import json
import os
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from services.sticker import StickerService, StickerHit, embed_via_rag  # noqa: E402
from tools import build_sticker_index as B  # noqa: E402


def fake_embed(texts):
    """确定性假嵌入：关键词计数归一化向量。"""
    keys = ("无奈", "开心", "害羞", "生气")
    out = []
    for t in texts:
        v = [float(str(t).count(k)) for k in keys]
        n = sum(x * x for x in v) ** 0.5 or 1.0
        out.append([x / n for x in v])
    return out


def _png(rgb=(200, 40, 40), w=8, h=8):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def ck(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

    return (b"\x89PNG\r\n\x1a\n"
            + ck(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + ck(b"IDAT", zlib.compress(raw)) + ck(b"IEND", b""))


def write_index(path, items):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "model": "fake", "dim": 4, "items": items}, f,
                  ensure_ascii=False)
    return path


def mk_items(specs):
    return [
        {"id": i, "file": f"{i}.jpg", "desc": d, "tags": [], "embedding": fake_embed([d])[0]}
        for i, d in specs
    ]


class TestStickerPick(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.idx = os.path.join(self.td.name, "sticker_index.json")

    def tearDown(self):
        self.td.cleanup()

    def _svc(self, items, **kw):
        write_index(self.idx, items)
        return StickerService(self.idx, fake_embed, library_dir=self.td.name, **kw)

    def test_high_confidence_bge_pick(self):
        svc = self._svc(mk_items([("a", "无奈"), ("b", "开心"), ("c", "害羞")]),
                        min_score=0.5, margin=0.05)
        r = asyncio.run(svc.pick("无奈"))
        self.assertIsNotNone(r.hit)
        self.assertEqual(r.hit.id, "a")
        self.assertEqual(r.via, "bge")
        self.assertEqual(svc.stats["ok"], 1)

    def test_hit_carries_absolute_path(self):
        svc = self._svc(mk_items([("a", "无奈")]), min_score=0.5)
        r = asyncio.run(svc.pick("无奈"))
        self.assertEqual(r.hit.path, os.path.join(self.td.name, "a.jpg"))

    def test_below_threshold_skips(self):
        # 注意：假嵌入下"无奈"↔"无奈"余弦恰为 1.0，所以要用 >1 的阈值
        # 才能构造出 below_threshold（真实 BGE 上不会出现 1.0）
        svc = self._svc(mk_items([("a", "无奈")]), min_score=1.01)
        r = asyncio.run(svc.pick("无奈"))
        self.assertIsNone(r.hit)
        self.assertIn("below_threshold", r.reason)
        self.assertEqual(svc.stats["below_threshold"], 1)
        self.assertTrue(r.top_k)          # 失败也要能看到候选分数（事后调阈）

    def test_ambiguous_without_picker_skips_conservatively(self):
        # "无奈 开心" 与两条候选等距 → margin 判定为 ambiguous → 宁可不发
        svc = self._svc(mk_items([("a", "无奈"), ("b", "开心")]), min_score=0.1, margin=0.5)
        r = asyncio.run(svc.pick("无奈 开心"))
        self.assertIsNone(r.hit)
        self.assertIn("ambiguous", r.reason)

    def test_ambiguous_with_picker_uses_llm_choice(self):
        async def picker(intent, cands):
            return cands[1].id
        svc = self._svc(mk_items([("a", "无奈"), ("b", "开心")]),
                        min_score=0.1, margin=0.5, picker=picker)
        r = asyncio.run(svc.pick("无奈 开心"))
        self.assertIsNotNone(r.hit)
        self.assertEqual(r.hit.id, "b")
        self.assertEqual(r.via, "llm")
        self.assertEqual(svc.stats["llm_pick"], 1)

    def test_picker_returns_none_skips(self):
        async def picker(intent, cands):
            return None
        svc = self._svc(mk_items([("a", "无奈"), ("b", "开心")]),
                        min_score=0.1, margin=0.5, picker=picker)
        r = asyncio.run(svc.pick("无奈 开心"))
        self.assertIsNone(r.hit)
        self.assertEqual(r.reason, "picker_no_choice")

    def test_picker_exception_is_visible(self):
        async def picker(intent, cands):
            raise RuntimeError("llm boom")
        svc = self._svc(mk_items([("a", "无奈"), ("b", "开心")]),
                        min_score=0.1, margin=0.5, picker=picker)
        r = asyncio.run(svc.pick("无奈 开心"))
        self.assertIsNone(r.hit)
        self.assertIn("llm boom", svc.last_error or "")

    def test_empty_library_reason(self):
        svc = self._svc([], min_score=0.5)
        r = asyncio.run(svc.pick("无奈"))
        self.assertIsNone(r.hit)
        self.assertEqual(r.reason, "empty_library")

    def test_missing_index_reason_distinct_from_empty(self):
        """索引文件缺失 vs 库为空 —— 处置完全不同，必须分开。"""
        svc = StickerService(os.path.join(self.td.name, "nope.json"), fake_embed)
        r = asyncio.run(svc.pick("无奈"))
        self.assertEqual(r.reason, "index_missing")

    def test_no_embed_fn_reason(self):
        write_index(self.idx, mk_items([("a", "无奈")]))
        svc = StickerService(self.idx, None, library_dir=self.td.name)
        r = asyncio.run(svc.pick("无奈"))
        self.assertEqual(r.reason, "no_embed")

    def test_empty_intent_reason(self):
        svc = self._svc(mk_items([("a", "无奈")]))
        r = asyncio.run(svc.pick("   "))
        self.assertEqual(r.reason, "empty_intent")

    def test_items_without_embedding_are_skipped(self):
        items = mk_items([("a", "无奈")])
        items.append({"id": "noemb", "file": "x.jpg", "desc": "无奈", "embedding": None})
        svc = self._svc(items, min_score=0.5)
        r = asyncio.run(svc.pick("无奈"))
        self.assertEqual(r.hit.id, "a")

    def test_index_hot_reload_on_mtime_change(self):
        """索引是人工文件：改完不该重启才生效。"""
        svc = self._svc(mk_items([("a", "无奈")]), min_score=0.5)
        self.assertEqual(svc.size, 1)
        write_index(self.idx, mk_items([("a", "无奈"), ("b", "开心")]))
        os.utime(self.idx, (0, 0))          # 确保 mtime 变化
        svc.reload_if_changed()
        self.assertEqual(svc.size, 2)

    def test_snapshot_reports_observability(self):
        svc = self._svc(mk_items([("a", "无奈")]), min_score=0.5)
        asyncio.run(svc.pick("无奈"))
        snap = svc.snapshot()
        self.assertEqual(snap["size"], 1)
        self.assertEqual(snap["stats"]["ok"], 1)
        self.assertFalse(snap["picker"])

    def test_embed_via_rag_none_safe(self):
        self.assertIsNone(embed_via_rag(None))

    def test_embed_via_rag_broken_backend_returns_none(self):
        class Bad:
            def _ensure_backend(self):
                raise RuntimeError("no model")
        self.assertIsNone(embed_via_rag(Bad()))

    def test_embed_via_rag_wraps_encode(self):
        class Backend:
            def encode(self, texts):
                return [[1.0, 0.0] for _ in texts]

        class Rag:
            def _ensure_backend(self):
                return Backend()
        fn = embed_via_rag(Rag())
        self.assertIsNotNone(fn)
        self.assertEqual(fn(["x"]), [[1.0, 0.0]])


class TestBuildStickerIndex(unittest.TestCase):
    """离线入库工具的不变式。"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.lib = Path(self.td.name) / "sticker_library"
        self.lib.mkdir()

    def tearDown(self):
        self.td.cleanup()

    def _put(self, name, rgb):
        (self.lib / name).write_bytes(_png(rgb))

    def test_scan_skips_non_images(self):
        self._put("a.png", (1, 2, 3))
        (self.lib / "readme.txt").write_text("x")
        sc, dup = B.scan_images(self.lib)
        self.assertEqual([s["file"] for s in sc], ["a.png"])
        self.assertEqual(dup, [])

    def test_scan_dedups_identical_content(self):
        """同图改名不得重复入库（否则索引里两条一模一样的贴纸）。

        保留哪一个 = 按文件名排序的第一个。注意 `' '`(0x20) < `'_'`(0x5F)，
        所以 `001 - 副本.png` 排在 `001_无奈.png` **前面**（是本测试踩到的细节）。
        """
        self._put("001_无奈.png", (200, 40, 40))
        self._put("001 - 副本.png", (200, 40, 40))
        self._put("002_开心.png", (40, 200, 40))
        sc, dup = B.scan_images(self.lib)
        self.assertEqual(len(sc), 2)
        self.assertIn(sc[0]["file"], ("001 - 副本.png", "001_无奈.png"))
        self.assertEqual(len(dup), 1)
        self.assertNotIn(dup[0], [x["file"] for x in sc])

    def test_merge_is_incremental_and_preserves_human_edits(self):
        """红线 #3：工具只建议不覆盖 —— 人工改过的 desc/tags 必须保住。"""
        self._put("a.png", (1, 2, 3))
        sc, _ = B.scan_images(self.lib)
        items, added, reused = B.merge({"items": []}, sc, force=False)
        self.assertEqual((added, reused), (1, 0))
        items[0]["desc"] = "人工描述"
        items[0]["tags"] = ["人工"]
        items2, added2, reused2 = B.merge({"items": items}, sc, force=False)
        self.assertEqual((added2, reused2), (0, 1))
        self.assertEqual(items2[0]["desc"], "人工描述")
        self.assertEqual(items2[0]["tags"], ["人工"])

    def test_force_recomputes(self):
        self._put("a.png", (1, 2, 3))
        sc, _ = B.scan_images(self.lib)
        items = [{"id": "a", "file": "a.png", "sha256": sc[0]["sha256"],
                  "desc": "旧", "tags": ["旧"], "embedding": [1.0]}]
        items2, added, _ = B.merge({"items": items}, sc, force=True)
        self.assertEqual(added, 1)
        self.assertEqual(items2[0]["desc"], "")
        self.assertIsNone(items2[0]["embedding"])

    def test_load_index_tolerates_corruption(self):
        p = Path(self.td.name) / "bad.json"
        p.write_text("{not json")
        d = B.load_index(p)
        self.assertEqual(d["items"], [])

    def test_scan_empty_dir(self):
        empty = Path(self.td.name) / "nope"
        sc, dup = B.scan_images(empty)
        self.assertEqual(sc, [])
        self.assertEqual(dup, [])


if __name__ == "__main__":
    unittest.main()
