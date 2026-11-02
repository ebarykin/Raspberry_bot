"""Ожидание camera provider и реакция драйвера на отказ демона."""

from __future__ import annotations

import io
import logging
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from capture_pipeline.argus_ready import restart_nvargus_daemon, wait_for_argus
from capture_pipeline.argus_stderr import ArgusStderrWatch, stdout_line_is_noise
from capture_pipeline.camera_service import CameraService
from capture_pipeline.log_setup import _HandshakeClosedFilter, configure_logging
from capture_pipeline.camera_slot import CameraSlot
from capture_pipeline.config import load_config

ROOT = Path(__file__).resolve().parents[1]


def _clock():
    state = {"t": 0.0}

    def now() -> float:
        return state["t"]

    def sleep(seconds: float) -> None:
        state["t"] += seconds

    return now, sleep


def _socket_stat(_path: str) -> os.stat_result:
    return SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_ino=42, st_size=0)  # type: ignore[return-value]


def test_wait_retries_until_provider_answers() -> None:
    now, sleep = _clock()
    calls: list[int] = []

    def probe() -> tuple[bool, str]:
        calls.append(1)
        if len(calls) < 3:
            return False, "Cannot create camera provider"
        return True, "paused"

    code = wait_for_argus(
        timeout_s=30,
        grace_s=1,
        probe_interval_s=2,
        log=lambda _message: None,
        sleep=sleep,
        now=now,
        stat_socket=_socket_stat,
        probe=probe,
    )
    assert code == 0
    assert len(calls) == 3


def test_wait_times_out_when_provider_never_answers() -> None:
    now, sleep = _clock()
    code = wait_for_argus(
        timeout_s=5,
        grace_s=0,
        probe_interval_s=2,
        log=lambda _message: None,
        sleep=sleep,
        now=now,
        stat_socket=_socket_stat,
        probe=lambda: (False, "Cannot create camera provider"),
    )
    assert code == 1


def test_wait_times_out_when_socket_never_appears() -> None:
    now, sleep = _clock()

    def missing(_path: str) -> os.stat_result:
        raise FileNotFoundError(_path)

    code = wait_for_argus(
        timeout_s=3,
        grace_s=0,
        log=lambda _message: None,
        sleep=sleep,
        now=now,
        stat_socket=missing,
        probe=lambda: (True, "unused"),
    )
    assert code == 1


def test_daemon_answer_other_than_provider_is_ready() -> None:
    now, sleep = _clock()
    code = wait_for_argus(
        timeout_s=10,
        grace_s=0,
        log=lambda _message: None,
        sleep=sleep,
        now=now,
        stat_socket=_socket_stat,
        probe=lambda: (True, "daemon answered: No cameras available"),
    )
    assert code == 0
    assert now() == 0


def test_stdout_noise_drops_banners_and_keeps_argus_errors() -> None:
    noise = [
        "(gst-plugin-scanner:31): GStreamer-WARNING **: Failed to load plugin '/usr/lib/libcluttergst3.so'",
        "0:00:15.307 WARN default gstcuvidloader.c:89:gst_cuvid_load_library: Could not open library libnvcuvid.so.1",
        "0:00:15.544 WARN default gstsfelement.c:97:gst_sf_create_audio_template_caps: format is not mapped",
        "Opening in BLOCKING MODE",
        "NvMMLiteOpen : Block : BlockType = 4",
        "===== NvVideo: NVENC =====",
        "NvMMLiteBlockCreate : Block : BlockType = 4",
        "NvVideo: NvMMAttributeVideoEnc_QPRange Get Parameter",
        "H264: Profile = 66 Level = 0",
        "NVMEDIA: Need to set EMC bandwidth",
        "GST_ARGUS: Available Sensor modes :",
        "GST_ARGUS: 1920 x 1200 FR = 29.999999 fps",
        "GST_ARGUS: Setup Complete, Starting captures for 0 seconds",
        "CONSUMER: Producer has connected; continuing.",
        "   Camera index = 0",
        "   Frame Rate = 29.999999",
        '1791970117 [0] python3: selected interface "lo" is not multicast-capable: disabling multicast',
        "",
    ]
    keep = [
        "(Argus) Error Timeout: Cannot create camera provider",
        "Error generated. gstnvarguscamerasrc.cpp, execute:815 Failed to create CameraProvider",
        "Error generated. Failed to create CaptureSession",
        "Invalid camera device specified",
        "GST_ARGUS: Cleaning up",
        "2026-10-14 10:24:56 INFO [zed] source=jetson cameras=4",
        "2026-10-14 10:25:00 INFO [capture_pipeline.camera_slot] cam0 capture playing appsink→appsrc",
    ]
    assert [line for line in noise if not stdout_line_is_noise(line)] == []
    assert [line for line in keep if stdout_line_is_noise(line)] == []


