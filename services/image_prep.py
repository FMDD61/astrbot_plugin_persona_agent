# -*- coding: utf-8 -*-
"""图像预处理：送视觉模型前的降采样与**动图抽帧拼网格**。

## 为什么独立成模块

`services/vision.py`（线上识图）与 `tools/build_sticker_index.py`（离线贴纸入库）
都需要"把任意图片变成为视觉模型友好的载荷"。放在任一侧都会造成反向依赖
（service 不能 import tools），所以抽到中性位置由两边共用。

## 三条实测结论

**① 不降采样 = 白烧 7 倍时间，且没有任何额外信息。**（2026-09-14，956 次标定 + A/B）
同一张图 A/B（同模型同参数）：

| | `prepare_for_vision`(768px) | 原图直送 |
|---|---|---|
| payload | 25–73 KB | 5.8–13.4 MB |
| **prompt_tokens** | **412** | **412（完全相同）** |
| 时延 | **4.0s** | **29.6s 均值 / 最坏 127.5s** |

网关侧图像 token 数是固定的 → 原图只贡献上传时间。

**② 动图不能只取第一帧，也**不能**整图直送 —— 改为抽帧拼网格（C27，2026-09-21）。**
很多 GIF 靠动态动作体现含义（实测：首帧把"疯狂摇头撞桌"描述成"张嘴"）；
而整图直送很贵（1.9MB/109 帧 实测 52s 且空返回）。

⚠️ **曾经的取舍错在哪（B-049）**：把"大动图"降级成**首帧 JPEG**（体积闸门
`GIF_INLINE_MAX_BYTES = 1_500_000`）。用户实测群内 GIF **普遍 >3MB、
大的 >10MB** → 那条"≤1.5MB 整图直送"的路**几乎从不触发**，绝大多数 GIF 都在走首帧，
**动作语义全丢**，而且**不留任何痕迹**（不报错、不降级标记，只是描述变静帧）。

现在的做法：**全部多帧 GIF 一律"均匀抽 6 帧 → 每帧长边 320px → 3 列 × 2 行拼网格
→ JPEG q82"**（约 960×640、几十 KB）。动作语义来自**帧与帧的差异**，网格正好把
差异并排摆出来 —— 一举解决"体积"与"动作丢失"两个问题。
**体积闸门已废**（`GIF_INLINE_MAX_BYTES` 只剩一个不再被读的名字，见下）。

**③ 降级必须留痕。** 拼网格失败（无 PIL / 解码失败）时回退首帧，但**原因必须带出去** ——
`meta["gif_grid_fail"]` → 线上落 `diag`/`stats`/日志（见 `services/vision.py`）。
"""
from __future__ import annotations

import io
import os
from pathlib import Path

# 降采样上限（长边像素）。768 是实测够用的档位：表情包的表情/文字细节保留，
# 请求体压到几十 KB，而图像 token 数与更大分辨率相同。
DESC_MAX_EDGE = 768
DESC_JPEG_QUALITY = 82

# ---------------------------------------------------------------- C27：GIF 网格
#: 均匀抽几帧（**含首尾**；帧数不足则全取）
GIF_GRID_FRAMES = 6
#: 每帧长边上限（只缩不放 —— 放大不加信息、只加字节）
GIF_GRID_FRAME_EDGE = 320
#: 网格列数 × 行数；``COLS * ROWS`` 必须 ≥ ``GIF_GRID_FRAMES``（否则多余的帧丢）
GIF_GRID_COLS = 3
GIF_GRID_ROWS = 2
#: 网格 JPEG 质量（与静图同档：q82 在几十 KB 量级，实测够用）
GIF_GRID_JPEG_QUALITY = 82

# ⚠️ **已废**（C27 / B-049）：1.5MB 体积闸门不再参与任何分流，多帧 GIF **一律**拼网格。
# 保留这个名字**只为** `tools/build_sticker_index.py` 的 `from services.image_prep import ...`
# 不炸（那个文件不在本次改动范围内）。它的值**没有任何作用**，改它不会改变行为
# —— `tests/test_image_prep.py::test_gate_constant_no_longer_gates` 钉住了这一点。
# 待离线工具改用新路径后，这个名字应当删掉。
GIF_INLINE_MAX_BYTES = 1_500_000

