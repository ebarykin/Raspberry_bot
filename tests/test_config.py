"""Unit-тесты конфига и launch-строк (GStreamer не требуется)."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from capture_pipeline.config import (  # noqa: E402
    Config,
    ParktronicOverlayConfig,
    UndistortConfig,
    load_config,
)
from capture_pipeline.pipelines import build_capture, jetson_vbv_size  # noqa: E402

_ABR = {
    "profiles": {
        "good": {"fps": 30, "bitrate_kbps": 1100},
        "normal": {"fps": 30, "bitrate_kbps": 700},
        "degraded": {"fps": 24, "bitrate_kbps": 500},
        "bad": {"fps": 18, "bitrate_kbps": 350},
        "critical": {"fps": 12, "bitrate_kbps": 250},
    }
}
_JETSON_VIDEO = {
    "sensor_width": 1920,
    "sensor_height": 1200,
    "width": 640,
    "height": 480,
    "fps": 30,
}
_JETSON_ENC = {
    "control_rate": 1,
    "preset_level": 1,
    "gop_s": 0.33,
    "vbv_horizon_s": 0.2,
}


def test_jetson_quad_yaml_loads() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    assert cfg.source == "jetson"
    assert cfg.rtsp.suspend_mode == "none"
    assert cfg.control_ws.port == 8591
    assert cfg.control_ws.host == "0.0.0.0"
    assert cfg.abr.default_profile == "good"
    assert cfg.abr.profiles["good"].bitrate_kbps == 1200
    assert cfg.abr.profiles["degraded"].bitrate_kbps == 600
    assert cfg.abr.profiles["critical"].fps == 12
    launch = build_capture(cfg, cfg.cameras[0])
    assert "nvarguscamerasrc" in launch
    assert "videorate" in launch
    assert "name=rate0" in launch
    assert "bitrate=1200000" in launch
    assert "vbv-size=240000" in launch
    assert "iframeinterval=10" in launch
    assert 'qp-range="0,51:0,51:0,51"' in launch
    assert cfg.encoder.qp_range == "0,51:0,51:0,51"
    assert cfg.encoder.vbv_size == 0
    assert cfg.encoder.vbv_horizon_s == 0.2
    assert cfg.encoder.gop_s == 0.33
    assert cfg.capture.stagger_ms == 200
    assert cfg.cameras[0].source_type == "nvarguscamerasrc"
    assert cfg.cameras[0].nav_enabled is False
    assert cfg.cameras[0].parktronic is True
    assert cfg.cameras[0].parktronic_overlay is not None
    assert cfg.cameras[0].parktronic_overlay.height_m == 0.80
    assert cfg.cameras[0].parktronic_overlay.fill_style == "edge"
    assert cfg.cameras[0].undistort is False
    assert cfg.cameras[0].undistort_params is None
    assert all(not c.parktronic for c in cfg.cameras[1:])
    assert all(not c.undistort for c in cfg.cameras[1:])
    assert cfg.nav.sink_type == "auto"
    assert cfg.nav.encoding == "compressed"
    assert cfg.nav.jpeg_quality == 100


def test_runtime_and_shipped_configs_are_appsrc_only() -> None:
    forbidden_pipeline_tokens = ("shmsink", "shmsrc", "shm_socket", "rtsp_mode")
    for path in sorted((ROOT / "capture_pipeline").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for token in forbidden_pipeline_tokens:
            assert token not in text, f"{token} remains in {path}"
    for path in (
        ROOT / "configs" / "jetson-quad.yaml",
        ROOT / "configs" / "jetson-duo-one-s.yaml",
        ROOT / "configs" / "jetson-delta.yaml",
        ROOT / "configs" / "laptop-emulate.yaml",
    ):
        text = path.read_text(encoding="utf-8")
        assert "\n  mode:" not in text
        assert "always_on:" not in text
        assert "iframe_interval:" not in text
        assert "bitrate_bps:" not in text
        assert "peak_bitrate_bps:" not in text
        assert "level_map:" not in text


def test_jetson_duo_one_s_yaml_loads() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-duo-one-s.yaml")
    assert cfg.source == "jetson"
    assert cfg.rtsp.suspend_mode == "none"
    assert cfg.control_ws.host == "0.0.0.0"
    assert cfg.control_ws.port == 8591
    assert cfg.video.sensor_width == 1920
    assert cfg.video.sensor_height == 1200
    assert cfg.video.width == 640
    assert cfg.video.height == 400
    assert cfg.video.fps == 60
    assert cfg.abr.default_profile == "good"
    assert len(cfg.cameras) == 1
    cam = cfg.cameras[0]
    assert cam.id == "cam0"
    assert cam.sensor_id == 0
    assert cam.rtsp_port == 8586
    assert cam.enabled is True
    assert cam.flip_method == 0
    assert cam.source_type == "nvarguscamerasrc"
    assert cam.ros_name == "front_camera"
    assert cam.nav_enabled is True
    assert cfg.nav.sink_type == "rbfimagesink"
    assert cfg.nav.encoding == "raw"
    assert cfg.nav.jpeg_quality == 100
    assert cfg.nav.width == 1920
    assert cfg.nav.height == 1200
    assert cam.parktronic is True
    assert cam.parktronic_overlay is not None
    assert cam.parktronic_overlay.height_m == 0.80
    assert cam.parktronic_overlay.fill_style == "edge"
    assert cam.parktronic_overlay.rings_m == (1.0, 2.0)
    assert cam.undistort is False
    assert cam.undistort_params is None
    good = cfg.abr.profiles[cfg.abr.default_profile]
    launch = build_capture(cfg, cam, fps=good.fps, bitrate_bps=good.bitrate_bps)
    assert "nvarguscamerasrc sensor-id=0" in launch
    assert "bitrate=1100000" in launch
    assert "vbv-size=55000" in launch
    assert "iframeinterval=15" in launch
    assert "idrinterval=15" in launch
    assert "insert-aud=true" in launch
    assert "control-rate=0" in launch
    assert "preset-level=2" in launch
    assert "appsink name=encoded_sink" in launch
    assert "max-buffers=1 drop=true" in launch
    assert "shmsink" not in launch
    assert "textoverlay name=clock0" in launch
    assert "cairooverlay name=park0" in launch
    assert "identity name=undist0" not in launch
    assert "clockoverlay" not in launch
    assert cfg.overlay.time_format == "%H:%M:%S.%f"
    assert not hasattr(cfg.overlay, "parktronic")
    assert cfg.encoder.vbv_horizon_s == 0.05
    assert cfg.encoder.gop_s == 0.5
    assert cfg.encoder.preset_level == 2
    assert cfg.overlay.clock is True


def test_laptop_emulate_yaml_loads() -> None:
    cfg = load_config(ROOT / "configs" / "laptop-emulate.yaml")
    assert cfg.is_emulate
    assert cfg.rtsp.suspend_mode == "reset"
    assert cfg.cameras[0].pattern == "smpte"
    launch = build_capture(cfg, cfg.cameras[0], profile_label="good")
    assert "videotestsrc" in launch
    assert "videorate" in launch
    assert "x264enc name=enc0" in launch
    assert "bitrate=1100" in launch
    assert "abr good" in launch


def test_critical_profile_values() -> None:
    cfg = load_config(ROOT / "configs" / "laptop-emulate.yaml")
    crit = cfg.abr.profiles["critical"]
    assert crit.fps == 12
    assert crit.bitrate_kbps == 250
    assert crit.bitrate_bps == 250_000


def test_degraded_profile_values() -> None:
    cfg = load_config(ROOT / "configs" / "laptop-emulate.yaml")
    degraded = cfg.abr.profiles["degraded"]
    assert degraded.fps == 24
    assert degraded.bitrate_kbps == 500
    assert degraded.bitrate_bps == 500_000


def test_jetson_launch_vbv_follows_abr_horizon() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    assert jetson_vbv_size(cfg.encoder, 1_200_000) == 240_000
    launch = build_capture(
        cfg, cfg.cameras[0], bitrate_bps=300_000, fps=12, profile_label="critical"
    )
    assert "bitrate=300000" in launch
    assert "vbv-size=60000" in launch
    assert "iframeinterval=4" in launch
    assert 'qp-range="0,51:0,51:0,51"' in launch


def test_rejects_missing_abr_profiles() -> None:
    raw = {
        "source": "emulate",
        "cameras": [{"id": "cam0"}],
    }
    with pytest.raises(ValueError, match="abr.profiles"):
        Config.from_dict(raw)


def test_rejects_non_positive_vbv_horizon() -> None:
    raw = {
        "source": "jetson",
        "rtsp": {"host": "0.0.0.0"},
        "control_ws": {"host": "0.0.0.0"},
        "abr": _ABR,
        "video": _JETSON_VIDEO,
        "encoder": {**_JETSON_ENC, "vbv_horizon_s": 0},
        "cameras": [{"id": "cam0", "sensor_id": 0, "rtsp_port": 8586, "mount": "/live"}],
    }
    with pytest.raises(ValueError, match="vbv_horizon_s"):
        Config.from_dict(raw)


def test_rejects_removed_rtsp_mode() -> None:
    raw = {
        "source": "emulate",
        "rtsp": {"mode": "magic"},
        "cameras": [{"id": "cam0"}],
    }
    with pytest.raises(ValueError, match="rtsp.mode"):
        Config.from_dict(raw)


def test_rejects_removed_duplicate_encoder_keys() -> None:
    raw = {
        "source": "emulate",
        "encoder": {"bitrate_bps": 900_000},
        "cameras": [{"id": "cam0"}],
    }
    with pytest.raises(ValueError, match="come from ABR"):
        Config.from_dict(raw)


def test_rejects_removed_overlay_parktronic() -> None:
    raw = {
        "source": "emulate",
        "abr": _ABR,
        "video": {"width": 640, "height": 480, "fps": 30},
        "overlay": {"parktronic": True},
        "cameras": [{"id": "cam0"}],
    }
    with pytest.raises(ValueError, match="cameras\\[\\].parktronic"):
        Config.from_dict(raw)
    raw["overlay"] = {"parktronic": {"height_m": 0.8}}
    with pytest.raises(ValueError, match="cameras\\[\\].parktronic"):
        Config.from_dict(raw)


def test_camera_parktronic_true_uses_defaults() -> None:
    raw = {
        "source": "emulate",
        "abr": _ABR,
        "video": {"width": 640, "height": 480, "fps": 30},
        "cameras": [{"id": "cam0", "parktronic": True}],
    }
    cfg = Config.from_dict(raw)
    cam = cfg.cameras[0]
    assert cam.parktronic is True
    assert cam.parktronic_overlay == ParktronicOverlayConfig()


def test_camera_feature_mapping_enabled_false_keeps_off() -> None:
    raw = {
        "source": "emulate",
        "abr": _ABR,
        "video": {"width": 640, "height": 480, "fps": 30},
        "cameras": [
            {
                "id": "cam0",
                "parktronic": {"enabled": False, "height_m": 0.8},
                "undistort": {"enabled": False, "k1": -0.1},
            }
        ],
    }
    cam = Config.from_dict(raw).cameras[0]
    assert cam.parktronic is False
    assert cam.parktronic_overlay is None
    assert cam.undistort is False
    assert cam.undistort_params is None


def test_camera_undistort_true_uses_defaults() -> None:
    raw = {
        "source": "emulate",
        "abr": _ABR,
        "video": {"width": 640, "height": 480, "fps": 30},
        "cameras": [{"id": "cam0", "undistort": True}],
    }
    cfg = Config.from_dict(raw)
    cam = cfg.cameras[0]
    assert cam.undistort is True
    assert cam.undistort_params == UndistortConfig()


def test_rejects_bad_undistort_crop() -> None:
    raw = {
        "source": "emulate",
        "abr": _ABR,
        "video": {"width": 640, "height": 480, "fps": 30},
        "cameras": [{"id": "cam0", "undistort": {"crop": 0}}],
    }
    with pytest.raises(ValueError, match="undistort.crop"):
        Config.from_dict(raw)


def test_rejects_removed_capture_keys() -> None:
    raw = {
        "source": "emulate",
        "capture": {"always_on": False},
        "cameras": [{"id": "cam0"}],
    }
    with pytest.raises(ValueError, match="capture is always-on"):
        Config.from_dict(raw)


def test_rejects_empty_qp_range() -> None:
    raw = {
        "source": "jetson",
        "rtsp": {"host": "0.0.0.0"},
        "control_ws": {"host": "0.0.0.0"},
        "abr": _ABR,
        "video": _JETSON_VIDEO,
        "encoder": {**_JETSON_ENC, "qp_range": "  "},
        "cameras": [{"id": "cam0", "sensor_id": 0, "rtsp_port": 8586, "mount": "/live"}],
    }
    with pytest.raises(ValueError, match="qp_range"):
        Config.from_dict(raw)


def test_config_keeps_disabled_cameras_for_control() -> None:
    raw = {
        "source": "emulate",
        "rtsp": {"host": "0.0.0.0"},
        "abr": _ABR,
        "video": {"width": 640, "height": 480, "fps": 30},
        "cameras": [
            {
                "id": "cam0",
                "rtsp_port": 8554,
                "mount": "/live",
                "enabled": True,
                "pattern": "smpte",
            },
            {
                "id": "cam1",
                "rtsp_port": 8555,
                "mount": "/live",
                "enabled": False,
                "pattern": "ball",
            },
        ],
    }
    cfg = Config.from_dict(raw)
    assert len(cfg.cameras) == 2
    assert cfg.cameras[1].enabled is False


def test_allows_shared_rtsp_port_with_distinct_mounts() -> None:
    raw = {
        "source": "emulate",
        "rtsp": {"host": "0.0.0.0"},
        "abr": _ABR,
        "video": {"width": 640, "height": 480, "fps": 30},
        "cameras": [
            {"id": "cam0", "rtsp_port": 8554, "mount": "/front", "pattern": "smpte"},
            {"id": "cam1", "rtsp_port": 8554, "mount": "/rear", "pattern": "ball"},
        ],
    }
    cfg = Config.from_dict(raw)
    assert [c.rtsp_port for c in cfg.cameras] == [8554, 8554]
    assert [c.mount for c in cfg.cameras] == ["/front", "/rear"]


def test_requires_at_least_one_enabled_camera() -> None:
    raw = {
        "source": "emulate",
        "rtsp": {"host": "0.0.0.0"},
        "abr": _ABR,
        "video": {"width": 640, "height": 480, "fps": 30},
        "cameras": [
            {
                "id": "cam0",
                "rtsp_port": 8554,
                "mount": "/live",
                "enabled": False,
            },
        ],
    }
    with pytest.raises(ValueError, match="no enabled cameras"):
        Config.from_dict(raw)


def test_rejects_legacy_http_control_key() -> None:
    raw = {
        "source": "emulate",
        "control": {"host": "127.0.0.1", "port": 8080},
        "rtsp": {"host": "0.0.0.0"},
        "video": {"width": 640, "height": 480, "fps": 30},
        "cameras": [{"id": "cam0", "rtsp_port": 8554, "mount": "/live"}],
    }
    with pytest.raises(ValueError, match="control_ws"):
        Config.from_dict(raw)


def test_rejects_nav_contract_keys_in_yaml() -> None:
    raw = {
        "source": "emulate",
        "rtsp": {"host": "0.0.0.0"},
        "abr": _ABR,
        "video": {"width": 640, "height": 480, "fps": 30},
        "nav": {"topic": "/camera/nav/image_raw"},
        "cameras": [{"id": "cam0", "rtsp_port": 8554, "mount": "/live"}],
    }
    with pytest.raises(ValueError, match="nav:"):
        Config.from_dict(raw)


def test_v4l2_requires_device() -> None:
    raw = {
        "source": "jetson",
        "rtsp": {"host": "0.0.0.0"},
        "control_ws": {"host": "0.0.0.0"},
        "abr": _ABR,
        "video": _JETSON_VIDEO,
        "encoder": _JETSON_ENC,
        "cameras": [
            {
                "id": "cam0",
                "sensor_id": 0,
                "rtsp_port": 8586,
                "mount": "/live",
                "source_type": "v4l2src",
            }
        ],
    }
    with pytest.raises(ValueError, match="device"):
        Config.from_dict(raw)


def test_nvv4l2_requires_device() -> None:
    raw = {
        "source": "jetson",
        "rtsp": {"host": "0.0.0.0"},
        "control_ws": {"host": "0.0.0.0"},
        "abr": _ABR,
        "video": _JETSON_VIDEO,
        "encoder": _JETSON_ENC,
        "cameras": [
            {
                "id": "cam0",
                "sensor_id": 0,
                "rtsp_port": 8586,
                "mount": "/live",
                "source_type": "nvv4l2camerasrc",
            }
        ],
    }
    with pytest.raises(ValueError, match="device"):
        Config.from_dict(raw)


def test_rejects_removed_level_map() -> None:
    raw = {
        "source": "emulate",
        "rtsp": {"host": "0.0.0.0"},
        "abr": {**_ABR, "level_map": {"good": "good"}},
        "video": {"width": 640, "height": 480, "fps": 30},
        "cameras": [{"id": "cam0", "rtsp_port": 8554, "mount": "/live"}],
    }
    with pytest.raises(ValueError, match="level_map"):
        Config.from_dict(raw)


def test_rejects_missing_video_keys() -> None:
    raw = {
        "source": "emulate",
        "abr": _ABR,
        "video": {"width": 640},
        "cameras": [{"id": "cam0"}],
    }
    with pytest.raises(ValueError, match="video requires"):
        Config.from_dict(raw)


def test_rejects_missing_jetson_encoder_keys() -> None:
    raw = {
        "source": "jetson",
        "control_ws": {"host": "0.0.0.0"},
        "abr": _ABR,
        "video": _JETSON_VIDEO,
        "encoder": {"control_rate": 1},
        "cameras": [{"id": "cam0", "sensor_id": 0, "rtsp_port": 8586, "mount": "/live"}],
    }
    with pytest.raises(ValueError, match="encoder requires"):
        Config.from_dict(raw)


def test_rejects_missing_jetson_control_ws_host() -> None:
    raw = {
        "source": "jetson",
        "control_ws": {"enabled": True},
        "abr": _ABR,
        "video": _JETSON_VIDEO,
        "encoder": _JETSON_ENC,
        "cameras": [{"id": "cam0", "sensor_id": 0, "rtsp_port": 8586, "mount": "/live"}],
    }
    with pytest.raises(ValueError, match="control_ws.host"):
        Config.from_dict(raw)


def test_camera_serial_defaults_to_none() -> None:
    raw = {
        "source": "emulate",
        "rtsp": {"host": "0.0.0.0"},
        "abr": _ABR,
        "video": {"width": 640, "height": 480, "fps": 30},
        "cameras": [
            {"id": "cam0", "rtsp_port": 8554, "mount": "/live"},
        ],
    }
    cfg = Config.from_dict(raw)
    assert cfg.cameras[0].serial is None


def test_camera_serial_parses_to_int() -> None:
    raw = {
        "source": "emulate",
        "rtsp": {"host": "0.0.0.0"},
        "abr": _ABR,
        "video": {"width": 640, "height": 480, "fps": 30},
        "cameras": [
            {"id": "cam0", "rtsp_port": 8554, "mount": "/live", "serial": 123456789},
        ],
    }
    cfg = Config.from_dict(raw)
    assert cfg.cameras[0].serial == 123456789


def test_rejects_duplicate_camera_serial() -> None:
    raw = {
        "source": "emulate",
        "rtsp": {"host": "0.0.0.0"},
        "abr": _ABR,
        "video": {"width": 640, "height": 480, "fps": 30},
        "cameras": [
            {"id": "cam0", "rtsp_port": 8554, "mount": "/front", "serial": 111},
            {"id": "cam1", "rtsp_port": 8555, "mount": "/rear", "serial": 111},
        ],
    }
    with pytest.raises(ValueError, match="camera serial values must be unique"):
        Config.from_dict(raw)
