"""Ветка teleop: encode и factory appsrc для GstRTSPMediaFactory."""

from __future__ import annotations

from ..config import Config, EncoderConfig
from .overlays.parktronic import PARK_ELEMENT_NAME
from .undistort import UNDIST_ELEMENT_NAME

LEAKY_QUEUE = (
    "queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream"
)
ENCODER_ELEMENT_NAME = "enc0"
RATE_ELEMENT_NAME = "rate0"
OVERLAY_ELEMENT_NAME = "ovl"
CLOCK_ELEMENT_NAME = "clock0"
ENCODED_SINK_ELEMENT_NAME = "encoded_sink"
ENCODED_SRC_ELEMENT_NAME = "encoded_src"
# Живому Argus GstRTSPMedia требует latency. 1 мс вместо дефолтных 200 мс,
# тайминг при этом не выключается.
RTSP_FACTORY_LATENCY_MS = 1


def jetson_vbv_size(encoder: EncoderConfig, bitrate_bps: int) -> int:
    """VBV в битах: абсолютный ``vbv_size`` или ``vbv_horizon_s`` × текущий ABR bitrate."""
    if encoder.vbv_size > 0:
        return int(encoder.vbv_size)
    return max(1, int(round(bitrate_bps * encoder.vbv_horizon_s)))


def jetson_iframe_interval(encoder: EncoderConfig, fps: int) -> int:
    """Период IDR в кадрах: ``gop_s`` × активный ABR fps."""
    return max(1, int(round(fps * encoder.gop_s)))


def build_teleop_branch(
    cfg: Config,
    *,
    jetson: bool,
    bitrate_bps: int,
    peak_bitrate_bps: int,
    fps: int,
    profile_label: str,
    parktronic: bool = False,
    undistort: bool = False,
) -> str:
    """Ветка encode teleop, заканчивается однобуферным appsink."""
    if jetson:
        return _teleop_jetson(
            cfg,
            bitrate_bps=bitrate_bps,
            peak_bitrate_bps=peak_bitrate_bps,
            fps=fps,
            parktronic=parktronic,
            undistort=undistort,
        )
    return _teleop_cpu(
        cfg,
        bitrate_bps=bitrate_bps,
        fps=fps,
        profile_label=profile_label,
    )


def build_rtsp_appsrc(fps: int) -> str:
    """Живой H.264 appsrc для factory: без повторного parse AU."""
    frame_ns = max(1, int(round(1_000_000_000 / max(1, fps))))
    return (
        f'appsrc name={ENCODED_SRC_ELEMENT_NAME} is-live=true format=time '
        'do-timestamp=true block=false max-buffers=1 max-bytes=0 max-time=0 '
        f'leaky-type=downstream min-latency=0 max-latency={frame_ns} '
        'caps="video/x-h264,stream-format=byte-stream,alignment=au" '
        "! rtph264pay name=pay0 pt=96 config-interval=-1 aggregate-mode=none"
    )


def _encoded_sink() -> str:
    return (
        f"! appsink name={ENCODED_SINK_ELEMENT_NAME} emit-signals=true "
        "sync=false async=false max-buffers=1 drop=true"
    )


def _teleop_size_caps(*, jetson: bool, width: int, height: int, nvmm_format: str) -> str:
    if jetson:
        return (
            f"video/x-raw(memory:NVMM),format={nvmm_format},"
            f"width={width},height={height}"
        )
    return f"video/x-raw,width={width},height={height}"