def test_handshake_failure_is_one_warning_without_traceback() -> None:
    try:
        raise ConnectionError("no close frame received or sent")
    except ConnectionError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        name="websockets.server",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="opening handshake failed",
        args=(),
        exc_info=exc_info,
    )
    assert _HandshakeClosedFilter().filter(record) is True
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    assert record.getMessage() == "control websocket handshake closed: ConnectionError"

    root = logging.getLogger()
    previous_handlers = list(root.handlers)
    previous_level = root.level
    stream = io.StringIO()
    try:
        configure_logging(load_config(ROOT / "configs" / "jetson-quad.yaml"))
        assert any(
            isinstance(item, _HandshakeClosedFilter)
            for handler in root.handlers
            for item in handler.filters
        )
        ws_log = logging.getLogger("websockets.server")
        previous_propagate = ws_log.propagate
        previous_ws_handlers = list(ws_log.handlers)
        ws_log.handlers.clear()
        ws_log.propagate = False
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        handler.addFilter(_HandshakeClosedFilter())
        ws_log.addHandler(handler)
        ws_log.setLevel(logging.ERROR)
        try:
            raise ConnectionError("no close frame received or sent")
        except ConnectionError:
            ws_log.error("opening handshake failed", exc_info=sys.exc_info())
        text = stream.getvalue()
        assert "Traceback" not in text
        assert text.strip() == "WARNING control websocket handshake closed: ConnectionError"
        ws_log.handlers.clear()
        for handler in previous_ws_handlers:
            ws_log.addHandler(handler)
        ws_log.propagate = previous_propagate
    finally:
        root.handlers.clear()
        for handler in previous_handlers:
            root.addHandler(handler)
        root.setLevel(previous_level)


def test_handle_line_forwards_only_useful_stdout() -> None:
    watch = ArgusStderrWatch()
    seen: list[str] = []
    watch.arm(seen.append)
    read_fd, write_fd = os.pipe()
    watch._saved_out = write_fd
    try:
        watch._handle_line("Opening in BLOCKING MODE")
        watch._handle_line("NvMMLiteOpen : Block : BlockType = 4")
        watch._handle_line("Failed to create CameraProvider")
    finally:
        os.close(write_fd)
    try:
        forwarded = os.read(read_fd, 4096)
    finally:
        os.close(read_fd)
    assert seen == ["Failed to create CameraProvider"]
    assert forwarded == b"Failed to create CameraProvider\n"


def test_stderr_watch_fires_once_until_acknowledged() -> None:
    watch = ArgusStderrWatch()
    seen: list[str] = []
    watch.arm(seen.append)
    watch._note("plugin scan")
    watch._note("Failed to create CameraProvider")
    watch._note("Failed to create CameraProvider")
    assert seen == ["Failed to create CameraProvider"]
    watch.acknowledge()
    watch._note("Cannot create camera provider")
    assert len(seen) == 2


def test_restart_nvargus_daemon_logs_exit_code() -> None:
    completed = SimpleNamespace(returncode=0, stdout="ok", stderr="")
    code = restart_nvargus_daemon(run=lambda *_args, **_kwargs: completed)
    assert code == 0


def test_provider_failure_restarts_daemon_instead_of_slot(monkeypatch) -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    fatal: list[str] = []
    glib = MagicMock()
    glib.idle_add.side_effect = lambda fn: fn() or 1
    svc = CameraService(
        cfg,
        Gst=MagicMock(),
        GstRtspServer=MagicMock(),
        glib=glib,
        on_fatal=fatal.append,
    )
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
    pipeline = MagicMock()
    slot.capture_pipeline = pipeline
    svc._slots[slot.config.id] = slot
    svc._start_in_flight = slot
    svc._enqueue_starts = MagicMock()  # type: ignore[method-assign]
    restarts: list[str] = []

    class _InlineThread:
        def __init__(self, target, args, **_kwargs) -> None:
            self._target = target
            self._args = args

        def start(self) -> None:
            self._target(*self._args)

    monkeypatch.setattr(
        "capture_pipeline.camera_service.threading.Thread",
        _InlineThread,
    )
    monkeypatch.setattr(
        "capture_pipeline.camera_service.restart_nvargus_daemon",
        lambda: restarts.append("restart") or 0,
    )
    monkeypatch.setattr(
        "capture_pipeline.camera_service.wait_for_argus",
        lambda **_kwargs: 0,
    )

    svc._capture_failed(slot, pipeline, "Failed to create CameraProvider")

    assert restarts == ["restart"]
    assert fatal == []
    assert glib.timeout_add.call_count == 0
    svc._enqueue_starts.assert_called_once()


def test_provider_restart_failure_requests_process_exit(monkeypatch) -> None:
    cfg = load_config(ROOT / "configs" / "jetson-quad.yaml")
    fatal: list[str] = []
    glib = MagicMock()
    glib.idle_add.side_effect = lambda fn: fn() or 1
    svc = CameraService(
        cfg,
        Gst=MagicMock(),
        GstRtspServer=MagicMock(),
        glib=glib,
        on_fatal=fatal.append,
    )
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
    pipeline = MagicMock()
    slot.capture_pipeline = pipeline
    svc._slots[slot.config.id] = slot

    class _InlineThread:
        def __init__(self, target, args, **_kwargs) -> None:
            self._target = target
            self._args = args

        def start(self) -> None:
            self._target(*self._args)

    monkeypatch.setattr(
        "capture_pipeline.camera_service.threading.Thread",
        _InlineThread,
    )
    monkeypatch.setattr(
        "capture_pipeline.camera_service.restart_nvargus_daemon",
        lambda: 1,
    )
    svc.note_argus_provider_failure("Cannot create camera provider")
    assert fatal
    assert "exit=1" in fatal[0]
