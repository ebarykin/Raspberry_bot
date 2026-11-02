"""Unit-тесты CameraService без GStreamer (мок слотов)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Event, Thread
from unittest.mock import MagicMock

import pytest

from capture_pipeline.camera_service import CameraService, CameraSlot
from capture_pipeline.camera_slot import (
    PIPELINE_NULL_TIMEOUT_NS,
    abr_keep_frame,
    attach_clock_overlay,
    attach_encoded_bridge,
    attach_parktronic_overlay,
    attach_undistort_probe,
    create_rtsp_slot,
    format_overlay_clock,
)
from capture_pipeline.pipelines.overlays.parktronic import draw_parktronic_guides
from capture_pipeline.config import (
    ParktronicOverlayConfig,
    UndistortConfig,
    load_config,
)
from capture_pipeline.pipeline_recovery import PLAYING_TIMEOUT_MS, STABLE_PLAYING_MS

ROOT = Path(__file__).resolve().parents[1]


def _last_timeout(glib: MagicMock, interval_ms: int) -> tuple[object, object]:
    matches = [c for c in glib.timeout_add.call_args_list if c.args[0] == interval_ms]
    assert matches, f"missing timeout_add({interval_ms})"
    args = matches[-1].args
    return args[1], args[2]


def _service_with_slot() -> tuple[CameraService, CameraSlot, MagicMock]:
    cfg = load_config(ROOT / "configs" / "laptop-emulate.yaml")
    svc = CameraService(cfg, Gst=object(), GstRtspServer=object())
    slot = CameraSlot(
        config=cfg.cameras[0],
        server=object(),
        factory=MagicMock(),
        mounts=MagicMock(),
        bitrate_bps=1_200_000,
        peak_bitrate_bps=1_440_000,
        fps=30,
        publishing=True,
    )

    def _apply(
        target: CameraSlot,
        *,
        bitrate_bps: int,
        fps: int,
        profile_label: str,
    ) -> None:
        target.bitrate_bps = bitrate_bps
        target.peak_bitrate_bps = max(bitrate_bps, int(bitrate_bps * 1.2))
        target.fps = fps

    apply = MagicMock(side_effect=_apply)
    svc._apply_to_slot = apply  # type: ignore[method-assign]
    svc._slots[cfg.cameras[0].id] = slot
    return svc, slot, apply


def test_set_profile_idempotent_skips_apply() -> None:
    svc, _slot, apply = _service_with_slot()
    assert svc.profile == "good"
    status = svc.set_profile("good")
    assert status["profile"] == "good"
    apply.assert_not_called()


def test_set_profile_applies_on_change() -> None:
    svc, slot, apply = _service_with_slot()
    status = svc.set_profile("critical")
    assert status["profile"] == "critical"
    assert status["fps"] == 12
    assert status["bitrate_kbps"] == 250
    apply.assert_called_once()
    kwargs = apply.call_args.kwargs
    assert kwargs["bitrate_bps"] == 250_000
    assert kwargs["fps"] == 12
    assert kwargs["profile_label"] == "critical"
    assert apply.call_args.args[0] is slot


def test_set_profile_respects_min_dwell() -> None:
    svc, _slot, apply = _service_with_slot()
    svc.set_profile("critical")
    apply.reset_mock()
    status = svc.set_profile("good")
    assert status["profile"] == "critical"
    apply.assert_not_called()
    svc._profile_changed_at = 0.0
    status = svc.set_profile("good")
    assert status["profile"] == "good"
    apply.assert_called_once()


def test_set_profile_force_bypasses_min_dwell() -> None:
    svc, _slot, apply = _service_with_slot()
    svc.set_profile("critical")
    apply.reset_mock()
    status = svc.set_profile("good", force=True)
    assert status["profile"] == "good"
    assert status["abr_applied"] is True
    apply.assert_called_once()


def test_set_profile_failure_does_not_commit_global_profile() -> None:
    svc, _slot, apply = _service_with_slot()
    apply.side_effect = RuntimeError("encoder rejected bitrate")

    with pytest.raises(RuntimeError, match="not applied to every camera"):
        svc.set_profile("critical")

    assert svc.profile == "good"
    assert svc.status()["bitrate_kbps"] == 1100


def test_set_profile_rolls_back_slots_updated_before_failure() -> None:
    svc, first, _apply = _service_with_slot()
    cfg = svc._cfg
    second = CameraSlot(
        config=cfg.cameras[1],
        server=object(),
        factory=MagicMock(),
        mounts=MagicMock(),
        bitrate_bps=1_100_000,
        peak_bitrate_bps=1_320_000,
        fps=30,
        publishing=True,
        capture_pipeline=object(),
    )
    svc._slots[second.config.id] = second
    svc._capture_failed = MagicMock()  # type: ignore[method-assign]

    def apply(
        slot: CameraSlot,
        *,
        bitrate_bps: int,
        fps: int,
        profile_label: str,
    ) -> None:
        if slot is second and profile_label == "critical":
            raise RuntimeError("second encoder rejected bitrate")
        slot.bitrate_bps = bitrate_bps
        slot.peak_bitrate_bps = max(bitrate_bps, int(bitrate_bps * 1.2))
        slot.fps = fps

    svc._apply_to_slot = MagicMock(side_effect=apply)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="second encoder rejected bitrate"):
        svc.set_profile("critical")

    assert svc.profile == "good"
    assert (first.bitrate_bps, first.peak_bitrate_bps, first.fps) == (
        1_200_000,
        1_440_000,
        30,
    )
    assert (second.bitrate_bps, second.peak_bitrate_bps, second.fps) == (
        1_100_000,
        1_320_000,
        30,
    )
    svc._capture_failed.assert_called_once()


def test_set_profile_from_level_idempotent() -> None:
    svc, _slot, apply = _service_with_slot()
    svc.set_profile("degraded")
    apply.reset_mock()
    status = svc.set_profile_from_level("degraded")
    assert status["profile"] == "degraded"
    apply.assert_not_called()


def test_set_profile_from_level_rejects_unknown() -> None:
    svc, _slot, apply = _service_with_slot()
    with pytest.raises(ValueError, match="unknown profile"):
        svc.set_profile_from_level("warn")
    apply.assert_not_called()


def test_abr_keep_frame_matches_target_fps() -> None:
    for capture, target in ((30, 30), (30, 24), (30, 18), (30, 12)):
        acc = 0
        kept = 0
        for _ in range(capture):
            keep, acc = abr_keep_frame(acc, capture_fps=capture, target_fps=target)
            kept += int(keep)
        assert kept == target


def test_apply_to_slot_sets_jetson_vbv_and_qp() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    svc = CameraService(cfg, Gst=object(), GstRtspServer=object())
    enc = MagicMock()
    rate = MagicMock()
    pipeline = MagicMock()
    pipeline.get_by_name.side_effect = lambda name: {"enc0": enc, "rate0": rate}.get(name)
    factory = MagicMock()
    slot = CameraSlot(
        config=cfg.cameras[0],
        server=object(),
        factory=factory,
        mounts=MagicMock(),
        bitrate_bps=1_200_000,
        peak_bitrate_bps=1_440_000,
        fps=30,
        publishing=True,
        capture_pipeline=pipeline,
    )
    svc._force_keyframe = MagicMock()  # type: ignore[method-assign]
    svc._apply_to_slot(slot, bitrate_bps=250_000, fps=12, profile_label="critical")
    enc.set_property.assert_any_call("bitrate", 250_000)
    enc.set_property.assert_any_call("vbv-size", 50_000)
    enc.set_property.assert_any_call("iframeinterval", 4)
    enc.set_property.assert_any_call("idrinterval", 4)
    enc.set_property.assert_any_call("qp-range", "0,51:0,51:0,51")
    rate.set_property.assert_not_called()
    assert slot._capture_fps == 30
    assert slot._abr_target_fps == 12
    factory.set_launch.assert_not_called()


def test_apply_to_slot_failure_keeps_previous_reported_values() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    svc = CameraService(cfg, Gst=object(), GstRtspServer=object())
    enc = MagicMock()
    enc.set_property.side_effect = RuntimeError("unsupported")
    rate = MagicMock()
    pipeline = MagicMock()
    pipeline.get_by_name.side_effect = lambda name: {
        "enc0": enc,
        "rate0": rate,
    }.get(name)
    slot = CameraSlot(
        config=cfg.cameras[0],
        server=object(),
        factory=MagicMock(),
        mounts=MagicMock(),
        bitrate_bps=1_200_000,
        peak_bitrate_bps=1_440_000,
        fps=30,
        publishing=True,
        capture_pipeline=pipeline,
    )

    with pytest.raises(RuntimeError, match="failed to apply ABR"):
        svc._apply_to_slot(
            slot, bitrate_bps=300_000, fps=12, profile_label="critical"
        )

    assert slot.bitrate_bps == 1_200_000
    assert slot.peak_bitrate_bps == 1_440_000
    assert slot.fps == 30
    assert slot._abr_target_fps is None


def test_status_does_not_require_gst() -> None:
    svc, slot, _apply = _service_with_slot()
    status = svc.status()
    assert status["source"] == "emulate"
    assert status["profile"] == "good"
    assert status["cameras"][0]["id"] == slot.config.id


def test_shutdown_detaches_publishing_slots() -> None:
    svc, slot, _apply = _service_with_slot()
    detach = MagicMock()
    stop_runtime = MagicMock()
    svc._detach_factory = detach  # type: ignore[method-assign]
    svc._stop_slot_runtime = stop_runtime  # type: ignore[method-assign]
    svc.shutdown()
    detach.assert_called_once_with(slot)
    stop_runtime.assert_not_called()
    assert slot.publishing is False
    assert slot.state == "idle"


def test_shutdown_stops_disabled_slots_without_detach() -> None:
    svc, slot, _apply = _service_with_slot()
    slot.publishing = False
    detach = MagicMock()
    stop_runtime = MagicMock()
    svc._detach_factory = detach  # type: ignore[method-assign]
    svc._stop_slot_runtime = stop_runtime  # type: ignore[method-assign]
    svc.shutdown()
    detach.assert_not_called()
    stop_runtime.assert_called_once_with(slot)


def test_stop_slot_runtime_cancels_restart() -> None:
    svc, slot, _apply = _service_with_slot()
    glib = MagicMock()
    svc._glib = glib
    svc._stop_shared_medias = MagicMock()  # type: ignore[method-assign]
    svc._stop_capture = MagicMock()  # type: ignore[method-assign]
    slot._restart_source_id = 7
    slot._playing_timeout_id = 8
    slot.factory_attached = True
    svc._stop_slot_runtime(slot)
    glib.source_remove.assert_any_call(7)
    glib.source_remove.assert_any_call(8)
    assert slot._restart_source_id is None
    assert slot._playing_timeout_id is None
    slot.mounts.remove_factory.assert_called_once_with(slot.config.mount)


def _start_configured_slots(svc: CameraService) -> list[CameraSlot]:
    created: list[CameraSlot] = []

    def _create(cam) -> CameraSlot:
        slot = CameraSlot(
            config=cam,
            server=object(),
            factory=MagicMock(),
            mounts=MagicMock(),
            bitrate_bps=1_200_000,
            peak_bitrate_bps=1_440_000,
            fps=30,
        )
        created.append(slot)
        return slot

    def _start(slot: CameraSlot) -> None:
        pipeline = object()
        slot.capture_pipeline = pipeline
        slot.state = "starting"
        svc._finish_capture_start(slot)

    svc._create_slot = _create  # type: ignore[method-assign]
    svc._start_slot = MagicMock(side_effect=_start)  # type: ignore[method-assign]
    return created


def test_start_configured_starts_capture_for_jetson() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    svc = CameraService(cfg, Gst=object(), GstRtspServer=object())
    created = _start_configured_slots(svc)
    svc.start_configured()
    assert svc._start_slot.call_count == len(created)
    assert all(slot.publishing for slot in created)


def test_start_configured_starts_capture_for_emulate() -> None:
    cfg = load_config(ROOT / "configs" / "laptop-emulate.yaml")
    svc = CameraService(cfg, Gst=object(), GstRtspServer=object())
    created = _start_configured_slots(svc)
    svc.start_configured()
    assert svc._start_slot.call_count == len(created)
    assert [call.args[0] for call in svc._start_slot.call_args_list] == created


def test_start_configured_skips_disabled_cameras() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    cfg = replace(
        cfg,
        cameras=tuple(
            cam if cam.id == "cam0" else replace(cam, enabled=False)
            for cam in cfg.cameras
        ),
    )
    svc = CameraService(cfg, Gst=object(), GstRtspServer=object())
    created = _start_configured_slots(svc)
    svc.start_configured()
    assert [slot.config.id for slot in created] == ["cam0"]
    assert list(svc.slots) == ["cam0"]
    svc._start_slot.assert_called_once()


def test_start_configured_resolves_serial_to_sensor_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    cfg = replace(
        cfg,
        cameras=tuple(
            replace(cam, serial=999) if cam.id == "cam0" else cam
            for cam in cfg.cameras
        ),
    )
    monkeypatch.setattr(
        "capture_pipeline.camera_service.discover_serial_to_sensor_id",
        lambda: {999: 7},
    )
    svc = CameraService(cfg, Gst=object(), GstRtspServer=object())
    created = _start_configured_slots(svc)
    svc.start_configured()
    cam0_slot = next(slot for slot in created if slot.config.id == "cam0")
    assert cam0_slot.config.sensor_id == 7
    assert svc._start_slot.call_count == len(created)
    assert svc.slots["cam0"].state != "error"


def test_start_configured_marks_unresolved_serial_as_error_and_starts_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    cfg = replace(
        cfg,
        cameras=tuple(
            replace(cam, serial=999) if cam.id == "cam0" else cam
            for cam in cfg.cameras
        ),
    )
    monkeypatch.setattr(
        "capture_pipeline.camera_service.discover_serial_to_sensor_id",
        lambda: {},
    )
    svc = CameraService(cfg, Gst=object(), GstRtspServer=object())
    created = _start_configured_slots(svc)
    svc.start_configured()
    errored = svc.slots["cam0"]
    assert errored.state == "error"
    assert errored.last_error == "serial 999 not found at startup"
    other_ids = [cam.id for cam in cfg.cameras if cam.id != "cam0" and cam.enabled]
    assert svc._start_slot.call_count == len(other_ids)
    started_ids = [call.args[0].config.id for call in svc._start_slot.call_args_list]
    assert "cam0" not in started_ids
    assert all(slot.state != "error" for slot in created if slot.config.id != "cam0")


def test_start_configured_raises_when_every_camera_fails_serial_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    cfg = replace(
        cfg,
        cameras=tuple(replace(cam, serial=1000 + i) for i, cam in enumerate(cfg.cameras)),
    )
    monkeypatch.setattr(
        "capture_pipeline.camera_service.discover_serial_to_sensor_id",
        lambda: {},
    )
    svc = CameraService(cfg, Gst=object(), GstRtspServer=object())
    _start_configured_slots(svc)
    with pytest.raises(RuntimeError, match="no camera could be started"):
        svc.start_configured()
    assert svc._start_slot.call_count == 0
    assert all(slot.state == "error" for slot in svc.slots.values())


def test_stagger_holds_second_slot_until_timeout() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    svc = CameraService(cfg, Gst=object(), GstRtspServer=object(), glib=MagicMock())
    created = _start_configured_slots(svc)
    svc.start_configured()
    assert svc._start_slot.call_count == 1
    assert svc._start_slot.call_args.args[0] is created[0]
    svc._glib.timeout_add.assert_called()
    svc._stagger_timeout()
    assert svc._start_slot.call_count == 2


def test_capture_mounts_only_after_first_encoded_sample() -> None:
    cfg = load_config(ROOT / "configs" / "laptop-emulate.yaml")
    cfg = replace(
        cfg,
        cameras=(replace(cfg.cameras[0], nav_enabled=True, ros_name="front_camera"),),
        nav=replace(cfg.nav, sink_type="fakesink"),
    )
    pipeline = MagicMock()
    bus = MagicMock()
    pipeline.get_bus.return_value = bus
    gst = MagicMock()
    gst.State.PLAYING = "PLAYING"
    gst.State.NULL = "NULL"
    gst.StateChangeReturn.ASYNC = "ASYNC"
    gst.StateChangeReturn.NO_PREROLL = "NO_PREROLL"
    gst.StateChangeReturn.FAILURE = "FAILURE"
    gst.FlowReturn.OK = "OK"
    gst.BufferFlags.DELTA_UNIT = "DELTA"
    gst.MessageType.ERROR = "ERROR"
    gst.MessageType.EOS = "EOS"
    gst.MessageType.STATE_CHANGED = "STATE_CHANGED"
    gst.parse_launch.return_value = pipeline
    pipeline.set_state.return_value = "NO_PREROLL"
    glib = MagicMock()
    svc = CameraService(cfg, Gst=gst, GstRtspServer=object(), glib=glib)
    slot = CameraSlot(
        config=cfg.cameras[0],
        server=object(),
        factory=MagicMock(),
        mounts=MagicMock(),
        bitrate_bps=1_200_000,
        peak_bitrate_bps=1_440_000,
        fps=30,
        publishing=True,
    )
    svc._create_slot = MagicMock(return_value=slot)  # type: ignore[method-assign]
    svc.start_configured()
    assert "fakesink name=nav_sink" in gst.parse_launch.call_args.args[0]
    slot.mounts.add_factory.assert_not_called()
    assert slot.state == "starting"
    assert svc._start_in_flight is slot
    _last_timeout(glib, PLAYING_TIMEOUT_MS)

    callback = bus.connect.call_args.args[1]
    message = MagicMock(type="STATE_CHANGED", src=pipeline)
    message.parse_state_changed.return_value = (None, "PLAYING", None)
    callback(bus, message)
    slot.mounts.add_factory.assert_not_called()
    assert slot.state == "starting"

    encoded_sample = MagicMock()
    encoded_buffer = MagicMock()
    encoded_sample.get_buffer.return_value = encoded_buffer
    pipeline.get_by_name.return_value.emit.return_value = encoded_sample
    assert slot._encoded_sample_cb is not None
    assert slot._encoded_sample_cb(pipeline.get_by_name.return_value) == "OK"

    idle_call = glib.idle_add.call_args
    idle_call.args[0](*idle_call.args[1:])
    slot.mounts.add_factory.assert_called_once_with(slot.config.mount, slot.factory)
    assert slot.state == "playing"
    assert svc._start_in_flight is None
    appsrc = MagicMock()
    slot.encoded_sources.append(appsrc)
    slot._encoded_ready_ids.add(id(appsrc))
    svc._stop_capture(slot)
    assert slot.encoded_sources == [appsrc]
    assert slot._encoded_ready_ids == set()
    bus.disconnect.assert_called_once_with(bus.connect.return_value)
    bus.remove_signal_watch.assert_called_once()


def test_playing_timeout_releases_start_queue() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    cfg = replace(
        cfg,
        cameras=cfg.cameras[:2],
        capture=replace(cfg.capture, stagger_ms=0),
    )
    pipeline0 = MagicMock()
    pipeline1 = MagicMock()
    pipeline0.get_bus.return_value = MagicMock()
    pipeline1.get_bus.return_value = MagicMock()
    pipeline0.set_state.return_value = "ASYNC"
    pipeline1.set_state.return_value = "ASYNC"
    gst = MagicMock()
    gst.State.PLAYING = "PLAYING"
    gst.State.NULL = "NULL"
    gst.StateChangeReturn.ASYNC = "ASYNC"
    gst.StateChangeReturn.FAILURE = "FAILURE"
    gst.MessageType.ERROR = "ERROR"
    gst.MessageType.EOS = "EOS"
    gst.MessageType.STATE_CHANGED = "STATE_CHANGED"
    gst.parse_launch.side_effect = [pipeline0, pipeline1]
    glib = MagicMock()
    svc = CameraService(cfg, Gst=gst, GstRtspServer=object(), glib=glib)
    created: list[CameraSlot] = []

    def _create(cam) -> CameraSlot:
        slot = CameraSlot(
            config=cam,
            server=object(),
            factory=MagicMock(),
            mounts=MagicMock(),
            bitrate_bps=1_200_000,
            peak_bitrate_bps=1_440_000,
            fps=30,
        )
        created.append(slot)
        return slot

    svc._create_slot = _create  # type: ignore[method-assign]
    svc.start_configured()
    assert gst.parse_launch.call_count == 1
    assert created[0].state == "starting"
    timeout_cb, hung = _last_timeout(glib, PLAYING_TIMEOUT_MS)
    assert hung is created[0]
    timeout_cb(hung)
    assert gst.parse_launch.call_count == 2
    assert created[1].state == "starting"
    assert created[0].state == "restarting"
    created[0].mounts.add_factory.assert_not_called()


def test_failed_start_sets_partial_pipeline_to_null() -> None:
    cfg = load_config(ROOT / "configs" / "laptop-emulate.yaml")
    pipeline = MagicMock()
    gst = MagicMock()
    gst.State.PLAYING = "PLAYING"
    gst.State.NULL = "NULL"
    gst.StateChangeReturn.FAILURE = "FAILURE"
    gst.parse_launch.return_value = pipeline
    pipeline.set_state.side_effect = ["FAILURE", None]
    pipeline.get_state.side_effect = [
        ("SUCCESS", "PLAYING", "NULL"),
        ("SUCCESS", "NULL", "VOID_PENDING"),
    ]
    gst.MessageType.ERROR = "ERROR"
    gst.MessageType.EOS = "EOS"
    gst.MessageType.STATE_CHANGED = "STATE_CHANGED"
    bus = MagicMock()
    pipeline.get_bus.return_value = bus
    slot = CameraSlot(
        config=cfg.cameras[0],
        server=object(),
        factory=MagicMock(),
        mounts=MagicMock(),
        bitrate_bps=1_200_000,
        peak_bitrate_bps=1_440_000,
        fps=30,
        restart_attempts=3,
    )
    with pytest.raises(RuntimeError, match="PLAYING"):
        slot.start_capture(
            cfg,
            gst,
            "good",
            before_play=lambda: slot.attach_bus_watch(
                gst, on_failure=lambda _p, _r: None
            ),
        )
    assert pipeline.set_state.call_args_list[1].args == ("NULL",)
    assert pipeline.get_state.call_args_list[0].args == (0,)
    assert pipeline.get_state.call_args_list[1].args == (
        PIPELINE_NULL_TIMEOUT_NS,
    )
    assert slot.capture_pipeline is None
    assert slot.restart_attempts == 3
    bus.disconnect.assert_called_once()
    bus.remove_signal_watch.assert_called_once()


def test_stop_capture_waits_until_pipeline_reaches_null() -> None:
    cfg = load_config(ROOT / "configs" / "laptop-emulate.yaml")
    pipeline = MagicMock()
    pipeline.get_state.side_effect = [
        ("SUCCESS", "PLAYING", "VOID_PENDING"),
        ("SUCCESS", "NULL", "VOID_PENDING"),
    ]
    gst = MagicMock()
    gst.State.NULL = "NULL"
    slot = CameraSlot(
        config=cfg.cameras[0],
        server=object(),
        factory=MagicMock(),
        mounts=MagicMock(),
        bitrate_bps=1_200_000,
        peak_bitrate_bps=1_440_000,
        fps=30,
        capture_pipeline=pipeline,
        state="playing",
        capture_generation=4,
    )

    slot.stop_capture(gst)

    pipeline.set_state.assert_called_once_with("NULL")
    assert [entry.args for entry in pipeline.get_state.call_args_list] == [
        (0,),
        (PIPELINE_NULL_TIMEOUT_NS,),
    ]
    assert slot.capture_pipeline is None
    assert slot.state == "stopping"


def test_async_start_keeps_backoff_until_stably_playing() -> None:
    cfg = load_config(ROOT / "configs" / "laptop-emulate.yaml")
    pipeline = MagicMock()
    gst = MagicMock()
    gst.State.PLAYING = "PLAYING"
    gst.StateChangeReturn.FAILURE = "FAILURE"
    gst.parse_launch.return_value = pipeline
    pipeline.set_state.return_value = "ASYNC"
    slot = CameraSlot(
        config=cfg.cameras[0],
        server=object(),
        factory=MagicMock(),
        mounts=MagicMock(),
        bitrate_bps=1_200_000,
        peak_bitrate_bps=1_440_000,
        fps=30,
        restart_attempts=3,
    )
    slot.start_capture(cfg, gst, "good")
    assert slot.restart_attempts == 3
    slot.mark_playing()
    assert slot.restart_attempts == 3


def test_backoff_resets_only_after_stable_playing_window() -> None:
    svc, slot, _apply = _service_with_slot()
    glib = MagicMock()
    glib.timeout_add.return_value = 91
    svc._glib = glib
    pipeline = object()
    slot.capture_pipeline = pipeline
    slot.state = "starting"
    slot.restart_attempts = 3

    svc._finish_capture_start(slot)

    assert slot.restart_attempts == 3
    stable_call = next(
        call
        for call in glib.timeout_add.call_args_list
        if call.args[0] == STABLE_PLAYING_MS
    )
    callback, callback_slot, callback_pipeline = stable_call.args[1:]
    callback(callback_slot, callback_pipeline)
    assert slot.restart_attempts == 0


def test_overlay_clock_uses_python_microseconds() -> None:
    stamp = format_overlay_clock(
        "%H:%M:%S.%f", now=datetime(2026, 9, 9, 13, 53, 28, 123456)
    )
    assert stamp == "13:53:28.123456"


def test_attach_clock_overlay_stamps_textoverlay() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-duo-one-s.yaml")
    cfg = replace(cfg, overlay=replace(cfg.overlay, clock=True))
    gst = MagicMock()
    gst.PadProbeType.BUFFER = "BUFFER"
    gst.PadProbeReturn.OK = "OK"
    pipeline = MagicMock()
    overlay = MagicMock()
    sink = MagicMock()
    pipeline.get_by_name.return_value = overlay
    overlay.get_static_pad.side_effect = lambda name: sink if name == "video_sink" else None
    cb = attach_clock_overlay(pipeline, cfg, gst)
    assert cb is not None
    pipeline.get_by_name.assert_called_once_with("clock0")
    sink.add_probe.assert_called_once()
    probe_cb = sink.add_probe.call_args.args[1]
    probe_cb(None, None)
    overlay.set_property.assert_called()
    assert overlay.set_property.call_args.args[0] == "text"
    text = overlay.set_property.call_args.args[1]
    datetime.strptime(text, "%H:%M:%S.%f")


def test_attach_parktronic_overlay_connects_draw() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-duo-one-s.yaml")
    pipeline = MagicMock()
    overlay = MagicMock()
    pipeline.get_by_name.return_value = overlay
    cb = attach_parktronic_overlay(pipeline, cfg, cfg.cameras[0])
    assert cb is not None
    pipeline.get_by_name.assert_called_once_with("park0")
    overlay.connect.assert_called_once()
    assert overlay.connect.call_args.args[0] == "draw"


def test_attach_undistort_probe_connects_src() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-duo-one-s.yaml")
    camera = replace(
        cfg.cameras[0],
        undistort=True,
        undistort_params=UndistortConfig(),
    )
    pipeline = MagicMock()
    elem = MagicMock()
    src = MagicMock()
    pipeline.get_by_name.return_value = elem
    elem.get_static_pad.return_value = src
    gst = MagicMock()
    gst.PadProbeType.BUFFER = "BUFFER"
    cb = attach_undistort_probe(pipeline, cfg, camera, gst)
    assert cb is not None
    pipeline.get_by_name.assert_called_once_with("undist0")
    elem.get_static_pad.assert_called_once_with("src")
    src.add_probe.assert_called_once()


def test_attach_undistort_probe_skipped_when_disabled() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    pipeline = MagicMock()
    gst = MagicMock()
    cb = attach_undistort_probe(pipeline, cfg, cfg.cameras[1], gst)
    assert cb is None
    pipeline.get_by_name.assert_not_called()


def test_attach_parktronic_overlay_skipped_when_disabled() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    pipeline = MagicMock()
    cb = attach_parktronic_overlay(pipeline, cfg, cfg.cameras[1])
    assert cb is None
    pipeline.get_by_name.assert_not_called()


def test_draw_parktronic_guides_labels_distances() -> None:
    cr = MagicMock()
    draw_parktronic_guides(cr, 640, 400)
    labels = [call.args[0] for call in cr.show_text.call_args_list]
    assert "1 m" in labels
    assert "2 m" in labels
    assert "0.5 m" not in labels
    assert cr.set_dash.called
    assert cr.stroke.call_count >= 5
    defaults = ParktronicOverlayConfig()
    draw_parktronic_guides(MagicMock(), 640, 400, replace(defaults, fill_style="fade"))
    draw_parktronic_guides(MagicMock(), 640, 400, replace(defaults, fill_style="edge"))
    draw_parktronic_guides(MagicMock(), 640, 400, replace(defaults, fill_style="band"))


def test_encoded_bridge_forwards_fresh_au_without_old_timestamps() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-duo-one-s.yaml")
    slot = CameraSlot(
        config=cfg.cameras[0],
        server=MagicMock(),
        factory=MagicMock(),
        mounts=MagicMock(),
        bitrate_bps=1_200_000,
        peak_bitrate_bps=1_440_000,
        fps=30,
    )
    pipeline = MagicMock()
    sink = MagicMock()
    pipeline.get_by_name.return_value = sink
    appsrc = MagicMock()
    appsrc.get_property.return_value = 0
    appsrc.emit.return_value = "OK"
    slot.encoded_sources.append(appsrc)
    sample = MagicMock()
    source_buffer = MagicMock()
    forwarded = MagicMock()
    source_buffer.copy.return_value = forwarded
    source_buffer.has_flags.return_value = False
    sample.get_buffer.return_value = source_buffer
    sink.emit.return_value = sample
    gst = MagicMock()
    gst.CLOCK_TIME_NONE = -1
    gst.BufferFlags.DELTA_UNIT = "DELTA"
    gst.FlowReturn.OK = "OK"
    ready = MagicMock()

    attach_encoded_bridge(slot, pipeline, gst, on_first_sample=ready)
    callback = sink.connect.call_args.args[1]
    assert callback(sink) == "OK"
    assert callback(sink) == "OK"
    ready.assert_called_once_with(pipeline)
    assert forwarded.pts == -1
    assert forwarded.dts == -1
    assert appsrc.emit.call_count == 2
    appsrc.emit.assert_called_with("push-buffer", forwarded)
    source_buffer.copy_deep.assert_not_called()


def test_rtsp_factory_disables_default_jitter() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-duo-one-s.yaml")
    gst_rtsp = MagicMock()
    factory = MagicMock()
    server = MagicMock()
    gst_rtsp.RTSPServer.new.return_value = server
    gst_rtsp.RTSPMediaFactory.new.return_value = factory
    gst_rtsp.RTSPSuspendMode.NONE = "NONE"
    create_rtsp_slot(
        cfg.cameras[0],
        cfg=cfg,
        gst_rtsp_server=gst_rtsp,
        bitrate_bps=1_200_000,
        fps=30,
    )
    factory.set_shared.assert_called_once_with(True)
    factory.set_latency.assert_called_once_with(1)
    launch = factory.set_launch.call_args.args[0]
    assert "appsrc name=encoded_src" in launch
    assert "rtph264pay name=pay0" in launch
    assert "h264parse" not in launch
    assert "shmsink" not in launch
    assert "shmsrc" not in launch


def test_appsrc_abr_updates_always_on_capture() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-duo-one-s.yaml")
    svc = CameraService(cfg, Gst=MagicMock(), GstRtspServer=MagicMock())
    pipeline = MagicMock()
    enc = MagicMock()
    rate = MagicMock()
    pipeline.get_by_name.side_effect = lambda name: {
        "enc0": enc,
        "rate0": rate,
    }.get(name)
    factory = MagicMock()
    appsrc = MagicMock()
    slot = CameraSlot(
        config=cfg.cameras[0],
        server=MagicMock(),
        factory=factory,
        mounts=MagicMock(),
        bitrate_bps=1_200_000,
        peak_bitrate_bps=1_440_000,
        fps=30,
        publishing=True,
        capture_pipeline=pipeline,
        encoded_sources=[appsrc],
    )
    svc._apply_to_slot(
        slot, bitrate_bps=250_000, fps=12, profile_label="critical"
    )
    enc.set_property.assert_any_call("bitrate", 250_000)
    enc.set_property.assert_any_call("vbv-size", 12_500)
    rate.set_property.assert_not_called()
    assert slot._capture_fps == 30
    assert slot._abr_target_fps == 12
    appsrc.set_property.assert_called_once_with("max-latency", 83_333_333)
    factory.set_launch.assert_not_called()


def test_attach_clock_overlay_skipped_when_disabled() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    pipeline = MagicMock()
    assert attach_clock_overlay(pipeline, cfg, MagicMock()) is None
    pipeline.get_by_name.assert_not_called()


def test_runtime_modules_have_no_anchor() -> None:
    root = ROOT / "capture_pipeline"
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "anchor" not in text.lower(), path


def test_restart_slot_requeues_start() -> None:
    cfg = load_config(ROOT / "configs" / "laptop-emulate.yaml")
    svc = CameraService(cfg, Gst=object(), GstRtspServer=object())
    slot = CameraSlot(
        config=cfg.cameras[0],
        server=object(),
        factory=MagicMock(),
        mounts=MagicMock(),
        bitrate_bps=1_200_000,
        peak_bitrate_bps=1_440_000,
        fps=30,
        publishing=True,
    )
    svc._slots[slot.config.id] = slot
    svc._stop_shared_medias = MagicMock()  # type: ignore[method-assign]
    svc._stop_capture = MagicMock()  # type: ignore[method-assign]
    start = MagicMock()
    svc._start_slot = start  # type: ignore[method-assign]
    svc._restart_slot(slot)
    start.assert_called_once_with(slot)


def test_restarts_share_the_global_start_queue() -> None:
    cfg = load_config(ROOT / "configs" / "laptop-emulate.yaml")
    svc = CameraService(cfg, Gst=object(), GstRtspServer=object())
    first = CameraSlot(
        config=cfg.cameras[0],
        server=object(),
        factory=MagicMock(),
        mounts=MagicMock(),
        bitrate_bps=1_200_000,
        peak_bitrate_bps=1_440_000,
        fps=30,
        publishing=True,
    )
    second = CameraSlot(
        config=cfg.cameras[1],
        server=object(),
        factory=MagicMock(),
        mounts=MagicMock(),
        bitrate_bps=1_200_000,
        peak_bitrate_bps=1_440_000,
        fps=30,
        publishing=True,
    )
    svc._start_in_flight = first
    svc._stop_capture_for_restart = MagicMock()  # type: ignore[method-assign]
    svc._start_slot = MagicMock()  # type: ignore[method-assign]

    svc._restart_slot(second)

    svc._start_slot.assert_not_called()
    assert svc._start_queue == [second]


def test_capture_failure_keeps_existing_rtsp_mount_and_media() -> None:
    svc, slot, _apply = _service_with_slot()
    pipeline = object()
    slot.capture_pipeline = pipeline
    slot.state = "playing"
    slot.factory_attached = True
    media = MagicMock()
    appsrc = MagicMock()
    slot.medias.append(media)
    slot.encoded_sources.append(appsrc)
    svc._stop_capture = MagicMock()  # type: ignore[method-assign]
    svc._schedule_restart = MagicMock()  # type: ignore[method-assign]

    svc._capture_failed(slot, pipeline, "temporary Argus failure")

    svc._stop_capture.assert_called_once_with(slot)
    slot.mounts.remove_factory.assert_not_called()
    media.unprepare.assert_not_called()
    assert slot.encoded_sources == [appsrc]
    svc._schedule_restart.assert_called_once_with(
        slot, reason="temporary Argus failure"
    )


def test_encoded_bridge_serializes_push_and_source_removal() -> None:
    cfg = load_config(ROOT / "configs" / "jetson-duo-one-s.yaml")
    gst = MagicMock()
    gst.CLOCK_TIME_NONE = -1
    gst.BufferFlags.DELTA_UNIT = "DELTA"
    gst.FlowReturn.OK = "OK"
    svc = CameraService(cfg, Gst=gst, GstRtspServer=MagicMock())
    slot = CameraSlot(
        config=cfg.cameras[0],
        server=MagicMock(),
        factory=MagicMock(),
        mounts=MagicMock(),
        bitrate_bps=900_000,
        peak_bitrate_bps=1_080_000,
        fps=30,
    )
    media = MagicMock()
    slot.medias.append(media)
    appsrc = MagicMock()
    appsrc.get_property.return_value = 0
    slot.encoded_sources.append(appsrc)
    push_entered = Event()
    release_push = Event()
    removal_done = Event()

    def _push(_name, _buffer):
        push_entered.set()
        assert release_push.wait(1)
        return "OK"

    appsrc.emit.side_effect = _push
    source_buffer = MagicMock()
    source_buffer.has_flags.return_value = False
    sample = MagicMock()
    sample.get_buffer.return_value = source_buffer
    sink = MagicMock()
    sink.emit.return_value = sample
    pipeline = MagicMock()
    pipeline.get_by_name.return_value = sink
    attach_encoded_bridge(slot, pipeline, gst)
    callback = sink.connect.call_args.args[1]

    push_thread = Thread(target=callback, args=(sink,))
    push_thread.start()
    assert push_entered.wait(1)

    def _remove() -> None:
        svc._remove_media(slot, media, appsrc)
        removal_done.set()

    remove_thread = Thread(target=_remove)
    remove_thread.start()
    assert not removal_done.wait(0.05)
    release_push.set()
    push_thread.join(1)
    remove_thread.join(1)
    assert removal_done.is_set()
    assert appsrc not in slot.encoded_sources


def test_finish_capture_start_is_idempotent() -> None:
    svc, slot, _apply = _service_with_slot()
    slot.state = "starting"
    slot.capture_pipeline = object()
    svc._finish_capture_start(slot)
    svc._finish_capture_start(slot)
    slot.mounts.add_factory.assert_called_once_with(slot.config.mount, slot.factory)


def test_restart_coalesces_latest_failure() -> None:
    svc, slot, _apply = _service_with_slot()
    glib = MagicMock()
    glib.timeout_add.return_value = 77
    svc._glib = glib
    svc._schedule_restart(slot, reason="first")
    svc._schedule_restart(slot, reason="latest")
    assert slot.restart_attempts == 1
    assert slot.last_error == "latest"
    assert slot._restart_source_id == 77
    assert glib.timeout_add.call_count == 1


def test_rtsp_media_error_removes_source_and_unprepares() -> None:
    cfg = load_config(ROOT / "configs" / "laptop-emulate.yaml")
    gst = MagicMock()
    gst.MessageType.ERROR = "ERROR"
    gst.MessageType.EOS = "EOS"
    gst.State.NULL = "NULL"
    svc = CameraService(cfg, Gst=gst, GstRtspServer=MagicMock())
    slot = CameraSlot(
        config=cfg.cameras[0],
        server=MagicMock(),
        factory=MagicMock(),
        mounts=MagicMock(),
        bitrate_bps=900_000,
        peak_bitrate_bps=1_080_000,
        fps=30,
    )
    svc._force_keyframe = MagicMock()  # type: ignore[method-assign]
    media = MagicMock()
    element = MagicMock()
    appsrc = MagicMock()
    bus = MagicMock()
    media.get_element.return_value = element
    element.get_by_name.return_value = appsrc
    element.get_bus.return_value = bus
    media.unprepare.return_value = True

    svc._register_media(slot, media)
    assert appsrc in slot.encoded_sources
    callback = bus.connect.call_args.args[1]
    error = MagicMock(message="payloader failed")
    message = MagicMock(type="ERROR")
    message.parse_error.return_value = (error, None)
    callback(bus, message)

    assert media not in slot.medias
    assert appsrc not in slot.encoded_sources
    media.unprepare.assert_called_once()
    bus.remove_signal_watch.assert_called_once()