def _teleop_jetson(
    cfg: Config,
    *,
    bitrate_bps: int,
    peak_bitrate_bps: int,
    fps: int,
    parktronic: bool,
    undistort: bool,
) -> str:
    v = cfg.video
    e = cfg.encoder
    vbv = jetson_vbv_size(e, bitrate_bps)
    gop = jetson_iframe_interval(e, fps)
    peak = max(bitrate_bps, peak_bitrate_bps)
    caps = _teleop_size_caps(
        jetson=True, width=v.width, height=v.height, nvmm_format="NV12"
    )
    clock = _jetson_osd_branch(cfg, caps, parktronic=parktronic, undistort=undistort)
    # Source уже NV12 @ sensor. Скейл/конверт в teleop только если размер другой или есть HUD.
    same_size = v.width == v.sensor_width and v.height == v.sensor_height
    to_enc = (
        f"! {caps} {clock}"
        if same_size and not clock
        else f"! nvvidconv ! {caps} {clock}"
    )
    return (
        f"{LEAKY_QUEUE} "
        f"! videorate drop-only=true name={RATE_ELEMENT_NAME} max-rate={fps} "
        f"{to_enc}"
        f"! nvv4l2h264enc name={ENCODER_ELEMENT_NAME} "
        f"bitrate={bitrate_bps} "
        f"peak-bitrate={peak} "
        f"vbv-size={vbv} "
        f'qp-range="{e.qp_range}" '
        f"iframeinterval={gop} "
        f"idrinterval={gop} "
        f"insert-aud=true "
        f"control-rate={e.control_rate} "
        f"maxperf-enable={str(e.maxperf_enable).lower()} "
        f"insert-sps-pps={str(e.insert_sps_pps).lower()} "
        f"preset-level={e.preset_level} "
        f"! h264parse config-interval=-1 "
        f"! video/x-h264,stream-format=byte-stream,alignment=au "
        f"{_encoded_sink()}"
    )


def _jetson_osd_branch(
    cfg: Config, nvmm_caps: str, *, parktronic: bool, undistort: bool
) -> str:
    """CPU Cairo HUD на 640×H BGRx, затем обратно в NVMM для NVENC.

    Слот как у DeepStream nvdsosd (после scale, до encode). nvdsosd на
    этой L4T нет — стоит cairooverlay. clockoverlay/%f на L4T не работает:
    часы — textoverlay + Python probe. Парктроник только здесь, не на NAV.
    Undistort на ``undist0`` до cairooverlay, чтобы метры HUD совпали с pinhole.
    """
    if not cfg.overlay.clock and not parktronic and not undistort:
        return ""
    parts = ["! nvvidconv ! video/x-raw,format=BGRx "]
    if undistort:
        parts.append(f"! identity name={UNDIST_ELEMENT_NAME} silent=true ")
    if parktronic:
        parts.append(f"! cairooverlay name={PARK_ELEMENT_NAME} ")
    if cfg.overlay.clock:
        parts.append(
            f"! textoverlay name={CLOCK_ELEMENT_NAME} shaded-background=true "
            f'font-desc="Sans Bold 18" halignment=left valignment=top '
            f'text="--:--:--.------" '
        )
    parts.append(f"! nvvidconv ! {nvmm_caps} ")
    return "".join(parts)


def _teleop_cpu(
    cfg: Config,
    *,
    bitrate_bps: int,
    fps: int,
    profile_label: str,
) -> str:
    v = cfg.video
    kbps = max(1, int(bitrate_bps) // 1000)
    in_w, in_h = (v.width, v.height) if cfg.is_emulate else (v.sensor_width, v.sensor_height)
    scale = ""
    if in_w != v.width or in_h != v.height:
        scale = f"! videoscale ! video/x-raw,width={v.width},height={v.height} "
    return (
        f"{LEAKY_QUEUE} "
        f"! videorate drop-only=true name={RATE_ELEMENT_NAME} max-rate={fps} "
        f"{scale}"
        f"! textoverlay name={OVERLAY_ELEMENT_NAME} font-desc=\"Sans Bold 20\" "
        f"halignment=left valignment=top shaded-background=true "
        f'text="abr {profile_label} {fps}fps {kbps}k" '
        f"! videoconvert ! video/x-raw,format=I420 "
        f"! x264enc name={ENCODER_ELEMENT_NAME} tune=zerolatency speed-preset=ultrafast "
        f"bitrate={kbps} key-int-max={min(int(fps), 5)} "
        f"rc-lookahead=0 vbv-buf-capacity=300 bframes=0 "
        f"! h264parse config-interval=-1 "
        f"! video/x-h264,stream-format=byte-stream,alignment=au "
        f"{_encoded_sink()}"
    )
