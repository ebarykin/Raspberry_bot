"""Карты Brown–Conrady для teleop 640×H BGRx.

Карты один раз на слот, в live только remap. Тот же Wide FOV, что у
пинхол-парктроника — иначе метры HUD разъедутся после коррекции.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

UNDIST_ELEMENT_NAME = "undist0"
# ZED X One S Wide 2.2 мм — тот же дефолтный FOV, что cameras[].parktronic.
_HFOV_DEG = 110.0
_VFOV_DEG = 79.6
_GST_LIB: Any = None


def build_maps(
    width: int,
    height: int,
    *,
    k1: float,
    k2: float = 0.0,
    crop: float = 1.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Карты output-пиксель → искажённый источник (float32, H×W).

    Предпочтительно OpenCV ``initUndistortRectifyMap`` с ``alpha=0``
    (каждый выходной пиксель валиден). ``crop`` — доп. зум (1.0 = без зума).
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"undistort size must be positive, got {width}x{height}")
    if not 0.0 < crop <= 1.0:
        raise ValueError(f"undistort crop must be in (0, 1], got {crop}")
    w = int(width)
    h = int(height)
    fx = (w / 2.0) / math.tan(math.radians(_HFOV_DEG / 2.0))
    fy = (h / 2.0) / math.tan(math.radians(_VFOV_DEG / 2.0))
    cx = (w - 1) * 0.5
    cy = (h - 1) * 0.5
    try:
        import cv2

        camera = np.array(
            [[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]], dtype=np.float64
        )
        dist = np.array([k1, k2, 0.0, 0.0], dtype=np.float64)
        new_k, _roi = cv2.getOptimalNewCameraMatrix(camera, dist, (w, h), 0.0)
        new_k = new_k.copy()
        new_k[0, 0] /= crop
        new_k[1, 1] /= crop
        return cv2.initUndistortRectifyMap(
            camera, dist, None, new_k, (w, h), cv2.CV_32FC1
        )
    except ImportError:
        return _build_maps_numpy(w, h, fx, fy, cx, cy, k1, k2, crop)


def _build_maps_numpy(
    w: int,
    h: int,
    fx: float,
    fy: float,
    cx: float,
    cy: float,
    k1: float,
    k2: float,
    crop: float,
) -> tuple[np.ndarray, np.ndarray]:
    fx_out = fx / crop
    fy_out = fy / crop
    grid_u, grid_v = np.meshgrid(
        np.arange(w, dtype=np.float64),
        np.arange(h, dtype=np.float64),
    )
    x = (grid_u - cx) / fx_out
    y = (grid_v - cy) / fy_out
    r2 = x * x + y * y
    radial = 1.0 + k1 * r2 + k2 * r2 * r2
    map_x = (fx * x * radial + cx).astype(np.float32)
    map_y = (fy * y * radial + cy).astype(np.float32)
    return map_x, map_y


def remap_bgrx(frame: np.ndarray, maps: tuple[np.ndarray, np.ndarray]) -> np.ndarray:
    """Remap упакованного BGRx (H×W×4 uint8). Сначала OpenCV, иначе bilinear NumPy."""
    map_x, map_y = maps
    try:
        import cv2

        return cv2.remap(
            frame,
            map_x,
            map_y,
            interpolation=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
        )
    except ImportError:
        return _remap_numpy(frame, map_x, map_y)


def remap_gst_buffer(
    buf: Any,
    maps: tuple[np.ndarray, np.ndarray],
    width: int,
    height: int,
    gst: Any,
    info: Any | None = None,
) -> None:
    """Undistort BGRx GstBuffer и подменить его в pad probe.

    На L4T pygobject отдаёт ``MapInfo.data`` как read-only bytes, live-буфер
    не writable, ``info.data = Gst.Buffer`` отклоняется (gpointer). Собираем
    новый буфер и кладём в ``info.data`` C-указатель после extra mini-object ref.
    """
    if info is None:
        return
    expected = height * width * 4
    if buf.get_size() < expected:
        return
    raw = buf.extract_dup(0, expected)
    if raw is None or len(raw) < expected:
        return
    frame = np.frombuffer(raw, dtype=np.uint8, count=expected).reshape(
        (height, width, 4)
    )
    remapped = np.ascontiguousarray(remap_bgrx(frame, maps))
    if remapped.size != expected:
        return
    # new_memdup(ndarray) на L4T GI разворачивает в список из ~1M int (~100 мс/кадр).
    wrapped = gst.Buffer.new_memdup(remapped.tobytes())
    if wrapped is None:
        return
    wrapped.pts = buf.pts
    wrapped.dts = buf.dts
    wrapped.duration = buf.duration
    wrapped.offset = buf.offset
    wrapped.offset_end = buf.offset_end
    ptr = _boxed_c_ptr(wrapped)
    _gst_mini_object_ref(ptr)
    info.data = ptr


def _boxed_c_ptr(obj: Any) -> int:
    text = repr(obj)
    left = text.rfind(" at ")
    right = text.rfind(")")
    if left < 0 or right <= left:
        raise RuntimeError(f"cannot parse C pointer from {text!r}")
    return int(text[left + 4 : right], 16)


def _gst_mini_object_ref(ptr: int) -> None:
    import ctypes

    global _GST_LIB
    lib = _GST_LIB
    if lib is None:
        lib = ctypes.CDLL("libgstreamer-1.0.so.0")
        lib.gst_mini_object_ref.argtypes = [ctypes.c_void_p]
        lib.gst_mini_object_ref.restype = ctypes.c_void_p
        _GST_LIB = lib
    lib.gst_mini_object_ref(ptr)


def _remap_numpy(frame: np.ndarray, map_x: np.ndarray, map_y: np.ndarray) -> np.ndarray:
    h, w = frame.shape[:2]
    x = np.clip(map_x, 0.0, w - 1.001)
    y = np.clip(map_y, 0.0, h - 1.001)
    x0 = np.floor(x).astype(np.int32)
    y0 = np.floor(y).astype(np.int32)
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    wx = (x - x0)[..., None]
    wy = (y - y0)[..., None]
    ia = frame[y0, x0].astype(np.float32)
    ib = frame[y0, x1].astype(np.float32)
    ic = frame[y1, x0].astype(np.float32)
    id_ = frame[y1, x1].astype(np.float32)
    top = ia * (1.0 - wx) + ib * wx
    bot = ic * (1.0 - wx) + id_ * wx
    return (top * (1.0 - wy) + bot * wy).astype(np.uint8)
