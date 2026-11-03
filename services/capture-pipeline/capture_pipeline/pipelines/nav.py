"""Ветка NAV после ``tee`` (фрагмент perception).

Контракт v1: 1280×720 @ 15 fps. Публикуется **один** формат: ``nav.encoding``
``raw`` (bgr8) или ``compressed`` (JPEG). На Jetson JPEG — ``nvjpegenc`` в NVMM,
на emulate — CPU ``jpegenc``. Топик базы ``/sensing/{ros_name}/image_raw``,
плюс ``camera_info``. QoS задаёт плагин (depth 1); свойств qos-* у rbfimagesink нет.
Не импортирует camera_service, RTSP, ABR.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from ..config import (
    DEFAULT_NAV_FPS,
    DEFAULT_NAV_HEIGHT,
    DEFAULT_NAV_WIDTH,
    CameraConfig,
    Config,
)

NAV_WIDTH = DEFAULT_NAV_WIDTH
NAV_HEIGHT = DEFAULT_NAV_HEIGHT
NAV_FPS = DEFAULT_NAV_FPS
NAV_QUEUE_MAX_BUFFERS = 1
logger = logging.getLogger(__name__)

_CAMERA_INFO_DIR = Path(__file__).resolve().parents[2] / "configs" / "camera_info"


@dataclass(frozen=True)
class NavSinks:
    kind: str
    encoding: str
    sink: str


def nav_image_topic(ros_name: str) -> str:
    return f"/sensing/{ros_name}/image_raw"


def nav_compressed_topic(ros_name: str) -> str:
    return f"{nav_image_topic(ros_name)}/compressed"


def nav_camera_info_topic(ros_name: str) -> str:
    return f"/sensing/{ros_name}/camera_info"


def nav_frame_id(ros_name: str) -> str:
    return f"{ros_name}_optical_frame"


def nav_node_name(ros_name: str) -> str:
    return f"zed_{ros_name}"


def require_nav_ros_name(camera: CameraConfig) -> str:
    if not camera.ros_name:
        raise ValueError(f"camera {camera.id}: nav_enabled requires ros_name")
    return camera.ros_name


def camera_info_stub_yaml(ros_name: str, width: int, height: int) -> str:
    """Заглушка CameraInfo: plumb_bob, D=0, fx≈ширина кадра."""
    fx = float(width)
    cx = width / 2.0
    cy = height / 2.0
    payload = {
        "image_width": width,
        "image_height": height,
        "camera_name": ros_name,
        "camera_matrix": {
            "rows": 3,
            "cols": 3,
            "data": [fx, 0.0, cx, 0.0, fx, cy, 0.0, 0.0, 1.0],
        },
        "distortion_model": "plumb_bob",
        "distortion_coefficients": {
            "rows": 1,
            "cols": 5,
            "data": [0.0, 0.0, 0.0, 0.0, 0.0],
        },
        "rectification_matrix": {
            "rows": 3,
            "cols": 3,
            "data": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        },
        "projection_matrix": {
            "rows": 3,
            "cols": 4,
            "data": [fx, 0.0, cx, 0.0, 0.0, fx, cy, 0.0, 0.0, 0.0, 1.0, 0.0],
        },
    }
    return yaml.safe_dump(payload, sort_keys=False)


def packaged_camera_info_path(ros_name: str) -> Path | None:
    extra = os.environ.get("ZED_XONEGS_CAMERA_INFO_DIR")
    candidates = []
    if extra:
        candidates.append(Path(extra) / f"{ros_name}.yaml")
    candidates.append(_CAMERA_INFO_DIR / f"{ros_name}.yaml")
    for path in candidates:
        if path.is_file():
            return path
    return None


def ensure_camera_info_file(ros_name: str, width: int, height: int) -> Path:
    packaged = packaged_camera_info_path(ros_name)
    if packaged is not None:
        return packaged.resolve()
    dest = (
        Path(os.environ.get("TMPDIR", "/tmp"))
        / "capture_pipeline"
        / "camera_info"
        / f"{ros_name}_{width}x{height}.yaml"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    text = camera_info_stub_yaml(ros_name, width, height)
    if not dest.is_file() or dest.read_text(encoding="utf-8") != text:
        dest.write_text(text, encoding="utf-8")
    return dest.resolve()


def _branch_queue() -> str:
    return (
        f"queue max-size-buffers={NAV_QUEUE_MAX_BUFFERS} "
        f"max-size-bytes=0 max-size-time=0 leaky=downstream"
    )


def _sink_queue() -> str:
    return _branch_queue()


def resolve_nav_kind(cfg: Config, gst: Any | None = None) -> str:
    """Выбрать rbfimagesink или fakesink. Без Gst-реестра ``auto`` → fakesink."""
    sink = cfg.nav.sink_type
    if sink == "auto":
        if gst is None:
            return "fakesink"
        found = gst.Registry.get().find_feature("rbfimagesink", gst.ElementFactory)
        return "rbfimagesink" if found else "fakesink"
    if sink not in ("rbfimagesink", "fakesink"):
        raise ValueError(
            f"nav.sink_type must be auto|rbfimagesink|fakesink (got {sink!r})"
        )
    return sink


def rbfimagesink_registered(gst: Any) -> bool:
    return bool(gst.Registry.get().find_feature("rbfimagesink", gst.ElementFactory))


def nvjpegenc_registered(gst: Any) -> bool:
    return bool(gst.Registry.get().find_feature("nvjpegenc", gst.ElementFactory))


def apply_nav_runtime(cfg: Config, gst: Any) -> Config:
    """GST-проверка NAV. Нет nvjpegenc → raw (контейнер не падает).
    Явный ``sink_type: rbfimagesink`` без плагина — ошибка. ``auto`` без
    плагина — fakesink и warning, процесс жив."""
    if not any(c.enabled and c.nav_enabled for c in cfg.cameras):
        return cfg
    if (
        cfg.is_jetson
        and cfg.nav.encoding == "compressed"
        and not nvjpegenc_registered(gst)
    ):
        logger.warning(
            "нет nvjpegenc (часто не линкуется libgstnvexifmeta.so) — "
            "NAV encoding=raw, топик тот же"
        )
        cfg = replace(cfg, nav=replace(cfg.nav, encoding="raw"))
    kind = resolve_nav_kind(cfg, gst)
    if kind == "rbfimagesink" and rbfimagesink_registered(gst):
        return cfg
    if cfg.nav.sink_type == "rbfimagesink":
        raise RuntimeError(
            'no GStreamer element "rbfimagesink". Kubernetes image builds it from '
            "third_party/gstreamer_ros_babel_fish (entrypoint prepends GST_PLUGIN_PATH). "
            "On the stand, source ROS in this shell: "
            "source /opt/ros/jazzy/setup.zsh && source ~/ros2_ws/install/setup.zsh "
            "(sets GST_PLUGIN_PATH and LD_LIBRARY_PATH). Then: make check-nav"
        )
    logger.warning(
        "rbfimagesink нет в GST registry — NAV fakesink, топиков не будет"
    )
    return cfg


def ensure_nav_gst_plugin(cfg: Config, gst: Any) -> Config:
    """Совместимость: фолбэки + ошибка, если явный rbfimagesink отсутствует."""
    return apply_nav_runtime(cfg, gst)


def _rbfimagesink(
    *,
    name: str,
    topic: str,
    frame_id: str,
    node_name: str,
    camera_info_url: str | None,
    prefer_compressed: bool,
) -> str:
    prefer = "true" if prefer_compressed else "false"
    parts = [
        f"rbfimagesink name={name} topic={topic} frame-id={frame_id} "
        f"node-name={node_name} sync=false async=false prefer-compressed={prefer}"
    ]
    if camera_info_url:
        parts.append(f'camera-info-url="{camera_info_url}"')
    return " ".join(parts)


def resolve_nav_sinks(
    cfg: Config, camera: CameraConfig, gst: Any | None = None
) -> NavSinks:
    ros_name = require_nav_ros_name(camera)
    kind = resolve_nav_kind(cfg, gst)
    encoding = cfg.nav.encoding
    sink_name = f"nav_sink_{camera.id}"
    if kind == "fakesink":
        return NavSinks(
            kind=kind,
            encoding=encoding,
            sink=f"fakesink name={sink_name} sync=false",
        )
    topic = nav_image_topic(ros_name)
    compressed = encoding == "compressed"
    info_path = ensure_camera_info_file(ros_name, cfg.nav.width, cfg.nav.height)
    return NavSinks(
        kind=kind,
        encoding=encoding,
        sink=_rbfimagesink(
            name=sink_name,
            topic=topic,
            frame_id=nav_frame_id(ros_name),
            node_name=nav_node_name(ros_name),
            camera_info_url=info_path.as_uri(),
            prefer_compressed=compressed,
        ),
    )


def resolve_nav_sink(cfg: Config, camera: CameraConfig, gst: Any | None = None) -> str:
    """Единственный NAV sink (tests / совместимость)."""
    return resolve_nav_sinks(cfg, camera, gst).sink


def build_nav_branch(
    cfg: Config,
    camera: CameraConfig,
    *,
    jetson: bool,
    gst: Any | None = None,
    sinks: NavSinks | None = None,
) -> str:
    """Фрагмент после ``t.``: queue → rate → (NVMM jpeg | BGR) → sink."""
    resolved = sinks if sinks is not None else resolve_nav_sinks(cfg, camera, gst)
    if jetson:
        prefix = _nav_branch_jetson(cfg, encoding=resolved.encoding)
    else:
        prefix = _nav_branch_cpu(cfg)
    tail = resolved.sink
    if resolved.encoding == "compressed":
        enc = "nvjpegenc" if jetson else "jpegenc"
        tail = f"{enc} quality={cfg.nav.jpeg_quality} ! {tail}"
    # На Jetson одна leaky-очередь в голове ветки. Вторая только копирует кадр.
    if jetson:
        return f"{prefix} ! {tail}"
    return f"{prefix} ! {_sink_queue()} ! {tail}"


def _input_wh(cfg: Config) -> tuple[int, int]:
    if not cfg.is_jetson:
        return cfg.video.width, cfg.video.height
    return cfg.video.sensor_width, cfg.video.sensor_height


def _nav_branch_cpu(cfg: Config) -> str:
    in_w, in_h = _input_wh(cfg)
    width, height, fps = cfg.nav.width, cfg.nav.height, cfg.nav.fps
    scale = ""
    if in_w != width or in_h != height:
        scale = f"! videoscale ! video/x-raw,width={width},height={height} "
    return (
        f"{_branch_queue()} "
        f"! videorate drop-only=true max-rate={fps} "
        f"{scale}"
        f"! videoconvert ! video/x-raw,format=BGR"
    )


def _nav_branch_jetson(cfg: Config, *, encoding: str) -> str:
    # Живой NVMM как teleop: только max-rate. Caps framerate=N/1 голодают sink.
    width, height, fps = cfg.nav.width, cfg.nav.height, cfg.nav.fps
    in_w, in_h = _input_wh(cfg)
    scale = in_w != width or in_h != height
    parts = [
        _branch_queue(),
        f"videorate drop-only=true max-rate={fps}",
    ]
    if encoding == "compressed":
        # NV12 уже подходит nvjpegenc; лишний nvvidconv — копия на VIC.
        if scale:
            parts.append("nvvidconv")
            parts.append(
                f"video/x-raw(memory:NVMM),format=NV12,width={width},height={height}"
            )
        return " ! ".join(parts)
    # Один hop VIC: NVMM NV12 → sysmem BGRx (масштаб в том же transform).
    parts.append("nvvidconv")
    if scale:
        parts.append(f"video/x-raw,format=BGRx,width={width},height={height}")
    else:
        parts.append("video/x-raw,format=BGRx")
    parts.append("videoconvert ! video/x-raw,format=BGR")
    return " ! ".join(parts)
