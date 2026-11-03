"""Загрузка и валидация YAML-конфига capture-pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_FPS = 30
DEFAULT_NVMM_FORMAT = "NV12"
DEFAULT_CONTROL_RATE = 1
DEFAULT_PEAK_BITRATE_RATIO = 1.2
DEFAULT_PRESET_LEVEL = 1
DEFAULT_MAXPERF_ENABLE = True
DEFAULT_INSERT_SPS_PPS = True
DEFAULT_QP_RANGE = "0,51:0,51:0,51"
DEFAULT_VBV_SIZE = 0
DEFAULT_VBV_HORIZON_S = 0.2
DEFAULT_GOP_S = 0.33
DEFAULT_OVERLAY_CLOCK = False
DEFAULT_OVERLAY_TIME_FORMAT = "%H:%M:%S.%f"
DEFAULT_OVERLAY_FONT = "Sans Bold 20"
DEFAULT_X264_TUNE = "zerolatency"
DEFAULT_X264_SPEED_PRESET = "ultrafast"
DEFAULT_X264_THREADS = 0
DEFAULT_X264_OPTION_STRING = "rc-lookahead=0:sync-lookahead=0"
DEFAULT_X264_IFRAME_PERIOD = 5
DEFAULT_CAMERA_PARKTRONIC = False
DEFAULT_PARKTRONIC_HFOV_DEG = 110.0
DEFAULT_PARKTRONIC_VFOV_DEG = 79.6
DEFAULT_PARKTRONIC_HEIGHT_M = 0.80
DEFAULT_PARKTRONIC_PITCH_DEG = 11.3
DEFAULT_PARKTRONIC_RINGS_M = (1.0, 2.0)
DEFAULT_PARKTRONIC_RAIL_HALF_WIDTH_M = 0.70
DEFAULT_PARKTRONIC_Y_MIN_M = 0.55
DEFAULT_PARKTRONIC_FILL_STYLE = "edge"
PARKTRONIC_FILL_STYLES = frozenset({"none", "uniform", "fade", "edge", "band"})
DEFAULT_CAMERA_UNDISTORT = False
DEFAULT_CAMERA_UNDISTORT_K1 = -0.22
DEFAULT_CAMERA_UNDISTORT_K2 = 0.0
DEFAULT_CAMERA_UNDISTORT_CROP = 1.0
DEFAULT_CONTROL_WS_HOST = "127.0.0.1"
DEFAULT_CONTROL_WS_PORT = 8591
DEFAULT_CAPTURE_STAGGER_MS = 200
DEFAULT_RTSP_SUSPEND_MODE = "none"
DEFAULT_PIPELINE_RESTART_BACKOFF_MS = 500
DEFAULT_PIPELINE_RESTART_MAX_BACKOFF_MS = 30_000
DEFAULT_LOG_LEVEL = "INFO"
# Глобально WARNING, но известный безвредный спам глушим по категориям:
# отсутствующие в контейнере опциональные плагины (GST_PLUGIN_LOADING,
# vadisplay, nvenc/nvcodec, ladspa), квирки nvv4l2h264enc (v4l2*) и
# "no factory for path" от клиентов, пришедших до появления mount
# (состояние камеры видно в status). appsrc на WARNING печатает
# "Dropping old item buffer" на каждый GOP для залипшего клиента.
DEFAULT_GST_DEBUG = (
    "2,GST_PLUGIN_LOADING:0,vadisplay:0,nvenc:0,nvcodec:0,ladspa:0,"
    "v4l2:1,v4l2bufferpool:1,appsrc:1,rtspclient:0,"
    "nvarguscamerasrc:2,rtspmedia:2"
)
SUPPORTED_SUSPEND_MODES = frozenset({"none", "pause", "reset"})
DEFAULT_ABR_DEFAULT_PROFILE = "good"
DEFAULT_ABR_MIN_DWELL_S = 5.0
SOURCE_JETSON = "jetson"
SOURCE_EMULATE = "emulate"
SOURCE_RPI = "rpi"
SUPPORTED_SOURCES = frozenset({SOURCE_JETSON, SOURCE_EMULATE, SOURCE_RPI})
SOURCE_TYPE_ARGUS = "nvarguscamerasrc"
SOURCE_TYPE_V4L2 = "v4l2src"
SOURCE_TYPE_NVV4L2 = "nvv4l2camerasrc"
SOURCE_TYPE_TEST = "videotestsrc"
SOURCE_TYPE_LIBCAMERA = "libcamerasrc"
SUPPORTED_CAMERA_SOURCE_TYPES = frozenset(
    {
        SOURCE_TYPE_ARGUS,
        SOURCE_TYPE_V4L2,
        SOURCE_TYPE_NVV4L2,
        SOURCE_TYPE_TEST,
        SOURCE_TYPE_LIBCAMERA,
    }
)
_RPI_CAMERA_SOURCE_TYPES = frozenset(
    {SOURCE_TYPE_LIBCAMERA, SOURCE_TYPE_V4L2, SOURCE_TYPE_TEST}
)
# v4l2src и nvv4l2camerasrc открывают /dev/videoN; Argus берёт sensor_id.
_DEVICE_SOURCE_TYPES = frozenset({SOURCE_TYPE_V4L2, SOURCE_TYPE_NVV4L2})
SUPPORTED_NAV_SINK_TYPES = frozenset({"auto", "rbfimagesink", "fakesink"})
SUPPORTED_NAV_ENCODINGS = frozenset({"raw", "compressed"})
DEFAULT_NAV_ENCODING = "compressed"
DEFAULT_NAV_WIDTH = 1280
DEFAULT_NAV_HEIGHT = 720
DEFAULT_NAV_FPS = 15
DEFAULT_NAV_JPEG_QUALITY = 80
_NAV_YAML_FORBIDDEN = frozenset({"topic", "qos", "frame_id", "node_name", "node-name"})


@dataclass(frozen=True)
class RtspConfig:
    host: str
    suspend_mode: str

@dataclass(frozen=True)
class ControlWsConfig:
    host: str
    port: int
    enabled: bool

@dataclass(frozen=True)
class AbrProfile:
    fps: int
    bitrate_kbps: int

    @property
    def bitrate_bps(self) -> int:
        return int(self.bitrate_kbps) * 1000

@dataclass(frozen=True)
class AbrConfig:
    profiles: dict[str, AbrProfile]
    default_profile: str
    min_dwell_s: float

@dataclass(frozen=True)
class VideoConfig:
    sensor_width: int
    sensor_height: int
    width: int
    height: int
    fps: int
    nvmm_format: str

@dataclass(frozen=True)
class EncoderConfig:
    control_rate: int
    maxperf_enable: bool
    insert_sps_pps: bool
    preset_level: int
    qp_range: str
    vbv_size: int
    vbv_horizon_s: float
    gop_s: float
    iframe_period: int = DEFAULT_X264_IFRAME_PERIOD
    tune: str = DEFAULT_X264_TUNE
    speed_preset: str = DEFAULT_X264_SPEED_PRESET
    threads: int = DEFAULT_X264_THREADS
    option_string: str = DEFAULT_X264_OPTION_STRING

@dataclass(frozen=True)
class ParktronicOverlayConfig:
    hfov_deg: float = DEFAULT_PARKTRONIC_HFOV_DEG
    vfov_deg: float = DEFAULT_PARKTRONIC_VFOV_DEG
    height_m: float = DEFAULT_PARKTRONIC_HEIGHT_M
    pitch_deg: float = DEFAULT_PARKTRONIC_PITCH_DEG
    rings_m: tuple[float, ...] = DEFAULT_PARKTRONIC_RINGS_M
    rail_half_width_m: float = DEFAULT_PARKTRONIC_RAIL_HALF_WIDTH_M
    y_min_m: float = DEFAULT_PARKTRONIC_Y_MIN_M
    fill_style: str = DEFAULT_PARKTRONIC_FILL_STYLE


@dataclass(frozen=True)
class UndistortConfig:
    k1: float = DEFAULT_CAMERA_UNDISTORT_K1
    k2: float = DEFAULT_CAMERA_UNDISTORT_K2
    crop: float = DEFAULT_CAMERA_UNDISTORT_CROP


@dataclass(frozen=True)
class OverlayConfig:
    clock: bool
    time_format: str
    font: str = DEFAULT_OVERLAY_FONT


@dataclass(frozen=True)
class CaptureConfig:
    stagger_ms: int

@dataclass(frozen=True)
class PipelineRecoveryConfig:
    restart_backoff_ms: int
    restart_max_backoff_ms: int

@dataclass(frozen=True)
class LoggingConfig:
    level: str
    gst_debug: str

@dataclass(frozen=True)
class NavConfig:
    sink_type: str
    encoding: str
    width: int
    height: int
    fps: int
    jpeg_quality: int


@dataclass(frozen=True)
class CameraConfig:
    id: str
    sensor_id: int
    serial: int | None
    rtsp_port: int
    mount: str
    enabled: bool
    flip_method: int
    pattern: str
    source_type: str
    device: str | None
    pixel_format: str | None
    camera_name: str | None
    ros_name: str | None
    nav_enabled: bool
    parktronic: bool
    parktronic_overlay: ParktronicOverlayConfig | None
    undistort: bool
    undistort_params: UndistortConfig | None


@dataclass(frozen=True)
class Config:
    source: str
    rtsp: RtspConfig
    video: VideoConfig
    encoder: EncoderConfig
    cameras: tuple[CameraConfig, ...]
    control_ws: ControlWsConfig
    abr: AbrConfig
    capture: CaptureConfig
    pipeline: PipelineRecoveryConfig
    logging: LoggingConfig
    nav: NavConfig
    overlay: OverlayConfig

    @property
    def is_emulate(self) -> bool:
        return self.source == SOURCE_EMULATE

    @property
    def is_rpi(self) -> bool:
        return self.source == SOURCE_RPI

    @property
    def is_jetson(self) -> bool:
        return self.source == SOURCE_JETSON

    @classmethod
    def from_dict(cls, raw: dict) -> "Config":
        source = str(raw.get("source", SOURCE_JETSON)).strip().lower()
        if source not in SUPPORTED_SOURCES:
            raise ValueError(f"source must be one of {sorted(SUPPORTED_SOURCES)}, got {source!r}")
        rtsp_raw = raw.get("rtsp") or {}
        video_raw = raw.get("video") or {}
        enc_raw = raw.get("encoder") or {}
        control_ws_raw = raw.get("control_ws") or {}
        abr_raw = raw.get("abr") or {}
        capture_raw = raw.get("capture") or {}
        pipeline_raw = raw.get("pipeline") or {}
        logging_raw = raw.get("logging") or {}
        if "control" in raw:
            raise ValueError("control: HTTP API removed; use control_ws: (host/port) instead")
        if any(k in abr_raw for k in ("host", "port", "enabled")):
            raise ValueError(
                "abr.host/port/enabled removed (transport is control_ws); "
                "keep only profiles / default_profile / min_dwell_s"
            )
        if "level_map" in abr_raw:
            raise ValueError(
                "abr.level_map removed: link-quality level must match an abr.profiles key"
            )
        if "mode" in rtsp_raw:
            raise ValueError("rtsp.mode removed: appsink/appsrc is the only transport")
        if any(k in capture_raw for k in ("always_on", "suspend_mode")):
            raise ValueError(
                "capture.always_on/suspend_mode removed: capture is always-on; "
                "use rtsp.suspend_mode"
            )
        stale_encoder = sorted(
            set(enc_raw) & {"bitrate_bps", "peak_bitrate_bps", "iframe_interval"}
        )
        if stale_encoder:
            raise ValueError(
                f"encoder keys {stale_encoder} removed: bitrate/fps come from ABR, "
                "GOP comes from encoder.gop_s"
            )
        default_suspend = DEFAULT_RTSP_SUSPEND_MODE if source == SOURCE_JETSON else "reset"
        suspend_mode = str(
            rtsp_raw.get("suspend_mode", default_suspend)
        ).strip().lower()
        if suspend_mode not in SUPPORTED_SUSPEND_MODES:
            raise ValueError(
                f"rtsp.suspend_mode must be one of {sorted(SUPPORTED_SUSPEND_MODES)}, "
                f"got {suspend_mode!r}"
            )
        rtsp = RtspConfig(
            host=str(rtsp_raw.get("host", "0.0.0.0")),
            suspend_mode=suspend_mode,
        )
        control_enabled = bool(control_ws_raw.get("enabled", True))
        if source == SOURCE_JETSON and control_enabled and "host" not in control_ws_raw:
            raise ValueError("control_ws.host is required on jetson")
        control_ws = ControlWsConfig(
            host=str(control_ws_raw.get("host", DEFAULT_CONTROL_WS_HOST)),
            port=int(control_ws_raw.get("port", DEFAULT_CONTROL_WS_PORT)),
            enabled=control_enabled,
        )
        profiles = _parse_abr_profiles(abr_raw.get("profiles"))
        default_profile = str(abr_raw.get("default_profile", DEFAULT_ABR_DEFAULT_PROFILE)).strip().lower()
        if default_profile not in profiles:
            raise ValueError(f"abr.default_profile {default_profile!r} not in profiles {sorted(profiles)}")
        abr = AbrConfig(
            profiles=profiles,
            default_profile=default_profile,
            min_dwell_s=max(0.0, float(abr_raw.get("min_dwell_s", DEFAULT_ABR_MIN_DWELL_S))),
        )
        missing_video = _missing_keys(video_raw, ("width", "height", "fps"))
        if missing_video:
            raise ValueError(f"video requires {missing_video}")
        if source == SOURCE_JETSON:
            missing_sensor = _missing_keys(video_raw, ("sensor_width", "sensor_height"))
            if missing_sensor:
                raise ValueError(f"video requires {missing_sensor} on jetson")
        out_w = int(video_raw["width"])
        out_h = int(video_raw["height"])
        video = VideoConfig(
            sensor_width=int(video_raw.get("sensor_width", out_w)),
            sensor_height=int(video_raw.get("sensor_height", out_h)),
            width=out_w,
            height=out_h,
            fps=int(video_raw["fps"]),
            nvmm_format=str(video_raw.get("nvmm_format", DEFAULT_NVMM_FORMAT)),
        )
        if source == SOURCE_JETSON and (
            video.sensor_width < video.width or video.sensor_height < video.height
        ):
            raise ValueError(
                "sensor resolution must be >= output resolution "
                f"(sensor={video.sensor_width}x{video.sensor_height}, out={video.width}x{video.height})"
            )
        if source == SOURCE_JETSON:
            missing_enc = _missing_keys(
                enc_raw, ("control_rate", "preset_level", "gop_s", "vbv_horizon_s")
            )
            if missing_enc:
                raise ValueError(f"encoder requires {missing_enc} on jetson")
        tune = str(enc_raw.get("tune", DEFAULT_X264_TUNE)).strip()
        speed_preset = str(
            enc_raw.get("speed_preset", DEFAULT_X264_SPEED_PRESET)
        ).strip()
        option_string = str(
            enc_raw.get("option_string", DEFAULT_X264_OPTION_STRING)
        ).strip()
        enc = EncoderConfig(
            control_rate=int(enc_raw.get("control_rate", DEFAULT_CONTROL_RATE)),
            maxperf_enable=bool(enc_raw.get("maxperf_enable", DEFAULT_MAXPERF_ENABLE)),
            insert_sps_pps=bool(enc_raw.get("insert_sps_pps", DEFAULT_INSERT_SPS_PPS)),
            preset_level=int(enc_raw.get("preset_level", DEFAULT_PRESET_LEVEL)),
            qp_range=str(enc_raw.get("qp_range", DEFAULT_QP_RANGE)).strip(),
            vbv_size=int(enc_raw.get("vbv_size", DEFAULT_VBV_SIZE)),
            vbv_horizon_s=float(enc_raw.get("vbv_horizon_s", DEFAULT_VBV_HORIZON_S)),
            gop_s=float(enc_raw.get("gop_s", DEFAULT_GOP_S)),
            iframe_period=int(
                enc_raw.get("iframe_period", DEFAULT_X264_IFRAME_PERIOD)
            ),
            tune=tune,
            speed_preset=speed_preset,
            threads=int(enc_raw.get("threads", DEFAULT_X264_THREADS)),
            option_string=option_string,
        )
        if enc.control_rate not in (0, 1):
            raise ValueError("encoder.control_rate must be 0 (VBR) or 1 (CBR)")
        if not enc.qp_range:
            raise ValueError("encoder.qp_range must be a non-empty nvv4l2 string")
        if enc.vbv_size < 0:
            raise ValueError("encoder.vbv_size must be >= 0 (0 = follow bitrate × vbv_horizon_s)")
        if enc.vbv_horizon_s <= 0:
            raise ValueError("encoder.vbv_horizon_s must be > 0")
        if enc.gop_s <= 0:
            raise ValueError("encoder.gop_s must be > 0")
        if enc.iframe_period <= 0:
            raise ValueError("encoder.iframe_period must be > 0")
        if enc.threads < 0:
            raise ValueError("encoder.threads must be >= 0 (0 = auto)")
        if not enc.tune:
            raise ValueError("encoder.tune must be a non-empty x264 tune")
        if not enc.speed_preset:
            raise ValueError(
                "encoder.speed_preset must be a non-empty x264 speed-preset"
            )
        if not enc.option_string:
            raise ValueError("encoder.option_string must be a non-empty x264 option-string")
        cameras = _parse_cameras(raw.get("cameras") or [], source)
        _require_unique([(c.rtsp_port, c.mount) for c in cameras], "camera (rtsp_port, mount) bindings must be unique")
        if source == SOURCE_JETSON:
            _require_unique([c.sensor_id for c in cameras], "camera sensor_id values must be unique")
        _require_unique(
            [c.serial for c in cameras if c.serial is not None],
            "camera serial values must be unique",
        )
        _require_unique([c.id for c in cameras], "camera id values must be unique")
        if not any(c.enabled for c in cameras):
            raise ValueError("no enabled cameras in config")
        stagger_ms = max(0, int(capture_raw.get("stagger_ms", DEFAULT_CAPTURE_STAGGER_MS)))
        restart_backoff_ms = max(
            0, int(pipeline_raw.get("restart_backoff_ms", DEFAULT_PIPELINE_RESTART_BACKOFF_MS))
        )
        restart_max_backoff_ms = max(
            restart_backoff_ms,
            int(pipeline_raw.get("restart_max_backoff_ms", DEFAULT_PIPELINE_RESTART_MAX_BACKOFF_MS)),
        )
        return cls(
            source=source,
            rtsp=rtsp,
            video=video,
            encoder=enc,
            cameras=tuple(cameras),
            control_ws=control_ws,
            abr=abr,
            capture=CaptureConfig(stagger_ms=stagger_ms),
            pipeline=PipelineRecoveryConfig(
                restart_backoff_ms=restart_backoff_ms,
                restart_max_backoff_ms=restart_max_backoff_ms,
            ),
            logging=LoggingConfig(
                level=str(logging_raw.get("level", DEFAULT_LOG_LEVEL)).strip().upper(),
                gst_debug=str(logging_raw.get("gst_debug", DEFAULT_GST_DEBUG)).strip(),
            ),
            nav=_parse_nav(raw.get("nav")),
            overlay=_parse_overlay(raw.get("overlay")),
        )


def _missing_keys(raw: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    return [key for key in keys if key not in raw]


def _require_unique(values: list[Any], msg: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(msg)


def _parse_cameras(cams_raw: list[Any], source: str) -> list[CameraConfig]:
    default_patterns = ("smpte", "ball", "snow", "circular")
    cameras: list[CameraConfig] = []
    for idx, cam_raw in enumerate(cams_raw):
        mount = str(cam_raw.get("mount", "/live")).strip()
        if not mount.startswith("/"):
            raise ValueError(f"camera mount must start with '/': {mount!r}")
        flip_method = int(cam_raw.get("flip_method", 0))
        if flip_method not in range(8):
            raise ValueError(
                f"camera {cam_raw.get('id', idx)} flip_method must be 0..7, got {flip_method}"
            )
        if source == SOURCE_EMULATE:
            default_src = SOURCE_TYPE_TEST
        elif source == SOURCE_RPI:
            default_src = SOURCE_TYPE_LIBCAMERA
        else:
            default_src = SOURCE_TYPE_ARGUS
        raw_src = cam_raw.get("source_type")
        source_type = default_src if raw_src is None else str(raw_src).strip().lower()
        if source_type not in SUPPORTED_CAMERA_SOURCE_TYPES:
            raise ValueError(
                f"camera {cam_raw.get('id', idx)} source_type must be one of "
                f"{sorted(SUPPORTED_CAMERA_SOURCE_TYPES)}, got {source_type!r}"
            )
        if source == SOURCE_RPI and source_type not in _RPI_CAMERA_SOURCE_TYPES:
            raise ValueError(
                f"camera {cam_raw.get('id', idx)} source_type must be one of "
                f"{sorted(_RPI_CAMERA_SOURCE_TYPES)} when source=rpi, "
                f"got {source_type!r}"
            )
        if source == SOURCE_JETSON and source_type == SOURCE_TYPE_LIBCAMERA:
            raise ValueError(
                f"camera {cam_raw.get('id', idx)}: libcamerasrc is not a jetson source"
            )
        raw_dev = cam_raw.get("device")
        device = str(raw_dev).strip() if raw_dev else None
        if source_type in _DEVICE_SOURCE_TYPES and not device:
            raise ValueError(
                f"camera {cam_raw.get('id', idx)}: device required for {source_type}"
            )
        raw_name = cam_raw.get("camera_name")
        camera_name = str(raw_name).strip() if raw_name else None
        if source_type == SOURCE_TYPE_LIBCAMERA and not camera_name:
            raise ValueError(
                f"camera {cam_raw.get('id', idx)}: camera_name required for libcamerasrc"
            )
        raw_pix = cam_raw.get("pixel_format")
        if raw_pix is not None:
            pixel_format = str(raw_pix).strip() or None
        elif source_type == SOURCE_TYPE_LIBCAMERA:
            pixel_format = "NV12"
        elif source_type in _DEVICE_SOURCE_TYPES:
            pixel_format = "UYVY"
        else:
            pixel_format = None
        cam_id = str(cam_raw.get("id", f"cam{idx}"))
        stale_undistort = sorted(
            set(cam_raw) & {"undistort_k1", "undistort_k2", "undistort_crop"}
        )
        if stale_undistort:
            raise ValueError(
                f"camera {cam_id}: {stale_undistort} moved under "
                "cameras[].undistort mapping (k1 / k2 / crop)"
            )
        undistort, undistort_params = _parse_camera_undistort(
            cam_raw.get("undistort", DEFAULT_CAMERA_UNDISTORT),
            cam_id,
        )
        parktronic, parktronic_overlay = _parse_camera_parktronic(
            cam_raw.get("parktronic", DEFAULT_CAMERA_PARKTRONIC),
            cam_id,
        )
        ros_name = _parse_ros_name(cam_raw.get("ros_name"), cam_id)
        nav_enabled = bool(cam_raw.get("nav_enabled", False))
        if nav_enabled and not ros_name:
            raise ValueError(f"camera {cam_id}: nav_enabled requires ros_name")
        raw_serial = cam_raw.get("serial")
        serial = int(raw_serial) if raw_serial is not None else None
        cameras.append(
            CameraConfig(
                id=cam_id,
                sensor_id=int(cam_raw.get("sensor_id", idx)),
                serial=serial,
                rtsp_port=int(cam_raw.get("rtsp_port", 8586 + idx)),
                mount=mount,
                enabled=bool(cam_raw.get("enabled", True)),
                flip_method=flip_method,
                pattern=str(cam_raw.get("pattern", default_patterns[idx % len(default_patterns)])).strip(),
                source_type=source_type,
                device=device,
                pixel_format=pixel_format,
                camera_name=camera_name,
                ros_name=ros_name,
                nav_enabled=nav_enabled,
                parktronic=parktronic,
                parktronic_overlay=parktronic_overlay,
                undistort=undistort,
                undistort_params=undistort_params,
            )
        )
    if not cameras:
        raise ValueError("no cameras in config")
    _require_unique(
        [c.ros_name for c in cameras if c.enabled and c.ros_name],
        "camera ros_name values must be unique among enabled cameras",
    )
    return cameras


def _parse_overlay(overlay_raw: Any) -> OverlayConfig:
    if overlay_raw is None:
        return OverlayConfig(
            clock=DEFAULT_OVERLAY_CLOCK,
            time_format=DEFAULT_OVERLAY_TIME_FORMAT,
            font=DEFAULT_OVERLAY_FONT,
        )
    if not isinstance(overlay_raw, dict):
        raise ValueError("overlay must be a mapping")
    if "parktronic" in overlay_raw:
        raise ValueError(
            "overlay.parktronic removed: use cameras[].parktronic "
            "(false, true, or a geometry mapping)"
        )
    time_format = str(overlay_raw.get("time_format", DEFAULT_OVERLAY_TIME_FORMAT)).strip()
    if not time_format:
        raise ValueError("overlay.time_format must be a non-empty strftime pattern")
    font = str(overlay_raw.get("font", DEFAULT_OVERLAY_FONT)).strip()
    if not font:
        raise ValueError("overlay.font must be a non-empty Pango font description")
    return OverlayConfig(
        clock=bool(overlay_raw.get("clock", DEFAULT_OVERLAY_CLOCK)),
        time_format=time_format,
        font=font,
    )


def _parse_camera_undistort(
    raw: Any, cam_id: Any
) -> tuple[bool, UndistortConfig | None]:
    if raw is None or raw is False:
        return False, None
    if raw is True:
        return True, UndistortConfig()
    if isinstance(raw, dict):
        enabled = bool(raw.get("enabled", True))
        params = _parse_undistort_mapping(raw)
        if not enabled:
            return False, None
        return True, params
    raise ValueError(
        f"camera {cam_id} undistort must be false, true, or a mapping, got {raw!r}"
    )


def _parse_undistort_mapping(raw: dict[str, Any]) -> UndistortConfig:
    k1 = float(raw.get("k1", DEFAULT_CAMERA_UNDISTORT_K1))
    k2 = float(raw.get("k2", DEFAULT_CAMERA_UNDISTORT_K2))
    crop = float(raw.get("crop", DEFAULT_CAMERA_UNDISTORT_CROP))
    if not 0.0 < crop <= 1.0:
        raise ValueError(
            f"cameras[].undistort.crop must be in (0, 1], got {crop}"
        )
    return UndistortConfig(k1=k1, k2=k2, crop=crop)


def _parse_camera_parktronic(
    raw: Any, cam_id: Any
) -> tuple[bool, ParktronicOverlayConfig | None]:
    if raw is None or raw is False:
        return False, None
    if raw is True:
        return True, ParktronicOverlayConfig()
    if isinstance(raw, dict):
        enabled = bool(raw.get("enabled", True))
        params = _parse_parktronic_overlay(raw)
        if not enabled:
            return False, None
        return True, params
    raise ValueError(
        f"camera {cam_id} parktronic must be false, true, or a mapping, got {raw!r}"
    )


def _parse_parktronic_overlay(raw: dict[str, Any]) -> ParktronicOverlayConfig:
    rings_raw = raw.get("rings_m", DEFAULT_PARKTRONIC_RINGS_M)
    if isinstance(rings_raw, (int, float)):
        rings_m = (float(rings_raw),)
    else:
        rings_m = tuple(float(item) for item in rings_raw)
    if not rings_m or any(radius <= 0.0 for radius in rings_m):
        raise ValueError(
            "cameras[].parktronic.rings_m must be a non-empty list of positive metres"
        )
    fill_style = str(raw.get("fill_style", DEFAULT_PARKTRONIC_FILL_STYLE)).strip().lower()
    if fill_style not in PARKTRONIC_FILL_STYLES:
        raise ValueError(
            f"cameras[].parktronic.fill_style must be one of {sorted(PARKTRONIC_FILL_STYLES)}, "
            f"got {fill_style!r}"
        )
    hfov_deg = float(raw.get("hfov_deg", DEFAULT_PARKTRONIC_HFOV_DEG))
    vfov_deg = float(raw.get("vfov_deg", DEFAULT_PARKTRONIC_VFOV_DEG))
    height_m = float(raw.get("height_m", DEFAULT_PARKTRONIC_HEIGHT_M))
    pitch_deg = float(raw.get("pitch_deg", DEFAULT_PARKTRONIC_PITCH_DEG))
    rail_half_width_m = float(
        raw.get("rail_half_width_m", DEFAULT_PARKTRONIC_RAIL_HALF_WIDTH_M)
    )
    y_min_m = float(raw.get("y_min_m", DEFAULT_PARKTRONIC_Y_MIN_M))
    if hfov_deg <= 0.0 or vfov_deg <= 0.0:
        raise ValueError("cameras[].parktronic hfov_deg and vfov_deg must be > 0")
    if height_m <= 0.0:
        raise ValueError("cameras[].parktronic.height_m must be > 0")
    if rail_half_width_m <= 0.0:
        raise ValueError("cameras[].parktronic.rail_half_width_m must be > 0")
    far_m = rings_m[-1]
    if rail_half_width_m >= far_m:
        raise ValueError(
            "cameras[].parktronic.rail_half_width_m must be < the farthest rings_m entry"
        )
    y_max_m = (far_m * far_m - rail_half_width_m * rail_half_width_m) ** 0.5
    if y_min_m <= 0.0 or y_min_m >= y_max_m:
        raise ValueError(
            "cameras[].parktronic.y_min_m must be > 0 and less than the rail/far-ring intersection"
        )
    return ParktronicOverlayConfig(
        hfov_deg=hfov_deg,
        vfov_deg=vfov_deg,
        height_m=height_m,
        pitch_deg=pitch_deg,
        rings_m=rings_m,
        rail_half_width_m=rail_half_width_m,
        y_min_m=y_min_m,
        fill_style=fill_style,
    )


def _parse_ros_name(raw: Any, cam_id: str) -> str | None:
    if raw is None:
        return None
    ros_name = str(raw).strip()
    if not ros_name:
        return None
    if "/" in ros_name or any(ch.isspace() for ch in ros_name):
        raise ValueError(
            f"camera {cam_id}: ros_name must be a single path segment, got {ros_name!r}"
        )
    return ros_name


def _parse_nav(nav_raw: Any) -> NavConfig:
    if nav_raw is None:
        return NavConfig(
            sink_type="auto",
            encoding=DEFAULT_NAV_ENCODING,
            width=DEFAULT_NAV_WIDTH,
            height=DEFAULT_NAV_HEIGHT,
            fps=DEFAULT_NAV_FPS,
            jpeg_quality=DEFAULT_NAV_JPEG_QUALITY,
        )
    if not isinstance(nav_raw, dict):
        raise ValueError("nav must be a mapping")
    forbidden = sorted(set(nav_raw) & _NAV_YAML_FORBIDDEN)
    if forbidden:
        raise ValueError(
            "nav: topic/qos/frame_id/node_name are derived from cameras[].ros_name; "
            f"got {forbidden}"
        )
    sink = str(nav_raw.get("sink_type", "auto")).strip().lower()
    if sink not in SUPPORTED_NAV_SINK_TYPES:
        raise ValueError(
            f"nav.sink_type must be one of {sorted(SUPPORTED_NAV_SINK_TYPES)}, got {sink!r}"
        )
    width = int(nav_raw.get("width", DEFAULT_NAV_WIDTH))
    height = int(nav_raw.get("height", DEFAULT_NAV_HEIGHT))
    fps = int(nav_raw.get("fps", DEFAULT_NAV_FPS))
    jpeg_quality = int(nav_raw.get("jpeg_quality", DEFAULT_NAV_JPEG_QUALITY))
    encoding = str(nav_raw.get("encoding", DEFAULT_NAV_ENCODING)).strip().lower()
    if encoding not in SUPPORTED_NAV_ENCODINGS:
        raise ValueError(
            f"nav.encoding must be one of {sorted(SUPPORTED_NAV_ENCODINGS)}, "
            f"got {encoding!r}"
        )
    if width <= 0 or height <= 0 or fps <= 0:
        raise ValueError("nav.width/height/fps must be > 0")
    if not 0 <= jpeg_quality <= 100:
        raise ValueError("nav.jpeg_quality must be in 0..100")
    return NavConfig(
        sink_type=sink,
        encoding=encoding,
        width=width,
        height=height,
        fps=fps,
        jpeg_quality=jpeg_quality,
    )


def _parse_abr_profiles(raw_profiles: Any) -> dict[str, AbrProfile]:
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise ValueError("abr.profiles is required and must be a non-empty mapping")
    profiles: dict[str, AbrProfile] = {}
    for name, value in raw_profiles.items():
        key = str(name).strip().lower()
        if isinstance(value, dict):
            fps = int(value.get("fps", DEFAULT_FPS))
            if "bitrate_kbps" in value:
                kbps = int(value["bitrate_kbps"])
            elif "bitrate_bps" in value:
                kbps = max(1, int(value["bitrate_bps"]) // 1000)
            else:
                raise ValueError(f"abr.profiles.{key}: need bitrate_kbps")
        else:
            kbps = max(1, int(value) // 1000)
            fps = DEFAULT_FPS
        if fps <= 0 or kbps <= 0:
            raise ValueError(f"abr.profiles.{key}: fps and bitrate_kbps must be > 0")
        profiles[key] = AbrProfile(fps=fps, bitrate_kbps=kbps)
    if not profiles:
        raise ValueError("abr.profiles must be a non-empty mapping")
    return profiles


def load_config(path: Path) -> Config:
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return Config.from_dict(raw)
