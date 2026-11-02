"""Состояние одной камеры: capture pipeline и appsrc RTSP factory."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock
from typing import Any

from .config import DEFAULT_PEAK_BITRATE_RATIO, CameraConfig, Config
from .pipeline_recovery import (
    PLAYING_TIMEOUT_MS,
    is_ignorable_argus_bus_error,
)
from .pipelines import (
    build_capture,
    build_rtsp_appsrc,
)
from .pipelines.teleop import (
    CLOCK_ELEMENT_NAME,
    ENCODED_SINK_ELEMENT_NAME,
    ENCODER_ELEMENT_NAME,
    OVERLAY_ELEMENT_NAME,
    RATE_ELEMENT_NAME,
    RTSP_FACTORY_LATENCY_MS,
    jetson_iframe_interval,
    jetson_vbv_size,
)
from .pipelines.overlays.parktronic import PARK_ELEMENT_NAME, draw_parktronic_guides
from .pipelines.undistort import (
    UNDIST_ELEMENT_NAME,
    build_maps,
    remap_gst_buffer,
)

logger = logging.getLogger(__name__)

SUSPEND_MODE_MAP = {"none": "NONE", "pause": "PAUSE", "reset": "RESET"}


def abr_keep_frame(acc: int, *, capture_fps: int, target_fps: int) -> tuple[bool, int]:
    """Решить, пропустить ли кадр, и вернуть новый аккумулятор.

    ``capture_fps`` — частота, на которой энкодер уже согласовал caps.
    ``nvv4l2h264enc`` отвергает повторный setcaps с другим framerate и
    начинает отдавать пустые буферы, поэтому ABR не меняет ``videorate``
    после PLAYING, а выбрасывает лишние кадры до энкодера.
    """
    if capture_fps <= 0 or target_fps >= capture_fps:
        return True, 0
    acc += target_fps
    if acc >= capture_fps:
        return True, acc - capture_fps
    return False, acc
PIPELINE_NULL_TIMEOUT_NS = 3_000_000_000


def _state_name(state: Any) -> str:
    """Вернуть читаемое имя Gst.State/StateChangeReturn для диагностики."""
    return str(
        getattr(state, "value_nick", None)
        or getattr(state, "name", None)
        or state
    )


def _pipeline_state(pipeline: Any, timeout_ns: int) -> tuple[Any, Any, Any]:
    """Нормализовать результат Gst.Element.get_state()."""
    result = pipeline.get_state(timeout_ns)
    if isinstance(result, tuple) and len(result) == 3:
        return result
    return result, None, None


def force_keyframe(enc: Any, gst: Any, gst_video: Any | None) -> None:
    if gst_video is None:
        return
    sink_pad = enc.get_static_pad("sink")
    if sink_pad is None:
        return
    try:
        event = gst_video.video_event_new_downstream_force_key_unit(
            gst.CLOCK_TIME_NONE,
            gst.CLOCK_TIME_NONE,
            gst.CLOCK_TIME_NONE,
            True,
            0,
        )
        if not sink_pad.send_event(event):
            logger.debug("force-keyframe not handled on %s", enc.get_name())
    except Exception as exc:  # noqa: BLE001
        logger.debug("force-keyframe failed: %s", exc)


def _try_set(el: Any, name: str, value: Any) -> None:
    try:
        el.set_property(name, value)
    except Exception as exc:  # noqa: BLE001
        logger.debug("optional encoder property %s ignored: %s", name, exc)


def format_overlay_clock(time_format: str, now: datetime | None = None) -> str:
    """Wall-clock на кадре (Python strftime, ``%f`` — микросекунды)."""
    return (now or datetime.now().astimezone()).strftime(time_format)


def attach_clock_overlay(pipeline: Any, cfg: Config, gst: Any) -> Any | None:
    """Часы на каждом буфере ``clock0``. Callback нужно держать живым."""
    if not cfg.overlay.clock:
        return None
    overlay = pipeline.get_by_name(CLOCK_ELEMENT_NAME)
    if overlay is None:
        return None
    sink = overlay.get_static_pad("video_sink") or overlay.get_static_pad("sink")
    if sink is None:
        return None
    fmt = cfg.overlay.time_format

    def _on_buffer(_pad: Any, _info: Any) -> Any:
        overlay.set_property("text", format_overlay_clock(fmt))
        return gst.PadProbeReturn.OK

    sink.add_probe(gst.PadProbeType.BUFFER, _on_buffer)
    return _on_buffer


def attach_parktronic_overlay(
    pipeline: Any, cfg: Config, camera: CameraConfig
) -> Any | None:
    """Cairo: направляющие 1 м / 2 м на земле. Callback держать живым."""
    if not camera.parktronic:
        return None
    overlay = pipeline.get_by_name(PARK_ELEMENT_NAME)
    if overlay is None:
        return None
    width = cfg.video.width
    height = cfg.video.height

    params = camera.parktronic_overlay
    if params is None:
        return None

    def _on_draw(_overlay: Any, context: Any, _timestamp: Any, _duration: Any) -> None:
        draw_parktronic_guides(context, width, height, params)

    overlay.connect("draw", _on_draw)
    return _on_draw


def attach_undistort_probe(
    pipeline: Any, cfg: Config, camera: CameraConfig, gst: Any
) -> Any | None:
    """Brown remap на src ``undist0``, до cairooverlay. Callback держать живым."""
    if not camera.undistort:
        return None
    params = camera.undistort_params
    if params is None:
        return None
    elem = pipeline.get_by_name(UNDIST_ELEMENT_NAME)
    if elem is None:
        return None
    src = elem.get_static_pad("src")
    if src is None:
        return None
    maps = build_maps(
        cfg.video.width,
        cfg.video.height,
        k1=params.k1,
        k2=params.k2,
        crop=params.crop,
    )
    width = cfg.video.width
    height = cfg.video.height
    logged_fail = False

    def _on_buffer(_pad: Any, info: Any) -> Any:
        nonlocal logged_fail
        buf = info.get_buffer()
        if buf is None:
            return gst.PadProbeReturn.OK
        try:
            remap_gst_buffer(buf, maps, width, height, gst, info)
        except Exception as exc:  # noqa: BLE001
            if not logged_fail:
                logger.warning("%s undistort remap failed: %s", camera.id, exc)
                logged_fail = True
        return gst.PadProbeReturn.OK

    src.add_probe(gst.PadProbeType.BUFFER, _on_buffer)
    return _on_buffer


def _queued_buffers(appsrc: Any) -> int:
    try:
        return int(appsrc.get_property("current-level-buffers"))
    except Exception:  # noqa: BLE001
        return 0


def _push_encoded_au(
    slot: CameraSlot,
    appsrc: Any,
    source_buffer: Any,
    *,
    is_delta: bool,
    gst: Any,
) -> None:
    # Lifecycle RTSP — на GLib, этот callback — на streaming-потоке appsink.
    # Членство, готовность и push сериализуем: иначе appsrc снимут mid-push.
    with slot._encoded_lock:
        if appsrc not in slot.encoded_sources:
            return
        source_id = id(appsrc)
        if _queued_buffers(appsrc):
            # Выкинутый reference-кадр ломает GOP. Этому клиенту — только со следующего IDR.
            slot._encoded_ready_ids.discard(source_id)
        if source_id not in slot._encoded_ready_ids and is_delta:
            return

        # Копируем только заголовок GstBuffer. GstMemory AU — refcount, шарится с appsrc.
        buffer = source_buffer.copy()
        buffer.pts = gst.CLOCK_TIME_NONE
        buffer.dts = gst.CLOCK_TIME_NONE
        result = appsrc.emit("push-buffer", buffer)
        if result == gst.FlowReturn.OK and not is_delta:
            slot._encoded_ready_ids.add(source_id)
        elif result != gst.FlowReturn.OK:
            slot._encoded_ready_ids.discard(source_id)


def attach_encoded_bridge(
    slot: CameraSlot,
    pipeline: Any,
    gst: Any,
    *,
    on_first_sample: Any | None = None,
) -> None:
    """Только свежие AU из appsink в активные RTSP appsrc."""
    sink = pipeline.get_by_name(ENCODED_SINK_ELEMENT_NAME)
    if sink is None:
        raise RuntimeError("appsrc mode requires encoded_sink appsink")
    first_sample_seen = False

    def _new_sample(appsink: Any) -> Any:
        nonlocal first_sample_seen
        sample = appsink.emit("pull-sample")
        source_buffer = sample.get_buffer() if sample is not None else None
        if source_buffer is None:
            return gst.FlowReturn.OK
        notify_ready = False
        with slot._encoded_lock:
            if not first_sample_seen:
                first_sample_seen = True
                notify_ready = True
        if notify_ready and on_first_sample is not None:
            on_first_sample(pipeline)
        is_delta = source_buffer.has_flags(gst.BufferFlags.DELTA_UNIT)
        with slot._encoded_lock:
            sources = tuple(slot.encoded_sources)
        for appsrc in sources:
            _push_encoded_au(
                slot, appsrc, source_buffer, is_delta=is_delta, gst=gst
            )
        return gst.FlowReturn.OK

    slot._encoded_sink = sink
    slot._encoded_sample_cb = _new_sample
    slot._encoded_sink_handler_id = sink.connect("new-sample", _new_sample)


def _bus_failure(message: Any, gst: Any) -> tuple[str, str] | None:
    if message.type == gst.MessageType.ERROR:
        err, debug = message.parse_error()
        detail = err.message if err else "unknown error"
        combined = f"{detail} ({debug})" if debug else detail
        if is_ignorable_argus_bus_error(combined):
            return (combined, "argus-correctable")
        return (combined, "pipeline error")
    if message.type == gst.MessageType.EOS:
        return "end-of-stream", "pipeline EOS"
    return None


@dataclass
class CameraSlot:
    """Runtime одной камеры. Restart/ABR соседние слоты не трогают."""

    config: CameraConfig
    server: Any
    factory: Any
    mounts: Any
    bitrate_bps: int
    peak_bitrate_bps: int
    fps: int
    publishing: bool = True
    medias: list[Any] = field(default_factory=list)
    capture_pipeline: Any | None = None
    encoded_sources: list[Any] = field(default_factory=list)
    _encoded_ready_ids: set[int] = field(default_factory=set)
    _encoded_lock: Any = field(default_factory=RLock, repr=False)
    restart_attempts: int = 0
    last_error: str | None = None
    state: str = "idle"
    client_count: int = 0
    factory_attached: bool = False
    _restart_source_id: int | None = None
    _playing_timeout_id: int | None = None
    _stable_playing_id: int | None = None
    _bus_watch_attached: bool = False
    _bus: Any | None = None
    _bus_handler_id: int | None = None
    _argus_correctable_logged: bool = False
    _clock_probe_cb: Any | None = None
    _park_draw_cb: Any | None = None
    _undistort_probe_cb: Any | None = None
    _encoded_sink: Any | None = None
    _encoded_sink_handler_id: int | None = None
    _encoded_sample_cb: Any | None = None
    # fps caps энкодера. max-rate после PLAYING не меняется.
    _capture_fps: int | None = None
    _abr_target_fps: int | None = None
    _abr_acc: int = 0
    _abr_lock: Any = field(default_factory=RLock, repr=False)
    _abr_probe_cb: Any | None = None
    capture_generation: int = 0
    _capture_started_at: float | None = None
    _media_bus_watches: dict[int, tuple[Any, int | None]] = field(
        default_factory=dict, repr=False
    )

    def launch_string(
        self,
        cfg: Config,
        *,
        gst: Any | None = None,
        profile_label: str | None = None,
    ) -> str:
        return build_capture(
            cfg,
            self.config,
            gst=gst,
            bitrate_bps=self.bitrate_bps,
            peak_bitrate_bps=self.peak_bitrate_bps,
            fps=self.fps,
            profile_label=profile_label,
        )

    def rtsp_factory_launch(
        self,
    ) -> str:
        return build_rtsp_appsrc(self.fps)

    def rtsp_url(self, cfg: Config) -> str:
        return f"rtsp://{cfg.rtsp.host}:{self.config.rtsp_port}{self.config.mount}"

    def cancel_glib_timer(self, glib: Any, name: str) -> None:
        source_id = getattr(self, name)
        setattr(self, name, None)
        if source_id is not None and glib is not None:
            glib.source_remove(source_id)

    def cancel_start_timers(self, glib: Any) -> None:
        self.cancel_glib_timer(glib, "_playing_timeout_id")
        self.cancel_glib_timer(glib, "_stable_playing_id")

    def arm_playing_timeout(self, glib: Any, callback: Any) -> None:
        self.cancel_glib_timer(glib, "_playing_timeout_id")
        if glib is None:
            return
        self._playing_timeout_id = glib.timeout_add(
            PLAYING_TIMEOUT_MS, callback, self
        )

    def arm_stable_playing(
        self, glib: Any, timeout_ms: int, callback: Any, pipeline: Any
    ) -> None:
        self.cancel_glib_timer(glib, "_stable_playing_id")
        if glib is None:
            return
        self._stable_playing_id = glib.timeout_add(
            timeout_ms, callback, self, pipeline
        )

    def hung_pipeline(self) -> Any | None:
        self._playing_timeout_id = None
        pipeline = self.capture_pipeline
        if pipeline is None or self.state == "playing":
            return None
        return pipeline

    def apply_abr(
        self,
        cfg: Config,
        *,
        bitrate_bps: int,
        fps: int,
        profile_label: str,
        force_keyframe_fn: Any,
    ) -> None:
        peak = max(bitrate_bps, int(bitrate_bps * DEFAULT_PEAK_BITRATE_RATIO))
        kbps = max(1, bitrate_bps // 1000)
        previous_bitrate = self.bitrate_bps
        previous_peak = self.peak_bitrate_bps
        previous_fps = self.fps
        pipeline = self.capture_pipeline
        if pipeline is None:
            self.bitrate_bps = bitrate_bps
            self.peak_bitrate_bps = peak
            self.fps = fps
            return
        rate = None
        enc = None
        with self._abr_lock:
            previous_target = self._abr_target_fps
            if self._capture_fps is None:
                self._capture_fps = previous_fps
            self._abr_target_fps = fps
            self._abr_acc = 0
        try:
            rate = pipeline.get_by_name(RATE_ELEMENT_NAME)
            if rate is None:
                raise RuntimeError(f"missing {RATE_ELEMENT_NAME}")
            enc = pipeline.get_by_name(ENCODER_ELEMENT_NAME)
            if enc is None:
                raise RuntimeError(f"missing {ENCODER_ELEMENT_NAME}")
            if not cfg.is_emulate:
                enc.set_property("bitrate", bitrate_bps)
                _try_set(enc, "peak-bitrate", peak)
                _try_set(enc, "vbv-size", jetson_vbv_size(cfg.encoder, bitrate_bps))
                _try_set(enc, "qp-range", cfg.encoder.qp_range)
                gop = jetson_iframe_interval(cfg.encoder, fps)
                _try_set(enc, "iframeinterval", gop)
                _try_set(enc, "idrinterval", gop)
                force_keyframe_fn(enc)
            else:
                enc.set_property("bitrate", kbps)
            ovl = pipeline.get_by_name(OVERLAY_ELEMENT_NAME)
            if ovl is not None:
                ovl.set_property("text", f"abr {profile_label} {fps}fps {kbps}k")
            frame_ns = max(1, round(1_000_000_000 / max(1, fps)))
            with self._encoded_lock:
                for appsrc in self.encoded_sources:
                    _try_set(appsrc, "max-latency", frame_ns)
        except Exception as exc:  # noqa: BLE001
            # Откат: не объявлять запрошенный профиль, если пайплайн принял его частично.
            with self._abr_lock:
                self._abr_target_fps = previous_target
                self._abr_acc = 0
            try:
                if enc is not None:
                    if cfg.is_emulate:
                        enc.set_property("bitrate", max(1, previous_bitrate // 1000))
                    else:
                        enc.set_property("bitrate", previous_bitrate)
                        _try_set(enc, "peak-bitrate", previous_peak)
                        _try_set(
                            enc,
                            "vbv-size",
                            jetson_vbv_size(cfg.encoder, previous_bitrate),
                        )
                        old_gop = jetson_iframe_interval(cfg.encoder, previous_fps)
                        _try_set(enc, "iframeinterval", old_gop)
                        _try_set(enc, "idrinterval", old_gop)
            except Exception as rollback_exc:  # noqa: BLE001
                logger.error(
                    "%s ABR rollback failed: %s", self.config.id, rollback_exc
                )
            raise RuntimeError(
                f"{self.config.id} failed to apply ABR profile {profile_label}: {exc}"
            ) from exc
        self.bitrate_bps = bitrate_bps
        self.peak_bitrate_bps = peak
        self.fps = fps

    def _install_abr_throttle(self, pipeline: Any, gst: Any) -> None:
        """Дропать лишние кадры на src videorate, не меняя caps энкодера."""
        if self._abr_probe_cb is not None:
            return
        rate = pipeline.get_by_name(RATE_ELEMENT_NAME)
        if rate is None:
            return
        pad = rate.get_static_pad("src")
        probe_type = getattr(getattr(gst, "PadProbeType", None), "BUFFER", None)
        drop = getattr(getattr(gst, "PadProbeReturn", None), "DROP", None)
        ok = getattr(getattr(gst, "PadProbeReturn", None), "OK", None)
        if pad is None or probe_type is None or drop is None or ok is None:
            return

        def _on_buffer(_pad: Any, info: Any) -> Any:
            buf = info.get_buffer() if info is not None else None
            if buf is None:
                return ok
            with self._abr_lock:
                keep, self._abr_acc = abr_keep_frame(
                    self._abr_acc,
                    capture_fps=self._capture_fps or self.fps,
                    target_fps=self._abr_target_fps or self.fps,
                )
            return ok if keep else drop

        pad.add_probe(probe_type, _on_buffer)
        self._abr_probe_cb = _on_buffer

    def start_capture(
        self,
        cfg: Config,
        gst: Any,
        profile_label: str,
        *,
        before_play: Any | None = None,
        on_first_sample: Any | None = None,
    ) -> Any:
        pipeline = gst.parse_launch(
            self.launch_string(cfg, gst=gst, profile_label=profile_label)
        )
        self.capture_generation += 1
        self._capture_started_at = time.monotonic()
        self.capture_pipeline = pipeline
        self.state = "starting"
        self._argus_correctable_logged = False
        logger.info(
            "%s capture start generation=%s pipeline=%#x sensor=%s "
            "restart_attempt=%s",
            self.config.id,
            self.capture_generation,
            id(pipeline),
            self.config.sensor_id,
            self.restart_attempts,
        )
        self._clock_probe_cb = attach_clock_overlay(pipeline, cfg, gst)
        self._park_draw_cb = attach_parktronic_overlay(pipeline, cfg, self.config)
        self._undistort_probe_cb = attach_undistort_probe(
            pipeline, cfg, self.config, gst
        )
        attach_encoded_bridge(
            self,
            pipeline,
            gst,
            on_first_sample=on_first_sample,
        )
        with self._abr_lock:
            self._capture_fps = self.fps
            self._abr_target_fps = self.fps
            self._abr_acc = 0
            self._abr_probe_cb = None
        self._install_abr_throttle(pipeline, gst)
        if before_play is not None:
            before_play()
        result = pipeline.set_state(gst.State.PLAYING)
        logger.info(
            "%s capture PLAYING requested generation=%s pipeline=%#x result=%s",
            self.config.id,
            self.capture_generation,
            id(pipeline),
            _state_name(result),
        )
        if result == gst.StateChangeReturn.FAILURE:
            self.detach_bus_watch()
            self._release_capture_pipeline(pipeline, gst, reason="PLAYING rejected")
            self.capture_pipeline = None
            self._capture_started_at = None
            self.state = "error"
            raise RuntimeError(f"{self.config.id} capture failed to reach PLAYING")
        return result

    def mark_playing(self) -> None:
        self.state = "playing"
        self.last_error = None

    def _release_capture_pipeline(
        self, pipeline: Any, gst: Any, *, reason: str
    ) -> bool:
        """Перевести pipeline в NULL и дождаться освобождения Argus/NVMM."""
        generation = self.capture_generation
        pipeline_id = id(pipeline)
        started_at = time.monotonic()
        try:
            before_result, before_state, before_pending = _pipeline_state(pipeline, 0)
        except Exception as exc:  # noqa: BLE001
            before_result = before_state = before_pending = f"unavailable: {exc}"
        logger.info(
            "%s capture release begin generation=%s pipeline=%#x reason=%s "
            "state_result=%s state=%s pending=%s",
            self.config.id,
            generation,
            pipeline_id,
            reason,
            _state_name(before_result),
            _state_name(before_state),
            _state_name(before_pending),
        )
        try:
            request_result = pipeline.set_state(gst.State.NULL)
            wait_result, state, pending = _pipeline_state(
                pipeline, PIPELINE_NULL_TIMEOUT_NS
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "%s capture release failed generation=%s pipeline=%#x "
                "reason=%s error=%s",
                self.config.id,
                generation,
                pipeline_id,
                reason,
                exc,
            )
            return False
        elapsed_ms = (time.monotonic() - started_at) * 1000
        released = state == gst.State.NULL
        log = logger.info if released else logger.warning
        log(
            "%s capture release %s generation=%s pipeline=%#x reason=%s "
            "request=%s wait=%s state=%s pending=%s elapsed_ms=%.1f",
            self.config.id,
            "complete" if released else "incomplete",
            generation,
            pipeline_id,
            reason,
            _state_name(request_result),
            _state_name(wait_result),
            _state_name(state),
            _state_name(pending),
            elapsed_ms,
        )
        return released

    def stop_capture(self, gst: Any) -> None:
        pipeline = self.capture_pipeline
        previous_state = self.state
        self.detach_bus_watch()
        self.state = "stopping"
        self._clock_probe_cb = None
        self._park_draw_cb = None
        self._undistort_probe_cb = None
        sink = self._encoded_sink
        handler_id = self._encoded_sink_handler_id
        self._encoded_sink = None
        self._encoded_sink_handler_id = None
        self._encoded_sample_cb = None
        if sink is not None and handler_id is not None:
            try:
                sink.disconnect(handler_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("%s disconnect encoded sink: %s", self.config.id, exc)
        with self._encoded_lock:
            self._encoded_ready_ids.clear()
        if pipeline is None:
            self.capture_pipeline = None
            self._capture_started_at = None
            return
        self._release_capture_pipeline(
            pipeline,
            gst,
            reason=f"stop from {previous_state}",
        )
        # Удалять последнюю сильную ссылку можно только после ожидания NULL:
        # иначе новый pipeline успевает открыть CaptureSession до teardown старого.
        if self.capture_pipeline is pipeline:
            self.capture_pipeline = None
        self._capture_started_at = None

    def stop_medias(self, gst: Any) -> None:
        with self._encoded_lock:
            self.encoded_sources.clear()
            self._encoded_ready_ids.clear()
        medias = list(self.medias)
        self.medias.clear()
        self.client_count = 0
        for media in medias:
            self.detach_media_bus_watch(media)
            try:
                if not media.unprepare():
                    media.set_state(gst.State.NULL, [])
            except Exception as exc:  # noqa: BLE001
                logger.debug("%s media NULL: %s", self.config.id, exc)

    def attach_media_bus_watch(
        self,
        media: Any,
        gst: Any,
        *,
        on_failure: Any,
    ) -> None:
        """Смотреть ошибки RTSP media отдельно от capture-пайплайна."""
        media_id = id(media)
        if media_id in self._media_bus_watches:
            return
        element = media.get_element()
        bus = element.get_bus() if element is not None else None
        if bus is None:
            return

        def _on_message(_bus: Any, message: Any) -> None:
            failure = _bus_failure(message, gst)
            if failure is None:
                return
            detail, label = failure
            logger.warning("%s RTSP %s: %s", self.config.id, label, detail)
            on_failure(media, detail)

        bus.add_signal_watch()
        handler_id = bus.connect("message", _on_message)
        self._media_bus_watches[media_id] = (bus, handler_id)

    def detach_media_bus_watch(self, media: Any) -> None:
        watch = self._media_bus_watches.pop(id(media), None)
        if watch is None:
            return
        bus, handler_id = watch
        if handler_id is not None:
            try:
                bus.disconnect(handler_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("%s disconnect media bus: %s", self.config.id, exc)
        try:
            bus.remove_signal_watch()
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s remove media bus watch: %s", self.config.id, exc)

    def attach_factory(
        self,
    ) -> None:
        if self.factory_attached:
            return
        launch = self.rtsp_factory_launch()
        self.factory.set_launch(f"( {launch} )")
        try:
            self.mounts.remove_factory(self.config.mount)
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s replace factory: %s", self.config.id, exc)
        self.mounts.add_factory(self.config.mount, self.factory)
        self.factory_attached = True

    def remove_factory(self) -> None:
        if not self.factory_attached:
            return
        try:
            self.mounts.remove_factory(self.config.mount)
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s remove_factory: %s", self.config.id, exc)
        finally:
            self.factory_attached = False

    def attach_bus_watch(
        self,
        gst: Any,
        *,
        on_failure: Any,
    ) -> None:
        pipeline = self.capture_pipeline
        if pipeline is None or self._bus_watch_attached:
            return
        bus = pipeline.get_bus()
        if bus is None:
            return

        def _on_message(_bus: Any, message: Any) -> None:
            if self.capture_pipeline is not pipeline:
                return
            failure = _bus_failure(message, gst)
            if failure is not None:
                detail, label = failure
                if label == "argus-correctable":
                    if not self._argus_correctable_logged:
                        logger.info(
                            "%s ignoring Argus correctable bus error: %s",
                            self.config.id,
                            detail,
                        )
                        self._argus_correctable_logged = True
                    return
                # EOS приходит вслед за ERROR того же отказа. Не затирать уже
                # записанную конкретную причину generic "end-of-stream":
                # иначе в логе и в status остаётся только EOS без повода.
                is_eos = message.type == gst.MessageType.EOS
                if not (is_eos and self.last_error):
                    self.last_error = detail
                self.state = "error"
                uptime_ms = (
                    (time.monotonic() - self._capture_started_at) * 1000
                    if self._capture_started_at is not None
                    else -1.0
                )
                logger.warning(
                    "%s %s generation=%s pipeline=%#x uptime_ms=%.1f: %s",
                    self.config.id,
                    label,
                    self.capture_generation,
                    id(pipeline),
                    uptime_ms,
                    self.last_error,
                )
                on_failure(pipeline, self.last_error)

        bus.add_signal_watch()
        self._bus = bus
        self._bus_handler_id = bus.connect("message", _on_message)
        self._bus_watch_attached = True

    def detach_bus_watch(self) -> None:
        bus = self._bus
        handler_id = self._bus_handler_id
        self._bus = None
        self._bus_handler_id = None
        self._bus_watch_attached = False
        if bus is None:
            return
        if handler_id is not None:
            try:
                bus.disconnect(handler_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("%s disconnect bus watch: %s", self.config.id, exc)
        try:
            bus.remove_signal_watch()
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s remove bus watch: %s", self.config.id, exc)

    def to_status(self, cfg: Config) -> dict[str, Any]:
        return {
            "id": self.config.id,
            "sensor_id": self.config.sensor_id,
            "rtsp_port": self.config.rtsp_port,
            "mount": self.config.mount,
            "enabled": self.publishing,
            "pattern": self.config.pattern,
            "source_type": self.config.source_type,
            "nav_enabled": self.config.nav_enabled,
            "fps": self.fps,
            "bitrate_bps": self.bitrate_bps,
            "bitrate_kbps": max(1, self.bitrate_bps // 1000),
            "peak_bitrate_bps": self.peak_bitrate_bps,
            "url": self.rtsp_url(cfg),
            "state": self.state,
            "clients": self.client_count,
            "restart_attempts": self.restart_attempts,
            "capture_generation": self.capture_generation,
            "last_error": self.last_error,
        }


def create_rtsp_slot(
    cam: CameraConfig,
    *,
    cfg: Config,
    gst_rtsp_server: Any,
    bitrate_bps: int,
    fps: int,
    server: Any | None = None,
    mounts: Any | None = None,
) -> CameraSlot:
    # Один RTSPServer на порт: несколько mount на :8554 как у старого сервера.
    if server is None:
        server = gst_rtsp_server.RTSPServer.new()
        server.set_address(cfg.rtsp.host)
        server.set_service(str(cam.rtsp_port))
        mounts = server.get_mount_points()
    elif mounts is None:
        mounts = server.get_mount_points()
    factory = gst_rtsp_server.RTSPMediaFactory.new()
    factory.set_shared(True)
    factory.set_latency(RTSP_FACTORY_LATENCY_MS)
    mode = SUSPEND_MODE_MAP[cfg.rtsp.suspend_mode]
    factory.set_suspend_mode(getattr(gst_rtsp_server.RTSPSuspendMode, mode))
    factory.set_eos_shutdown(False)
    peak = max(bitrate_bps, int(bitrate_bps * DEFAULT_PEAK_BITRATE_RATIO))
    slot = CameraSlot(
        config=cam,
        server=server,
        factory=factory,
        mounts=mounts,
        bitrate_bps=bitrate_bps,
        peak_bitrate_bps=peak,
        fps=fps,
        publishing=False,
    )
    factory.set_launch(f"( {slot.rtsp_factory_launch()} )")
    return slot
