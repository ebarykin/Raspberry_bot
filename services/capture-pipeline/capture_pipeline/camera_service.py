"""Оркестратор слотов: старт capture, RTSP mount, ABR, restart."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from typing import Any, Callable

from .argus_ready import restart_nvargus_daemon, wait_for_argus
from .camera_identity import discover_serial_to_sensor_id
from .camera_slot import CameraSlot, create_rtsp_slot, force_keyframe
from .config import DEFAULT_PEAK_BITRATE_RATIO, AbrProfile, CameraConfig, Config
from .pipeline_recovery import STABLE_PLAYING_MS, is_argus_provider_failure, next_backoff_ms
from .pipelines.teleop import (
    ENCODED_SRC_ELEMENT_NAME,
    ENCODER_ELEMENT_NAME,
    RTSP_FACTORY_LATENCY_MS,
)

logger = logging.getLogger(__name__)

# Сколько раз за жизнь процесса перезапускать nvargus-daemon.
# Дальше выход: kubelet поднимет контейнер заново, init повторит рестарт.
_ARGUS_DAEMON_RESTART_LIMIT = 3


class CameraService:
    """Единственная точка мутации пайплайнов. Control WS ходит сюда."""

    def __init__(
        self,
        cfg: Config,
        *,
        Gst: Any,
        GstRtspServer: Any,
        GstVideo: Any | None = None,
        glib: Any | None = None,
        on_fatal: Callable[[str], None] | None = None,
    ) -> None:
        self._cfg = cfg
        self._Gst = Gst
        self._GstRtspServer = GstRtspServer
        self._GstVideo = GstVideo
        self._glib = glib
        self._on_fatal = on_fatal
        self._argus_restart_lock = threading.Lock()
        self._argus_restarting = False
        self._argus_restart_count = 0
        self._slots: dict[str, CameraSlot] = {}
        active = cfg.abr.profiles[cfg.abr.default_profile]
        self._profile = cfg.abr.default_profile
        self._fps = active.fps
        self._bitrate_bps = active.bitrate_bps
        self._profile_changed_at = 0.0
        self._start_queue: list[CameraSlot] = []
        self._start_in_flight: CameraSlot | None = None
        self._stagger_source_id: int | None = None
        self._rtsp_by_port: dict[int, tuple[Any, Any]] = {}


    @property
    def slots(self) -> dict[str, CameraSlot]:
        return self._slots

    @property
    def profile(self) -> str:
        return self._profile

    def start_configured(self) -> None:
        """Создать слоты включённых камер и поставить их в очередь запуска."""

        serial_map: dict[int, int] | None = None
        if any(cam.enabled and cam.serial is not None for cam in self._cfg.cameras):
            serial_map = discover_serial_to_sensor_id()
            logger.info("camera discovery resolved serials: %s", serial_map)

        enabled_slots: list[CameraSlot] = []
        for cam in self._cfg.cameras:
            if not cam.enabled:
                logger.info("%s configured but disabled at startup", cam.id)
                continue
            if cam.serial is not None:
                resolved = (serial_map or {}).get(cam.serial)
                if resolved is None:
                    slot = self._create_slot(cam)
                    slot.publishing = True
                    slot.state = "error"
                    slot.last_error = f"serial {cam.serial} not found at startup"
                    self._slots[cam.id] = slot
                    logger.error(
                        "%s serial=%s not found among discovered cameras; "
                        "leaving in error, other cameras continue",
                        cam.id,
                        cam.serial,
                    )
                    continue
                cam = replace(cam, sensor_id=resolved)
            slot = self._create_slot(cam)
            slot.publishing = True
            self._slots[cam.id] = slot
            src = (
                f"src={cam.source_type}"
                if cam.source_type != "nvarguscamerasrc"
                else f"sensor={cam.sensor_id} flip={cam.flip_method}"
            )
            logger.info(
                "%s RTSP server initialized %s (%s); capture pending",
                cam.id,
                slot.rtsp_url(self._cfg),
                src,
            )
            enabled_slots.append(slot)
        if not enabled_slots:
            errors = [
                f"{slot.config.id}: {slot.last_error}" for slot in self._slots.values()
            ]
            raise RuntimeError(
                "no camera could be started at startup: " + "; ".join(errors)
            )
        self._enqueue_starts(enabled_slots)

    def status(self) -> dict[str, Any]:
        """Снимок ABR и слотов для control WS / healthcheck."""

        profiles = {
            name: {"fps": p.fps, "bitrate_kbps": p.bitrate_kbps}
            for name, p in self._cfg.abr.profiles.items()
        }
        return {
            "source": self._cfg.source,
            "rtsp_transport": "appsink-appsrc",
            "profile": self._profile,
            "fps": self._fps,
            "bitrate_bps": self._bitrate_bps,
            "bitrate_kbps": max(1, self._bitrate_bps // 1000),
            "profiles": profiles,
            "capture": {
                "stagger_ms": self._cfg.capture.stagger_ms,
                "rtsp_suspend_mode": self._cfg.rtsp.suspend_mode,
            },
            "cameras": [slot.to_status(self._cfg) for slot in self._slots.values()],
        }

    def set_profile(self, profile: str, *, force: bool = False) -> dict[str, Any]:
        """Применить ABR-профиль ко всем слотам камер.

        Для активных слотов обновляет FPS и битрейт capture-пайплайна, а для
        неактивных сохраняет параметры для следующего запуска. Повторный вызов
        идемпотентен; ограничение ``min_dwell_s`` пропускается при ``force=True``.

        Возвращает общий статус сервиса с результатом применения профиля.
        Неизвестное имя профиля приводит к ``ValueError``.
        """

        key = str(profile).strip().lower()
        if key not in self._cfg.abr.profiles:
            raise ValueError(
                f"unknown profile: {profile!r}, use one of {sorted(self._cfg.abr.profiles)}"
            )
        if key == self._profile:
            return self._abr_out(False, key, reason="already_active")
        dwell_s = float(self._cfg.abr.min_dwell_s)
        now = time.monotonic()
        if not force and dwell_s > 0 and self._profile_changed_at > 0:
            elapsed = now - self._profile_changed_at
            if elapsed < dwell_s:
                logger.info(
                    "ABR profile=%s ignored (dwell %.1fs < %.1fs, stay %s)",
                    key, elapsed, dwell_s, self._profile,
                )
                return self._abr_out(
                    False, key, reason="min_dwell",
                    abr_dwell_remaining_s=round(dwell_s - elapsed, 2),
                )
        active = self._cfg.abr.profiles[key]
        applied: list[tuple[CameraSlot, int, int, int]] = []
        failure: str | None = None
        failed_slot: CameraSlot | None = None
        for slot in self._slots.values():
            previous = (
                slot,
                slot.bitrate_bps,
                slot.peak_bitrate_bps,
                slot.fps,
            )
            try:
                self._apply_profile_to_slot(slot, active, key)
            except Exception as exc:  # noqa: BLE001
                failure = f"{slot.config.id}: {exc}"
                failed_slot = slot
                break
            applied.append(previous)
        if failure is not None:
            rollback_failures = self._rollback_slots(applied, self._profile)
            if failed_slot is not None:
                self._restart_after_abr_error(failed_slot, failure)
            detail = f"ABR profile {key!r} was not applied to every camera: {failure}"
            if rollback_failures:
                detail += "; rollback failed: " + "; ".join(rollback_failures)
            raise RuntimeError(detail)
        self._profile = key
        self._bitrate_bps = active.bitrate_bps
        self._fps = active.fps
        self._profile_changed_at = now
        logger.info(
            "ABR profile=%s fps=%s bitrate_kbps=%s", key, active.fps, active.bitrate_kbps
        )
        return self._abr_out(True, key)

    def set_profile_from_level(self, level: str, *, force: bool = False) -> dict[str, Any]:
        """Применить ABR-профиль с тем же именем, что link-quality ``level``.

        ``level`` должен быть ключом ``abr.profiles``. ``force=True`` пропускает
        ``min_dwell_s``. Неизвестный уровень вызывает ``ValueError``.
        """

        return self.set_profile(level, force=force)

    def shutdown(self) -> None:
        """Остановить все слоты камер и завершить работу сервиса."""
        active = [
            f"{slot.config.id}:{slot.state}:g{slot.capture_generation}"
            for slot in self._slots.values()
            if slot.capture_pipeline is not None
        ]
        logger.info("camera service shutdown begin active=%s", active)
        self._cancel_glib_source(self._stagger_source_id)
        self._stagger_source_id = None
        self._start_queue.clear()
        self._start_in_flight = None
        for slot in self._slots.values():
            if slot.publishing:
                self._detach_factory(slot)
                slot.publishing = False
            else:
                self._stop_slot_runtime(slot)
            slot.state = "idle"
        logger.info("camera service shutdown complete")

    def _abr_out(
        self, applied: bool, requested: str, *, reason: str | None = None, **extra: Any
    ) -> dict[str, Any]:
        """Сформировать ответ о результате применения ABR-профиля.

        Формат ответа: поля :meth:`status`, ``abr_applied: bool``,
        ``abr_requested: str``, опциональный ``abr_ignore_reason: str``
        и дополнительные поля из ``extra``.
        """

        out = self.status()
        out["abr_applied"] = applied
        out["abr_requested"] = requested
        if reason is not None:
            out["abr_ignore_reason"] = reason
        out.update(extra)
        return out

    def _apply_to_slot(
        self, slot: CameraSlot, *, bitrate_bps: int, fps: int, profile_label: str
    ) -> None:
        slot.apply_abr(
            self._cfg, bitrate_bps=bitrate_bps, fps=fps,
            profile_label=profile_label, force_keyframe_fn=self._force_keyframe,
        )

    def _apply_profile_to_slot(
        self,
        slot: CameraSlot,
        profile: AbrProfile,
        profile_label: str,
    ) -> None:
        """Применить профиль к работающему слоту или сохранить для его старта."""
        if slot.publishing:
            self._apply_to_slot(
                slot,
                bitrate_bps=profile.bitrate_bps,
                fps=profile.fps,
                profile_label=profile_label,
            )
            return
        slot.bitrate_bps = profile.bitrate_bps
        slot.peak_bitrate_bps = max(
            profile.bitrate_bps,
            int(profile.bitrate_bps * DEFAULT_PEAK_BITRATE_RATIO),
        )
        slot.fps = profile.fps

    def _rollback_slots(
        self,
        applied: list[tuple[CameraSlot, int, int, int]],
        profile_label: str,
    ) -> list[str]:
        """Откатить уже обновлённые слоты после частичного отказа ABR."""
        failures: list[str] = []
        for slot, bitrate_bps, peak_bitrate_bps, fps in reversed(applied):
            try:
                if slot.publishing:
                    self._apply_to_slot(
                        slot,
                        bitrate_bps=bitrate_bps,
                        fps=fps,
                        profile_label=profile_label,
                    )
                else:
                    slot.bitrate_bps = bitrate_bps
                    slot.peak_bitrate_bps = peak_bitrate_bps
                    slot.fps = fps
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{slot.config.id}: {exc}")
                self._restart_after_abr_error(slot, str(exc))
        return failures

    def _restart_after_abr_error(self, slot: CameraSlot, reason: str) -> None:
        """Пересоздать пайплайн, если его ABR-состояние нельзя подтвердить."""
        pipeline = slot.capture_pipeline
        if not slot.publishing or pipeline is None:
            return
        logger.error("%s restarting after ABR inconsistency: %s", slot.config.id, reason)
        self._capture_failed(slot, pipeline, f"ABR inconsistency: {reason}")

    def _force_keyframe(self, enc: Any) -> None:
        force_keyframe(enc, self._Gst, self._GstVideo)

    def _activate_encoded_source(self, slot: CameraSlot, appsrc: Any) -> None:
        with slot._encoded_lock:
            if appsrc not in slot.encoded_sources:
                slot.encoded_sources.append(appsrc)
            slot._encoded_ready_ids.discard(id(appsrc))
        capture = slot.capture_pipeline
        enc = (
            capture.get_by_name(ENCODER_ELEMENT_NAME)
            if capture is not None
            else None
        )
        if enc is not None:
            self._force_keyframe(enc)

    def _remove_media(
        self, slot: CameraSlot, media: Any, appsrc: Any | None
    ) -> None:
        slot.detach_media_bus_watch(media)
        try:
            slot.medias.remove(media)
        except ValueError:
            pass
        with slot._encoded_lock:
            if appsrc in slot.encoded_sources:
                slot.encoded_sources.remove(appsrc)
            if appsrc is not None:
                slot._encoded_ready_ids.discard(id(appsrc))
        self._update_client_count(slot, len(slot.medias))

    def _rtsp_media_failed(
        self, slot: CameraSlot, media: Any, appsrc: Any | None, reason: str
    ) -> None:
        self._remove_media(slot, media, appsrc)
        try:
            if not media.unprepare():
                media.set_state(self._Gst.State.NULL, [])
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s failed to reset RTSP media after %s: %s", slot.config.id, reason, exc)

    def _register_media(self, slot: CameraSlot, media: Any) -> None:
        try:
            media.set_latency(RTSP_FACTORY_LATENCY_MS)
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s media.set_latency: %s", slot.config.id, exc)
        if media not in slot.medias:
            slot.medias.append(media)
        self._update_client_count(slot, len(slot.medias))

        element = media.get_element()
        appsrc = (
            element.get_by_name(ENCODED_SRC_ELEMENT_NAME)
            if element is not None
            else None
        )
        if appsrc is None:
            logger.warning("%s RTSP media missing encoded_src", slot.config.id)
        else:
            # prepare() ждёт первый AU в appsrc; ждать сигнал prepared здесь — deadlock.
            self._activate_encoded_source(slot, appsrc)
        media.connect(
            "unprepared",
            lambda _media: self._remove_media(slot, media, appsrc),
        )
        slot.attach_media_bus_watch(
            media,
            self._Gst,
            on_failure=lambda failed_media, reason: self._rtsp_media_failed(
                slot, failed_media, appsrc, reason
            ),
        )

    def _create_slot(self, cam: CameraConfig) -> CameraSlot:
        existing = self._rtsp_by_port.get(cam.rtsp_port)
        slot = create_rtsp_slot(
            cam,
            cfg=self._cfg,
            gst_rtsp_server=self._GstRtspServer,
            bitrate_bps=self._bitrate_bps,
            fps=self._fps,
            server=existing[0] if existing else None,
            mounts=existing[1] if existing else None,
        )

        slot.factory.connect(
            "media-constructed",
            lambda _factory, media: self._register_media(slot, media),
        )
        if existing is None:
            if slot.server.attach(None) == 0:
                raise RuntimeError(
                    f"failed to attach RTSP server cam={cam.id} port={cam.rtsp_port}"
                )
            self._rtsp_by_port[cam.rtsp_port] = (slot.server, slot.mounts)
        return slot

    def _detach_factory(self, slot: CameraSlot) -> None:
        self._stop_slot_runtime(slot)

    def _enqueue_starts(self, slots: list[CameraSlot]) -> None:
        self._start_queue.extend(slots)
        self._pump_start_queue()

    def _pump_start_queue(self) -> None:
        if self._argus_restarting or self._start_in_flight is not None:
            return
        while self._start_queue:
            slot = self._start_queue.pop(0)
            if not slot.publishing:
                continue
            self._start_in_flight = slot
            try:
                self._start_slot(slot)
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s capture start failed: %s", slot.config.id, exc)
                self._start_in_flight = None
                self._schedule_restart(slot, reason=str(exc))
                self._schedule_next_start()
                return
            return

    def _schedule_next_start(self) -> None:
        if not self._start_queue:
            return
        delay_ms = max(0, int(self._cfg.capture.stagger_ms))
        if self._glib is not None and delay_ms > 0:
            self._stagger_source_id = self._glib.timeout_add(
                delay_ms, self._stagger_timeout
            )
        else:
            self._pump_start_queue()

    def _stagger_timeout(self) -> None:
        self._stagger_source_id = None
        self._pump_start_queue()

    def _start_slot(self, slot: CameraSlot) -> None:
        slot.start_capture(
            self._cfg,
            self._Gst,
            self._profile,
            before_play=lambda: self._attach_capture_bus(slot),
            on_first_sample=lambda pipeline: self._first_capture_sample(slot, pipeline),
        )
        slot.arm_playing_timeout(self._glib, self._playing_timeout)

    def _first_capture_sample(self, slot: CameraSlot, pipeline: Any) -> None:
        """Завершить запуск в GLib-потоке после первого реально закодированного кадра."""
        if self._glib is None:
            self._capture_playing(slot, pipeline)
            return
        self._glib.idle_add(
            self._capture_playing,
            slot,
            pipeline,
            priority=self._glib.PRIORITY_DEFAULT,
        )

    def _playing_timeout(self, slot: CameraSlot) -> None:
        pipeline = slot.hung_pipeline()
        if pipeline is None:
            return
        logger.warning("%s PLAYING timeout", slot.config.id)
        self._capture_failed(slot, pipeline, "PLAYING timeout")

    def _capture_playing(self, slot: CameraSlot, pipeline: Any) -> None:
        if pipeline is None or slot.capture_pipeline is not pipeline:
            return
        if slot.state == "playing":
            return
        self._finish_capture_start(slot)

    def _finish_capture_start(self, slot: CameraSlot) -> None:
        if slot.state == "playing":
            return
        slot.cancel_start_timers(self._glib)
        slot.mark_playing()
        slot.arm_stable_playing(
            self._glib,
            STABLE_PLAYING_MS,
            self._stable_playing,
            slot.capture_pipeline,
        )
        slot.attach_factory()
        logger.info("%s capture playing appsink→appsrc", slot.config.id)
        if self._start_in_flight is slot:
            self._start_in_flight = None
            self._schedule_next_start()

    def _stable_playing(self, slot: CameraSlot, pipeline: Any) -> None:
        slot._stable_playing_id = None
        if slot.capture_pipeline is pipeline and slot.state == "playing":
            slot.restart_attempts = 0

    def _capture_failed(self, slot: CameraSlot, pipeline: Any, reason: str) -> None:
        if slot.capture_pipeline is not pipeline:
            logger.debug(
                "%s ignored stale capture failure pipeline=%#x current=%s reason=%s",
                slot.config.id,
                id(pipeline),
                (
                    f"{id(slot.capture_pipeline):#x}"
                    if slot.capture_pipeline is not None
                    else "none"
                ),
                reason,
            )
            return
        was_starting = self._start_in_flight is slot
        if not self._cfg.is_emulate and (
            is_argus_provider_failure(reason) or self._argus_restarting
        ):
            logger.error(
                "%s argus provider unavailable generation=%s; "
                "slot restart cannot create a camera provider (%s)",
                slot.config.id,
                slot.capture_generation,
                reason,
            )
            self._stop_capture_for_restart(slot)
            slot.last_error = reason
            slot.state = "error"
            self.note_argus_provider_failure(reason)
            return
        logger.warning(
            "%s capture recovery begin generation=%s pipeline=%#x "
            "was_starting=%s queued=%s reason=%s",
            slot.config.id,
            slot.capture_generation,
            id(pipeline),
            was_starting,
            [queued.config.id for queued in self._start_queue],
            reason,
        )
        self._stop_capture_for_restart(slot)
        self._schedule_restart(slot, reason=reason)
        if was_starting:
            self._schedule_next_start()

    def _stop_capture(self, slot: CameraSlot) -> None:
        slot.stop_capture(self._Gst)

    def _stop_shared_medias(self, slot: CameraSlot) -> None:
        slot.stop_medias(self._Gst)

    def _stop_capture_for_restart(self, slot: CameraSlot) -> None:
        """Остановить capture, не снимая уже поднятый RTSP mount."""
        slot.cancel_start_timers(self._glib)
        if slot in self._start_queue:
            self._start_queue = [s for s in self._start_queue if s is not slot]
        if slot is self._start_in_flight:
            self._start_in_flight = None
        self._stop_capture(slot)

    def _stop_slot_runtime(self, slot: CameraSlot) -> None:
        slot.cancel_start_timers(self._glib)
        slot.cancel_glib_timer(self._glib, "_restart_source_id")
        if slot in self._start_queue:
            self._start_queue = [s for s in self._start_queue if s is not slot]
        if slot is self._start_in_flight:
            self._start_in_flight = None
        self._stop_capture(slot)
        self._stop_shared_medias(slot)
        slot.remove_factory()

    def _update_client_count(self, slot: CameraSlot, count: int) -> None:
        if count == slot.client_count:
            return
        prev = slot.client_count
        slot.client_count = count
        logger.info("%s rtsp clients %s→%s", slot.config.id, prev, count)

    def _attach_capture_bus(self, slot: CameraSlot) -> None:
        slot.attach_bus_watch(
            self._Gst,
            on_failure=lambda pipeline, reason: self._capture_failed(
                slot, pipeline, reason
            ),
        )

    def _cancel_glib_source(self, source_id: int | None) -> None:
        if source_id is not None and self._glib is not None:
            self._glib.source_remove(source_id)

    def note_argus_provider_failure(self, reason: str) -> None:
        """Остановить слоты и один раз перезапустить nvargus-daemon на хосте.

        Init-контейнер при рестарте основного контейнера заново не выполняется,
        поэтому демон рестартится отсюда. Лимит попыток — выход процесса.
        """
        if self._cfg.is_emulate:
            return
        with self._argus_restart_lock:
            if self._argus_restarting:
                logger.info("argus daemon restart already in progress (%s)", reason)
                return
            if self._argus_restart_count >= _ARGUS_DAEMON_RESTART_LIMIT:
                logger.error(
                    "argus daemon restart limit %s reached: %s",
                    _ARGUS_DAEMON_RESTART_LIMIT,
                    reason,
                )
                if self._on_fatal is not None:
                    self._on_fatal(reason)
                return
            self._argus_restarting = True
            self._argus_restart_count += 1
            attempt = self._argus_restart_count
        self._hold_cameras_for_argus_restart(reason)
        logger.error(
            "argus camera provider failed (attempt %s/%s): %s",
            attempt,
            _ARGUS_DAEMON_RESTART_LIMIT,
            reason,
        )
        thread = threading.Thread(
            target=self._restart_argus_daemon,
            args=(attempt, reason),
            name="argus-daemon-restart",
            daemon=True,
        )
        thread.start()

    def _hold_cameras_for_argus_restart(self, reason: str) -> None:
        self._cancel_glib_source(self._stagger_source_id)
        self._stagger_source_id = None
        self._start_queue = []
        self._start_in_flight = None
        for slot in self._slots.values():
            if not slot.publishing:
                continue
            slot.cancel_start_timers(self._glib)
            slot.cancel_glib_timer(self._glib, "_restart_source_id")
            self._stop_capture(slot)
            slot.last_error = reason
            slot.state = "restarting"

    def _restart_argus_daemon(self, attempt: int, reason: str) -> None:
        code = restart_nvargus_daemon()
        if code != 0:
            detail = f"nvargus-daemon restart exit={code} after {reason}"
            logger.error(detail)
            self._finish_argus_restart(ok=False, detail=detail)
            return
        ready = wait_for_argus(
            timeout_s=60,
            grace_s=0,
            log=lambda message: logger.info("%s", message),
        )
        if ready != 0:
            detail = f"camera provider still unavailable after daemon restart ({reason})"
            logger.error(detail)
            self._finish_argus_restart(ok=False, detail=detail)
            return
        logger.info("argus daemon restart %s finished, requeueing cameras", attempt)
        self._finish_argus_restart(ok=True, detail=reason)

    def _finish_argus_restart(self, *, ok: bool, detail: str) -> None:
        def _resume() -> bool:
            with self._argus_restart_lock:
                self._argus_restarting = False
            if not ok:
                if self._on_fatal is not None:
                    self._on_fatal(detail)
                return False
            slots = [slot for slot in self._slots.values() if slot.publishing]
            for slot in slots:
                slot.restart_attempts = 0
                slot.last_error = None
                slot.state = "idle"
            logger.info("requeueing %s camera(s) after argus daemon restart", len(slots))
            self._enqueue_starts(slots)
            return False

        if self._glib is None:
            _resume()
            return
        self._glib.idle_add(_resume)

    def _schedule_restart(self, slot: CameraSlot, *, reason: str) -> None:
        if not slot.publishing:
            return
        if self._argus_restarting or is_argus_provider_failure(reason):
            slot.last_error = reason
            slot.state = "error"
            self.note_argus_provider_failure(reason)
            return
        slot.last_error = reason
        if slot._restart_source_id is not None:
            slot.state = "restarting"
            logger.warning("%s restart already scheduled; latest error: %s", slot.config.id, reason)
            return
        slot.restart_attempts += 1
        delay_ms = next_backoff_ms(
            slot.restart_attempts,
            base_ms=self._cfg.pipeline.restart_backoff_ms,
            max_ms=self._cfg.pipeline.restart_max_backoff_ms,
        )
        logger.warning(
            "%s restart in %.1fs (attempt %s): %s",
            slot.config.id, delay_ms / 1000.0, slot.restart_attempts, reason,
        )
        slot.state = "restarting"
        if self._glib is None:
            self._restart_slot(slot)
            return
        slot._restart_source_id = self._glib.timeout_add(
            delay_ms, self._restart_timeout, slot
        )

    def _restart_timeout(self, slot: CameraSlot) -> None:
        slot._restart_source_id = None
        self._restart_slot(slot)

    def _restart_slot(self, slot: CameraSlot) -> None:
        cam_id = slot.config.id
        self._stop_capture_for_restart(slot)
        if not slot.publishing:
            slot.state = "idle"
            return
        self._enqueue_starts([slot])
        logger.info("%s restart queued", cam_id)
