"""NAV branch launch strings — контракт 1280×720@15, без teleop/source."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from capture_pipeline.config import load_config
from capture_pipeline.pipelines.nav import (
    NAV_FPS,
    NAV_HEIGHT,
    NAV_WIDTH,
    build_nav_branch,
    camera_info_stub_yaml,
    ensure_camera_info_file,
    ensure_nav_gst_plugin,
    nav_camera_info_topic,
    nav_compressed_topic,
    nav_frame_id,
    nav_image_topic,
    resolve_nav_sink,
    resolve_nav_sinks,
)

ROOT = Path(__file__).resolve().parents[1]


def _nav_cam(cfg):
    return replace(cfg.cameras[0], ros_name="front_camera", nav_enabled=True)


def test_nav_constants_match_contract() -> None:
    assert NAV_WIDTH == 1280
    assert NAV_HEIGHT == 720
    assert NAV_FPS == 15
    assert nav_image_topic("front_camera") == "/sensing/front_camera/image_raw"
    assert (
        nav_compressed_topic("front_camera")
        == "/sensing/front_camera/image_raw/compressed"
    )
    assert nav_camera_info_topic("front_camera") == "/sensing/front_camera/camera_info"
    assert nav_frame_id("front_camera") == "front_camera_optical_frame"


def test_nav_branch_has_no_camera_or_encoder() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    cam = _nav_cam(cfg)
    sinks = resolve_nav_sinks(cfg, cam, gst=None)
    launch = build_nav_branch(cfg, cam, jetson=True, sinks=sinks)
    assert "nvarguscamerasrc" not in launch
    assert "enc0" not in launch
    assert "shmsink" not in launch
    assert "rbfimagesink" not in launch
    assert "/camera/nav/image_raw" not in launch
    assert "fakesink name=nav_sink_cam0" in launch
    assert "nav_jpeg_sink" not in launch
    assert "nvjpegenc quality=100" in launch
    assert "format=BGR" not in launch
    assert "prefer-compressed" not in launch
    assert f"width={NAV_WIDTH}" in launch
    assert f"height={NAV_HEIGHT}" in launch
    assert f"max-rate={NAV_FPS}" in launch
    assert f"framerate={NAV_FPS}/1" not in launch
    assert "leaky=downstream" in launch


def test_nav_cpu_branch_is_bgr() -> None:
    cfg = load_config(ROOT / "configs" / "laptop-emulate.yaml")
    cam = _nav_cam(cfg)
    sinks = resolve_nav_sinks(cfg, cam, gst=None)
    launch = build_nav_branch(cfg, cam, jetson=False, sinks=sinks)
    assert "video/x-raw,format=BGR" in launch
    assert f"max-rate={NAV_FPS}" in launch
    assert "jpegenc quality=80" in launch
    assert "nvjpegenc" not in launch
    assert "nvv4l2h264enc" not in launch
    assert "x264enc" not in launch


def test_nav_rbf_topics_use_ros_name() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    cam = _nav_cam(cfg)
    cfg = replace(cfg, nav=replace(cfg.nav, sink_type="rbfimagesink"))
    sinks = resolve_nav_sinks(cfg, cam, gst=None)
    launch = build_nav_branch(cfg, cam, jetson=True, sinks=sinks)
    assert "/camera/nav/image_raw" not in launch
    assert "/sensing/front_camera/image_raw" in launch
    assert "prefer-compressed=true" in launch
    assert "nvjpegenc quality=100" in launch
    assert "front_camera_optical_frame" in launch
    assert "camera-info-url=" in launch
    assert "node-name=zed_front_camera" in launch
    assert "name=nav_sink_cam0" in launch
    assert "nav_jpeg_sink" not in launch
    assert "navt_cam0" not in launch


def test_nav_auto_uses_rbfimagesink_when_registered() -> None:
    class Registry:
        @staticmethod
        def get():
            return Registry()

        def find_feature(self, name, _factory):
            return object() if name == "rbfimagesink" else None

    class Gst:
        pass

    Gst.Registry = Registry
    Gst.ElementFactory = object()

    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    cam = _nav_cam(cfg)
    raw = resolve_nav_sink(cfg, cam, Gst)
    assert raw.startswith("rbfimagesink name=nav_sink_cam0")
    assert "topic=/sensing/front_camera/image_raw" in raw
    assert "frame-id=front_camera_optical_frame" in raw
    assert "camera-info-url=" in raw
    assert "prefer-compressed=true" in raw


def test_nav_raw_encoding_skips_jpegenc() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    cam = _nav_cam(cfg)
    cfg = replace(cfg, nav=replace(cfg.nav, sink_type="rbfimagesink", encoding="raw"))
    launch = build_nav_branch(cfg, cam, jetson=True)
    assert "jpegenc" not in launch
    assert "nvjpegenc" not in launch
    assert "video/x-raw,format=BGR" in launch
    assert "prefer-compressed=false" in launch
    assert "prefer-compressed=true" not in launch
    assert "camera-info-url=" in launch
    assert launch.count("nvvidconv") == 1
    assert launch.count("queue ") == 1
    assert f"format=BGRx,width={NAV_WIDTH},height={NAV_HEIGHT}" in launch
    assert "memory:NVMM" not in launch


def test_nav_fullres_raw_one_nvvidconv() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-duo-one-s.yaml")
    cam = _nav_cam(cfg)
    cfg = replace(cfg, nav=replace(cfg.nav, encoding="raw"))
    launch = build_nav_branch(cfg, cam, jetson=True)
    assert launch.count("nvvidconv") == 1
    assert launch.count("queue ") == 1
    assert "video/x-raw,format=BGRx" in launch
    assert "width=" not in launch.split("nvvidconv", 1)[1].split("videoconvert", 1)[0]
    assert "video/x-raw,format=BGR" in launch
    assert "memory:NVMM" not in launch
    assert "nvjpegenc" not in launch


def test_nav_fullres_compressed_skips_nvvidconv() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-duo-one-s.yaml")
    cfg = replace(cfg, nav=replace(cfg.nav, encoding="compressed"))
    cam = _nav_cam(cfg)
    launch = build_nav_branch(
        cfg, cam, jetson=True, sinks=resolve_nav_sinks(cfg, cam, gst=None)
    )
    assert "nvvidconv" not in launch
    assert "nvjpegenc quality=100" in launch
    assert launch.count("queue ") == 1


def test_nav_scaled_compressed_keeps_nvvidconv() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    cam = _nav_cam(cfg)
    launch = build_nav_branch(
        cfg, cam, jetson=True, sinks=resolve_nav_sinks(cfg, cam, gst=None)
    )
    assert "nvvidconv" in launch
    assert f"width={NAV_WIDTH}" in launch
    assert f"height={NAV_HEIGHT}" in launch
    assert launch.count("queue ") == 1


def test_ensure_nav_gst_plugin_errors_when_rbf_missing() -> None:
    class Registry:
        @staticmethod
        def get():
            return Registry()

        def find_feature(self, name, *_args):
            return object() if name == "nvjpegenc" else None

    class Gst:
        pass

    Gst.Registry = Registry
    Gst.ElementFactory = object()
    cfg = load_config(ROOT / "configs" / "jetson-duo-one-s.yaml")
    try:
        ensure_nav_gst_plugin(cfg, Gst)
    except RuntimeError as exc:
        assert "rbfimagesink" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_ensure_nav_gst_plugin_falls_back_to_raw_without_nvjpegenc() -> None:
    class Registry:
        @staticmethod
        def get():
            return Registry()

        def find_feature(self, name, *_args):
            return object() if name == "rbfimagesink" else None

    class Gst:
        pass

    Gst.Registry = Registry
    Gst.ElementFactory = object()
    cfg = load_config(ROOT / "configs" / "jetson-duo-one-s.yaml")
    cfg = replace(cfg, nav=replace(cfg.nav, encoding="compressed"))
    out = ensure_nav_gst_plugin(cfg, Gst)
    assert out.nav.encoding == "raw"


def test_ensure_nav_gst_plugin_skips_teleop_only() -> None:
    class Gst:
        pass

    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    ensure_nav_gst_plugin(cfg, Gst)


def test_nav_uses_normalized_nv12_caps() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    cfg = replace(cfg, video=replace(cfg.video, nvmm_format="I420"))
    cam = _nav_cam(cfg)
    launch = build_nav_branch(
        cfg,
        cam,
        jetson=True,
        sinks=resolve_nav_sinks(cfg, cam, gst=None),
    )
    assert "format=NV12" in launch
    assert "format=I420" not in launch


def test_camera_info_stub_is_plumb_bob() -> None:
    text = camera_info_stub_yaml("front_camera", 1280, 720)
    assert "distortion_model: plumb_bob" in text
    assert "camera_name: front_camera" in text
    assert "image_width: 1280" in text
    path = ensure_camera_info_file("front_camera", 1280, 720)
    assert path.is_file()
    assert "plumb_bob" in path.read_text(encoding="utf-8")
