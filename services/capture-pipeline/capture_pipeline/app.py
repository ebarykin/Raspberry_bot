"""Lifecycle сервиса: YAML → Gst.init → камеры → control WS → GLib → shutdown."""


from __future__ import annotations

import argparse
import logging
import os
import signal
from pathlib import Path
from typing import Any

from .argus_ready import restart_nvargus_daemon, wait_for_argus
from .argus_stderr import ArgusStderrWatch
from .camera_service import CameraService
from .config import load_config
from .control_ws import ControlWsServer
from .log_setup import configure_logging
from .pipelines.nav import ensure_nav_gst_plugin

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_CONFIG = _REPO_ROOT / "configs" / "jetson-quad.yaml"
logger = logging.getLogger("zed")


def _import_gst() -> tuple[Any, Any, Any, Any | None]:
    """Импорт GI после ``configure_logging``: GST_DEBUG должен быть до Gst.init.

    GstVideo опционален — только force-keyframe при смене ABR.
    """
    try:
        import gi  # type: ignore[import-untyped]
    except ModuleNotFoundError:
        logger.error(
            "PyGObject not found. Install: "
            "sudo apt install python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-rtsp-server-1.0"
        )
        raise SystemExit(1) from None

    gi.require_version("Gst", "1.0")
    gi.require_version("GstRtspServer", "1.0")
    from gi.repository import GLib, Gst, GstRtspServer  # type: ignore[reportMissingImports]

    GstVideo = None
    try:
        gi.require_version("GstVideo", "1.0")
        from gi.repository import GstVideo as _GstVideo  # type: ignore[reportMissingImports]

        GstVideo = _GstVideo
    except (ValueError, ImportError):
        logger.warning("GstVideo not available — ABR force-keyframe disabled")

    Gst.init(None)
    return Gst, GLib, GstRtspServer, GstVideo


def _recover_argus_provider(reason: str) -> bool:
    """Перезапустить nvargus-daemon и дождаться provider. False — процесс должен выйти."""
    logger.error("recovering argus camera provider: %s", reason)
    if restart_nvargus_daemon() != 0:
        logger.error("nvargus-daemon restart failed")
        return False
    if wait_for_argus(timeout_s=60, grace_s=0, log=lambda message: logger.info("%s", message)) != 0:
        logger.error("camera provider still unavailable after daemon restart")
        return False
    logger.info("argus camera provider recovered")
    return True


def _run_loop(glib: Any, loop: Any) -> None:
    """GLib MainLoop до SIGINT/SIGTERM. Control WS живёт в другом потоке."""

    def shutdown(signum: int, *_args: object) -> None:
        logger.info("shutdown signal received: %s", signal.Signals(signum).name)
        loop.quit()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    loop.run()


def main(argv: list[str] | None = None) -> int:
    """YAML → камеры → ABR WS → блокировка на GLib."""
    parser = argparse.ArgumentParser(description="capture-pipeline")
    parser.add_argument(
        "--config",
        default=os.environ.get("CONFIG_PATH", str(_DEFAULT_CONFIG)),
        help=f"path to YAML config (default: {_DEFAULT_CONFIG})",
    )
    args = parser.parse_args(argv)

    cfg_path = Path(args.config)
    if not cfg_path.is_file():
        logger.error("config not found: %s", cfg_path)
        return 1

    cfg = load_config(cfg_path)
    configure_logging(cfg)

    v = cfg.video
    active = cfg.abr.profiles[cfg.abr.default_profile]
    enabled_count = sum(1 for c in cfg.cameras if c.enabled)
    sensor_part = (
        f"sensor {v.sensor_width}x{v.sensor_height}@{v.fps} -> "
        if not cfg.is_emulate
        else ""
    )
    logger.info(
        "source=%s %sout %dx%d@%d profile=%s bitrate_kbps=%s fps=%s "
        "cameras=%s transport=appsink-appsrc config=%s",
        cfg.source,
        sensor_part,
        v.width,
        v.height,
        v.fps,
        cfg.abr.default_profile,
        active.bitrate_kbps,
        active.fps,
        enabled_count,
        cfg_path,
    )

    watch: ArgusStderrWatch | None = None
    if not cfg.is_emulate:
        watch = ArgusStderrWatch()
        watch.start()
        logger.info("argus stderr watch started (camera provider failures exit the capture loop)")

    Gst, glib, GstRtspServer, GstVideo = _import_gst()
    if watch is not None and watch.fatal:
        logger.error("argus camera provider failed during startup: %s", watch.fatal)
        if not _recover_argus_provider(watch.fatal):
            return 1
        watch.acknowledge()
    try:
        cfg = ensure_nav_gst_plugin(cfg, Gst)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1

    exit_reason: list[str] = []
    loop = glib.MainLoop()

    def _request_exit(reason: str) -> None:
        if exit_reason:
            return
        exit_reason.append(reason)
        logger.error("exiting so kubelet restarts the container: %s", reason)
        loop.quit()

    service = CameraService(
        cfg,
        Gst=Gst,
        GstRtspServer=GstRtspServer,
        GstVideo=GstVideo,
        glib=glib,
        on_fatal=_request_exit,
    )
    control_ws: ControlWsServer | None = None
    if cfg.control_ws.enabled:
        try:
            control_ws = ControlWsServer(
                host=cfg.control_ws.host,
                port=cfg.control_ws.port,
                camera_service=service,
                glib=glib,
            )
            control_ws.start()
            logger.info(
                "control WebSocket profiles=%s default=%s",
                sorted(cfg.abr.profiles),
                cfg.abr.default_profile,
            )
        except RuntimeError as exc:
            logger.error("control WebSocket startup failed: %s", exc)
            service.shutdown()
            return 1

    try:
        service.start_configured()
    except RuntimeError as exc:
        logger.error("%s", exc)
        if control_ws is not None:
            control_ws.stop()
        service.shutdown()
        return 1

    publishing = sum(1 for s in service.slots.values() if s.publishing)
    logger.info(
        "RTSP listening on %s camera(s) (%s configured)",
        publishing,
        len(cfg.cameras),
    )

    if watch is not None:
        watch.arm(lambda line: glib.idle_add(service.note_argus_provider_failure, line))

    try:
        _run_loop(glib, loop)
    finally:
        # Сначала WS, потом слоты: иначе ABR ещё пишет в уже остановленный Gst.
        if control_ws is not None:
            control_ws.stop()
        service.shutdown()

    return 1 if exit_reason else 0