_MIME_BY_EXT = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
}
_MIME_BY_MAGIC = (
    (b"GIF8", "image/gif"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)


def sniff_mime(data: bytes) -> str:
    """按魔数嗅探 MIME（未知回退 png）。"""
    for magic, mime in _MIME_BY_MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def mime_of(path: str | os.PathLike) -> str:
    ext = Path(path).suffix.lower().lstrip(".")
    return _MIME_BY_EXT.get(ext, "image/png")


def frame_count(path: str | os.PathLike) -> int:
    """返回帧数（>1 即动图）。打不开返回 0。"""
    try:
        from PIL import Image  # type: ignore
        with Image.open(path) as im:
            return int(getattr(im, "n_frames", 1) or 1)
    except Exception:
        return 0


def to_jpeg(data: bytes, max_edge: int = DESC_MAX_EDGE,
            quality: int = DESC_JPEG_QUALITY) -> bytes:
    """把任意图片字节降采样成 JPEG（**动图取首帧**）。任何失败回退原字节。

    回退而非抛错：宁可慢一点/大一点，也不要因为预处理失败而丢掉描述
    （B-005 的教训：静默丢弃会被误读成"识别不了"）。

    ⚠️ 动图**不该**走这里（会把动作语义压成一张静帧）—— 那是 `gif_grid` 的活；
    这里只在"拼网格失败"的降级路径上兜底，且调用方必须把降级报出去。
    """
    try:
        from PIL import Image  # type: ignore
        with Image.open(io.BytesIO(data)) as im:
            im.seek(0)                       # 动图取首帧
            im = im.convert("RGB")
            w, h = im.size
            if max(w, h) > max_edge:
                scale = max_edge / float(max(w, h))
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                               Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality, optimize=True)
            return buf.getvalue()
    except Exception:
        return data


def gif_frame_indices(n_frames: int, k: int = GIF_GRID_FRAMES) -> list[int]:
    """均匀抽 ``k`` 帧的**帧号**：含首尾、等间隔；帧数不足则全取。

    ``round(i * (n-1)/(k-1))`` —— 保证第一帧与最后一帧一定入选（动作的起点与
    终点是最有信息的两格），中间等间隔跳帧。返回值严格递增（不会出现同一帧占两格）。
    """
    n = int(n_frames or 0)
    k = int(k or 0)
    if n <= 0 or k <= 0:
        return []
    if k >= n:
        return list(range(n))
    if k == 1:
        return [0]
    return [int(round(i * (n - 1) / (k - 1))) for i in range(k)]


def _frame_rgb(im):
    """单帧 → RGB：透明像素**合成到白底**。

    JPEG 没有 alpha 通道，直接 `convert("RGB")` 会把透明区变成**黑色** ——
    对表情包（大量透明底）等于凭空造出一片黑块，模型会当画面内容描述。
    """
    from PIL import Image  # type: ignore
    fr = im.convert("RGBA")
    bg = Image.new("RGB", fr.size, (255, 255, 255))
    bg.paste(fr, mask=fr.split()[-1])
    return bg


