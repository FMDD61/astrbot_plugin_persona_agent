# -*- coding: utf-8 -*-
"""图像预处理：送视觉模型前的降采样与动图处理。

## 为什么独立成模块

`services/vision.py`（线上识图）与 `tools/build_sticker_index.py`（离线贴纸入库）
都需要"把任意图片变成为视觉模型友好的载荷"。放在任一侧都会造成反向依赖
（service 不能 import tools），所以抽到中性位置由两边共用。

## 两条实测结论（2026-09-14，956 次标定调用 + A/B）

**① 不降采样 = 白烧 7 倍时间，且没有任何额外信息。**
同一张图 A/B（同模型同参数）：

| | `prepare_for_vision`(768px) | 原图直送 |
|---|---|---|
| payload | 25–73 KB | 5.8–13.4 MB |
| **prompt_tokens** | **412** | **412（完全相同）** |
| 时延 | **4.0s** | **29.6s 均值 / 最坏 127.5s** |

网关侧图像 token 数是固定的 → 原图只贡献上传时间。线上 `vision.py` 一直是
原图直送，导致 10 个最大 GIF 里 4 个撞 15s 超时、1 个撞 30s 超时。

**② 动图不能只取第一帧，但大动图要回退首帧。**
很多 GIF 靠动态动作体现含义（实测：首帧把"疯狂摇头撞桌"描述成"张嘴"）；
而 1.9MB/109 帧 的整图实测 52s 且空返回 → 需要体积闸门。
"""
from __future__ import annotations

import io
import os
from pathlib import Path

# 降采样上限（长边像素）。768 是实测够用的档位：表情包的表情/文字细节保留，
# 请求体压到几十 KB，而图像 token 数与更大分辨率相同。
DESC_MAX_EDGE = 768
DESC_JPEG_QUALITY = 82

# 动图整图直送上限（见模块 docstring 结论②）。
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
    """把任意图片字节降采样成 JPEG。**任何失败回退原字节**。

    回退而非抛错：宁可慢一点/大一点，也不要因为预处理失败而丢掉描述
    （B-005 的教训：静默丢弃会被误读成"识别不了"）。
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


def prepare_for_vision(path: str | os.PathLike, *,
                       max_edge: int = DESC_MAX_EDGE) -> tuple[bytes, str]:
    """把磁盘上的图片变成适合送视觉模型的 ``(bytes, mime)``。

    - **多帧 GIF 且 ≤ ``GIF_INLINE_MAX_BYTES`` → 整图直送**（保留动作语义）
    - 其余 → 长边 ``max_edge`` 降采样转 JPEG（首帧）
    - PIL 不可用或解码失败 → 原字节 + 按扩展名嗅探的 mime
    """
    p = Path(path)
    try:
        size = p.stat().st_size
        if size <= GIF_INLINE_MAX_BYTES and frame_count(p) > 1:
            return p.read_bytes(), "image/gif"
    except OSError:
        pass
    try:
        data = p.read_bytes()
    except OSError:
        return b"", "image/png"
    return _as_jpeg_or_original(data, max_edge)


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
                             max_edge: int = DESC_MAX_EDGE) -> tuple[bytes, str]:
    """`prepare_for_vision` 的内存版本（线上识图拿到的是 bytes，不是路径）。"""
    if not data:
        return b"", "image/png"
    if sniff_mime(data) == "image/gif" and len(data) <= GIF_INLINE_MAX_BYTES:
        # 魔数已确认是 GIF；只有**多帧**才值得整图直送（保留动作语义）；
        # 单帧 GIF 走降采样转 JPEG 更小。
        try:
            from PIL import Image  # type: ignore
            with Image.open(io.BytesIO(data)) as im:
                if int(getattr(im, "n_frames", 1) or 1) > 1:
                    return data, "image/gif"
        except Exception:
            pass
    return _as_jpeg_or_original(data, max_edge)
