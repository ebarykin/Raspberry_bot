"""Ждать живой nvargus-daemon: сокет есть и camera provider создаётся.

Init-контейнер и сам драйвер вызывают это после ``systemctl restart``.
Файла ``/tmp/argus_socket`` недостаточно: демон создаёт его раньше, чем
начинает отвечать на RPC.
"""

from __future__ import annotations

import argparse
import logging
import os
import stat
import time
from typing import Any, Callable

from .pipeline_recovery import is_argus_provider_failure

logger = logging.getLogger(__name__)

LogFn = Callable[[str], None]
SleepFn = Callable[[float], None]
NowFn = Callable[[], float]
StatFn = Callable[[str], os.stat_result]
ProbeFn = Callable[[], tuple[bool, str]]


def _default_log(message: str) -> None:
    print(f"argus-ready: {message}", flush=True)


def wait_for_argus(
    *,
    timeout_s: float,
    grace_s: float,
    socket_path: str = "/tmp/argus_socket",
    probe_interval_s: float = 2.0,
    log: LogFn = _default_log,
    sleep: SleepFn = time.sleep,
    now: NowFn = time.monotonic,
    stat_socket: StatFn = os.stat,
    probe: ProbeFn | None = None,
) -> int:
    """0 когда провайдер создался, 1 по таймауту."""
    deadline = now() + timeout_s
    started = now()
    log(
        f"waiting for {socket_path} then camera provider "
        f"(timeout={timeout_s:.0f}s grace={grace_s:.0f}s)"
    )
    last_wait_log = started
    while True:
        elapsed = now() - started
        try:
            info = stat_socket(socket_path)
        except OSError as exc:
            if now() >= deadline:
                log(f"timeout after {elapsed:.1f}s, socket missing: {exc}")
                return 1
            if now() - last_wait_log >= 5:
                log(f"socket missing, elapsed={elapsed:.1f}s ({exc})")
                last_wait_log = now()
            sleep(min(1.0, max(0.0, deadline - now())))
            continue
        mode = stat.S_IMODE(info.st_mode)
        kind = "socket" if stat.S_ISSOCK(info.st_mode) else f"mode={oct(info.st_mode)}"
        log(
            f"socket present after {elapsed:.1f}s kind={kind} inode={info.st_ino} "
            f"perms={oct(mode)} size={info.st_size}"
        )
        if not stat.S_ISSOCK(info.st_mode):
            if now() >= deadline:
                log(f"timeout after {elapsed:.1f}s, path is not a socket")
                return 1
            sleep(min(1.0, max(0.0, deadline - now())))
            continue
        break

    if grace_s > 0:
        log(f"grace {grace_s:.0f}s before provider probe (daemon may still enumerate sensors)")
        sleep(grace_s)
    else:
        log(f"probing camera provider immediately, retry every {probe_interval_s:.0f}s")

    if probe is None:
        probe = probe_camera_provider
    attempt = 0
    while True:
        attempt += 1
        elapsed = now() - started
        log(f"provider probe attempt={attempt} elapsed={elapsed:.1f}s")
        try:
            ok, detail = probe()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        if ok:
            log(f"camera provider ready attempt={attempt} elapsed={elapsed:.1f}s ({detail})")
            return 0
        log(f"provider probe failed attempt={attempt}: {detail}")
        if now() >= deadline:
            log(f"timeout after {elapsed:.1f}s waiting for camera provider")
            return 1
        sleep(min(probe_interval_s, max(0.0, deadline - now())))


def probe_camera_provider(timeout_s: float = 3.0) -> tuple[bool, str]:
    """Короткий PAUSED ``nvarguscamerasrc``: создаёт provider и сразу отпускает.

    «No cameras» и прочие ответы демона считаются успехом ожидания: демон жив.
    Неуспех — только отсутствие элемента или ``Cannot create camera provider``.
    """
    import gi  # type: ignore[import-untyped]

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst  # type: ignore[reportMissingImports]

    if not Gst.is_initialized():
        Gst.init(None)
    src = Gst.ElementFactory.make("nvarguscamerasrc", "argus-ready-src")
    sink = Gst.ElementFactory.make("fakesink", "argus-ready-sink")
    if src is None or sink is None:
        return False, "nvarguscamerasrc or fakesink element not found"
    pipeline = Gst.Pipeline.new("argus-provider-probe")
    pipeline.add(src)
    pipeline.add(sink)
    if not src.link(sink):
        return False, "failed to link nvarguscamerasrc to fakesink"
    src.set_property("sensor-id", 0)
    try:
        src.set_property("num-buffers", 1)
    except TypeError:
        pass
    bus = pipeline.get_bus()
    try:
        change = pipeline.set_state(Gst.State.PAUSED)
        if change == Gst.StateChangeReturn.FAILURE:
            detail = _bus_error(bus, Gst) or "set_state(PAUSED) failed"
            if is_argus_provider_failure(detail):
                return False, detail
            return True, f"daemon answered: {detail}"
        msg = bus.timed_pop_filtered(
            int(timeout_s * Gst.SECOND),
            Gst.MessageType.ERROR | Gst.MessageType.ASYNC_DONE,
        )
        if msg is not None and msg.type == Gst.MessageType.ERROR:
            detail = _format_error(msg, Gst)
            if is_argus_provider_failure(detail):
                return False, detail
            return True, f"daemon answered: {detail}"
        return True, "paused" if msg is None else "async-done"
    finally:
        pipeline.set_state(Gst.State.NULL)
        pipeline.get_state(2 * Gst.SECOND)


def _bus_error(bus: Any, gst: Any) -> str:
    msg = bus.timed_pop_filtered(0, gst.MessageType.ERROR)
    if msg is None:
        return ""
    return _format_error(msg, gst)


def _format_error(msg: Any, gst: Any) -> str:
    err, debug = msg.parse_error()
    detail = err.message if err is not None else "unknown error"
    if debug:
        return f"{detail} ({debug})"
    return detail


def restart_nvargus_daemon(unit: str = "nvargus-daemon", *, run: Any | None = None) -> int:
    """``systemctl restart`` в namespace хоста. Под должен быть hostPID и privileged."""
    import subprocess

    argv = [
        "nsenter",
        "--target",
        "1",
        "--mount",
        "--uts",
        "--ipc",
        "--net",
        "--pid",
        "--",
        "systemctl",
        "restart",
        unit,
    ]
    logger.info("restarting host %s: %s", unit, " ".join(argv))
    runner = run or subprocess.run
    try:
        completed = runner(argv, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("restart %s failed to spawn: %s", unit, exc)
        return 1
    stderr = (completed.stderr or "").strip()
    stdout = (completed.stdout or "").strip()
    logger.info(
        "restart %s exit=%s stdout=%s stderr=%s",
        unit,
        completed.returncode,
        stdout or "-",
        stderr or "-",
    )
    return int(completed.returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wait until nvargus-daemon serves a camera provider")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--grace", type=float, default=0)
    parser.add_argument("--socket", default="/tmp/argus_socket")
    args = parser.parse_args(argv)
    return wait_for_argus(
        timeout_s=args.timeout,
        grace_s=args.grace,
        socket_path=args.socket,
    )


if __name__ == "__main__":
    raise SystemExit(main())
