"""Склейка capture + source adapter (GStreamer не требуется)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from capture_pipeline.config import load_config
from capture_pipeline.pipelines import (
    build_capture,
    build_rtsp_appsrc,
    jetson_vbv_size,
)

ROOT = Path(__file__).resolve().parents[1]


def test_jetson_default_is_argus_teleop_only() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    cam = cfg.cameras[0]
    assert cam.source_type == "nvarguscamerasrc"
    assert cam.nav_enabled is False
    launch = build_capture(cfg, cam)
    assert "nvarguscamerasrc" in launch
    assert "name=rate0" in launch
    assert "enc0" in launch
    assert "appsink name=encoded_sink" in launch
    assert "rbfimagesink" not in launch
    assert "textoverlay name=clock0" not in launch
    assert "cairooverlay name=park0" in launch
    assert "identity name=undist0" not in launch
    assert "clockoverlay" not in launch
    assert cfg.overlay.clock is False
    assert cam.parktronic is True
    assert cam.undistort is False
    assert all(not c.parktronic for c in cfg.cameras[1:])
    assert all(not c.undistort for c in cfg.cameras[1:])
    rear = build_capture(cfg, cfg.cameras[1])
    assert "cairooverlay name=park0" not in rear
    assert "identity name=undist0" not in rear
    assert "t. !" in launch
    assert "nav_sink" not in launch
    assert "rtph264pay" not in launch


def test_emulate_has_overlay_and_appsink() -> None:
    cfg = load_config(ROOT / "configs" / "laptop-emulate.yaml")
    launch = build_capture(cfg, cfg.cameras[0], profile_label="good")
    assert "videotestsrc" in launch
    assert "x264enc name=enc0" in launch
    assert "bitrate=1100" in launch
    assert "abr good" in launch
    assert "appsink name=encoded_sink" in launch
    assert "nvarguscamerasrc" not in launch


def test_v4l2_slot_does_not_use_argus() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    cam = replace(
        cfg.cameras[0],
        source_type="v4l2src",
        device="/dev/video0",
        pixel_format="UYVY",
    )
    launch = build_capture(cfg, cam)
    assert "v4l2src device=/dev/video0" in launch
    assert "UYVY" in launch
    assert "nvarguscamerasrc" not in launch
    assert "nvvidconv" in launch
    assert "enc0" in launch


def test_undistort_opens_bgrx_hop_without_hud() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    cfg = replace(
        cfg,
        overlay=replace(cfg.overlay, clock=False),
        cameras=(replace(cfg.cameras[0], parktronic=False, undistort=True),),
    )
    launch = build_capture(cfg, cfg.cameras[0])
    assert "video/x-raw,format=BGRx" in launch
    assert "identity name=undist0" in launch
    assert "cairooverlay name=park0" not in launch
    assert "textoverlay name=clock0" not in launch


def test_nav_enabled_adds_branch() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    cam = replace(cfg.cameras[0], nav_enabled=True, ros_name="front_camera")
    launch = build_capture(cfg, cam)
    assert "nav_sink" in launch
    assert "1280" in launch
    assert "720" in launch
    assert "appsink name=encoded_sink" in launch


def test_appsrc_factory_has_bounded_queue_and_no_parser() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-duo-one-s.yaml")
    launch = build_capture(cfg, cfg.cameras[0], fps=30)
    pay = build_rtsp_appsrc(30)
    assert launch.startswith("nvarguscamerasrc sensor-id=0")
    assert "nvv4l2h264enc name=enc0" in launch
    assert "textoverlay name=clock0" in launch
    assert "cairooverlay name=park0" in launch
    assert "identity name=undist0" not in launch
    assert "appsink name=encoded_sink" in launch
    assert launch.count("h264parse") == 1
    assert "shmsink" not in launch
    assert "shmsrc" not in launch
    assert "appsrc name=encoded_src" in pay
    assert "max-buffers=1" in pay
    assert "max-bytes=0" in pay
    assert "max-time=0" in pay
    assert "leaky-type=downstream" in pay
    assert "min-latency=0" in pay
    assert "max-latency=33333333" in pay
    assert "rtph264pay name=pay0" in pay
    assert "h264parse" not in pay


def test_vbv_follows_abr_horizon() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    assert jetson_vbv_size(cfg.encoder, 1_200_000) == 240_000
    launch = build_capture(
        cfg, cfg.cameras[0], bitrate_bps=300_000, fps=12, profile_label="critical"
    )
    assert "bitrate=300000" in launch
    assert "vbv-size=60000" in launch
    assert "iframeinterval=4" in launch