def gif_grid(data: bytes, *, frames: int = GIF_GRID_FRAMES,
             frame_edge: int = GIF_GRID_FRAME_EDGE,
             cols: int = GIF_GRID_COLS, rows: int = GIF_GRID_ROWS,
             quality: int = GIF_GRID_JPEG_QUALITY,
             meta: dict | None = None) -> bytes | None:
    """多帧 GIF → **抽帧拼网格**的 JPEG（C27）。不适用或失败返回 ``None``。

    返回 None 有**两种**含义，调用方必须分得开（分不开就是静默降级）：

    - **不是动图**（非 GIF / 单帧 GIF）→ 正常走静图降采样路径，``meta`` 不动；
    - **该拼却拼不出来**（无 Pillow / 解码失败）→ ``meta["gif_grid_fail"]`` 记下原因，
      调用方回退首帧并**必须**把它报出去（diag / stats / 日志）。

    成功时写 ``meta["gif_grid"]``（实际帧数/总帧数/格子边长/列数/行数/画布尺寸），
    供调用方选"动图网格版"提示词并做观测。
    """
    if sniff_mime(data) != "image/gif":
        return None
    try:
        from PIL import Image  # type: ignore
    except ImportError as e:
        if meta is not None:
            meta.setdefault("gif_grid_fail", f"no PIL: {e}")
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            total = int(getattr(im, "n_frames", 1) or 1)
            if total <= 1:
                return None                     # 单帧：不是降级，走静图路径
            cell = max(1, int(frame_edge))
            n_cols = max(1, int(cols))
            n_rows = max(1, int(rows))
            idx = gif_frame_indices(total, frames)[: n_cols * n_rows]
            if not idx:
                return None
            # 帧数不足时**不留整行空白**（2~3 帧的 GIF 实测占 12%）：画布高度按实际
            # 用到的行数收窄，省下的字节是纯赚。最后一行的零星空格留给提示词解释。
            rows_used = max(1, (len(idx) + n_cols - 1) // n_cols)
            canvas = Image.new("RGB", (cell * n_cols, cell * rows_used), (255, 255, 255))
            for slot, fi in enumerate(idx):
                im.seek(fi)
                fr = _frame_rgb(im)
                w, h = fr.size
                if max(w, h) > cell:            # 只缩不放
                    s = cell / float(max(w, h))
                    fr = fr.resize((max(1, int(w * s)), max(1, int(h * s))),
                                   Image.LANCZOS)
                canvas.paste(fr, ((slot % n_cols) * cell + (cell - fr.width) // 2,
                                  (slot // n_cols) * cell + (cell - fr.height) // 2))
            buf = io.BytesIO()
            canvas.save(buf, format="JPEG", quality=int(quality), optimize=True)
            out = buf.getvalue()
    except Exception as e:
        if meta is not None:
            meta.setdefault("gif_grid_fail", f"{type(e).__name__}: {e}")
        return None
    if meta is not None:
        meta["gif_grid"] = {"frames": len(idx), "of": total, "cell": cell,
                           "cols": n_cols, "rows": rows_used,
                           "size": [cell * n_cols, cell * rows_used]}
        meta["payload_bytes"] = len(out)
    return out


def prepare_for_vision(path: str | os.PathLike, *,
                       max_edge: int = DESC_MAX_EDGE,
                       meta: dict | None = None) -> tuple[bytes, str]:
    """把磁盘上的图片变成适合送视觉模型的 ``(bytes, mime)``。

    - **多帧 GIF → 抽帧拼网格 JPEG**（C27；不再分体积）
    - 其余 → 长边 ``max_edge`` 降采样转 JPEG（首帧）
    - PIL 不可用或解码失败 → 原字节 + 按魔数嗅探的 mime（原因见 ``meta``）

    ``meta`` 是可选出参（见 ``prepare_bytes_for_vision``）。
    """
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as e:
        if meta is not None:
            meta["read_fail"] = f"{type(e).__name__}: {e}"
        return b"", "image/png"
    return prepare_bytes_for_vision(data, max_edge=max_edge, meta=meta)


def _as_jpeg_or_original(data: bytes, max_edge: int) -> tuple[bytes, str]:
    """降采样；若转换未生效（PIL 缺失/解码失败），**按真实内容**标注 mime。

    ⚠️ 不能盲目返回 ``"image/jpeg"``：`to_jpeg` 在失败时**回退成原字节**，
    此时标签就会与实际内容不符（PIL 缺失时尤其危险）。用魔数核对输出。
    """
    out = to_jpeg(data, max_edge=max_edge)
    if out is data or out == data:
        return out, sniff_mime(out)
    return out, "image/jpeg"


def prepare_bytes_for_vision(data: bytes, *,
                             max_edge: int = DESC_MAX_EDGE,
                             meta: dict | None = None) -> tuple[bytes, str]:
    """`prepare_for_vision` 的内存版本（线上识图拿到的是 bytes，不是路径）。

    返回契约仍是 ``(bytes, mime)``（老调用方原样可用）。``meta`` 是**可选出参**：
    只在调用方显式传入时填写，用来回答"这次走了哪条路、有没有降级、载荷多大" ——
    `VisionService.describe_bytes` 靠它选提示词（GIF 网格版 / 静图版）并留痕。
    """
    if not data:
        return b"", "image/png"
    if sniff_mime(data) == "image/gif":
        grid = gif_grid(data, meta=meta)
        if grid is not None:
            if meta is not None:
                meta["path"] = "gif_grid"
            return grid, "image/jpeg"
        # 落到这里 = 不是动图（正常）或拼网格失败（降级，原因已在 meta 里）
    out, mime = _as_jpeg_or_original(data, max_edge)
    if meta is not None:
        meta["path"] = ("gif_firstframe_fallback" if meta.get("gif_grid_fail")
                        else "static")
    return out, mime
