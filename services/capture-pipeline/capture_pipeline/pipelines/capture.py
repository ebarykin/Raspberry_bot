"""Склейка capture: source → tee → (NAV?) + teleop."""

from __future__ import annotations

from typing import Any

from ..config import DEFAULT_PEAK_BITRATE_RATIO, CameraConfig, Config
from .nav import build_nav_branch
from .source import build_source
from .teleop import build_teleop_branch


def build_capture(
    cfg: Config,
    camera: CameraConfig,
    *,
    gst: Any | None = None,
    bitrate_bps: int | None = None,
    peak_bitrate_bps: int | None = None,
    fps: int | None = None,
    profile_label: str | None = None,
) -> str:
    """Полный capture launch (без RTSP pay)."""
    active = cfg.abr.profiles[cfg.abr.default_profile]
    bps = bitrate_bps if bitrate_bps is not None else active.bitrate_bps
    peak = (
        peak_bitrate_bps
        if peak_bitrate_bps is not None
        else max(bps, int(bps * DEFAULT_PEAK_BITRATE_RATIO))
    )
    out_fps = fps if fps is not None else active.fps
    label = profile_label or cfg.abr.default_profile
    jetson = cfg.is_jetson
    src = build_source(cfg, camera)
    teleop = build_teleop_branch(
        cfg,
        jetson=jetson,
        bitrate_bps=bps,
        peak_bitrate_bps=peak,
        fps=out_fps,
        profile_label=label,
        parktronic=camera.parktronic,
        undistort=camera.undistort,
    )
    parts = [f"{src} ! tee name=t allow-not-linked=true"]
    if camera.nav_enabled:
        nav = build_nav_branch(cfg, camera, jetson=jetson, gst=gst)
        parts.append(f"t. ! {nav}")
    parts.append(f"t. ! {teleop}")
    return " ".join(parts)
