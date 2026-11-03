"""Источник кадра: v4l2 / nvv4l2 / Argus / libcamera / videotestsrc → raw caps.

NAV и teleop не знают модель камеры. После адаптера Jetson — NVMM NV12
в размере сенсора; emulate и rpi — CPU raw @ video.width/height.
"""

from __future__ import annotations

from ..config import (
    SOURCE_TYPE_ARGUS,
    SOURCE_TYPE_LIBCAMERA,
    SOURCE_TYPE_NVV4L2,
    SOURCE_TYPE_TEST,
    SOURCE_TYPE_V4L2,
    CameraConfig,
    Config,
)


def build_source(cfg: Config, camera: CameraConfig) -> str:
    """Фрагмент launch до ``tee`` (сам tee не входит)."""
    kind = camera.source_type
    if kind == SOURCE_TYPE_ARGUS:
        return _source_argus(cfg, camera)
    if kind == SOURCE_TYPE_V4L2:
        return _source_v4l2(cfg, camera)
    if kind == SOURCE_TYPE_NVV4L2:
        return _source_nvv4l2(cfg, camera)
    if kind == SOURCE_TYPE_LIBCAMERA:
        return _source_libcamera(cfg, camera)
    if kind == SOURCE_TYPE_TEST:
        return _source_test(cfg, camera)
    raise ValueError(f"unsupported source_type: {kind!r}")


def _sensor_wh(cfg: Config) -> tuple[int, int]:
    if not cfg.is_jetson:
        return cfg.video.width, cfg.video.height
    return cfg.video.sensor_width, cfg.video.sensor_height


def _nvmm_caps(cfg: Config, *, width: int, height: int) -> str:
    v = cfg.video
    return (
        f"video/x-raw(memory:NVMM),format=NV12,width={width},"
        f"height={height},framerate={v.fps}/1"
    )


def _source_argus(cfg: Config, camera: CameraConfig) -> str:
    v = cfg.video
    w, h = _sensor_wh(cfg)
    return (
        f"nvarguscamerasrc sensor-id={camera.sensor_id} "
        f"! video/x-raw(memory:NVMM),format={v.nvmm_format},"
        f"width={w},height={h},framerate={v.fps}/1 "
        f"! nvvidconv flip-method={camera.flip_method} "
        f"! {_nvmm_caps(cfg, width=w, height=h)}"
    )


def _source_v4l2(cfg: Config, camera: CameraConfig) -> str:
    v = cfg.video
    w, h = _sensor_wh(cfg)
    pix = camera.pixel_format or "UYVY"
    device = camera.device or "/dev/video0"
    raw = (
        f"v4l2src device={device} do-timestamp=true "
        f"! video/x-raw,format={pix},width={w},height={h},framerate={v.fps}/1"
    )
    if not cfg.is_jetson:
        return (
            f"{raw} ! videoconvert ! videoscale "
            f"! video/x-raw,format=I420,width={w},height={h},framerate={v.fps}/1"
        )
    return (
        f"{raw} ! nvvidconv flip-method={camera.flip_method} "
        f"! {_nvmm_caps(cfg, width=w, height=h)}"
    )


def _source_nvv4l2(cfg: Config, camera: CameraConfig) -> str:
    """V4L2 сразу в NVMM (L4T nvv4l2camerasrc), без CPU-копии как у v4l2src."""
    v = cfg.video
    w, h = _sensor_wh(cfg)
    pix = camera.pixel_format or "UYVY"
    device = camera.device or "/dev/video0"
    return (
        f"nvv4l2camerasrc device={device} "
        f"! video/x-raw(memory:NVMM),format={pix},width={w},"
        f"height={h},framerate={v.fps}/1 "
        f"! nvvidconv flip-method={camera.flip_method} "
        f"! {_nvmm_caps(cfg, width=w, height=h)}"
    )


def _source_test(cfg: Config, camera: CameraConfig) -> str:
    v = cfg.video
    w, h = _sensor_wh(cfg)
    head = (
        f"videotestsrc is-live=true pattern={camera.pattern} do-timestamp=true "
        f"! video/x-raw,width={w},height={h},framerate={v.fps}/1"
    )
    if not cfg.is_jetson:
        return head
    return (
        f"{head} ! nvvidconv flip-method={camera.flip_method} "
        f"! {_nvmm_caps(cfg, width=w, height=h)}"
    )


def _source_libcamera(cfg: Config, camera: CameraConfig) -> str:
    """Pi CSI через libcamerasrc (Pi 5 PiSP, NV12)."""
    v = cfg.video
    w, h = _sensor_wh(cfg)
    pix = camera.pixel_format or "NV12"
    name = camera.camera_name or ""
    return (
        f'libcamerasrc camera-name="{name}" name=cam0 ! '
        f"video/x-raw,format={pix},width={w},height={h},framerate={v.fps}/1"
    )
