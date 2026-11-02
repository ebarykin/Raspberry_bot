"""Brown undistort maps (GStreamer не требуется)."""

from __future__ import annotations

import numpy as np

from capture_pipeline.pipelines.undistort import build_maps, remap_bgrx


def test_build_maps_shape_and_center() -> None:
    map_x, map_y = build_maps(640, 400, k1=-0.18, k2=0.0, crop=0.92)
    assert map_x.shape == (400, 640)
    assert map_y.shape == (400, 640)
    assert map_x.dtype == np.float32
    cx, cy = 319.5, 199.5
    assert abs(float(map_x[200, 320]) - cx) < 3.0
    assert abs(float(map_y[200, 320]) - cy) < 3.0


def test_negative_k1_samples_inward() -> None:
    map_x, _ = build_maps(640, 400, k1=-0.18, k2=0.0, crop=1.0)
    sampled = float(map_x[200, 600])
    assert sampled < 600.0


def test_build_maps_rejects_bad_crop() -> None:
    try:
        build_maps(640, 400, k1=-0.18, crop=0.0)
    except ValueError as exc:
        assert "crop" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_remap_bgrx_preserves_shape() -> None:
    maps = build_maps(32, 24, k1=-0.18, crop=1.0)
    frame = np.zeros((24, 32, 4), dtype=np.uint8)
    frame[12, 16] = (1, 2, 3, 255)
    out = remap_bgrx(frame, maps)
    assert out.shape == frame.shape
    assert out.dtype == np.uint8
