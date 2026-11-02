#!/usr/bin/env python3
"""Локальный preview RTSP → HTML для проверки камер capture-pipeline.

На Jetson/ноуте — N фоновых ffmpeg (RTSP→JPEG), сетка в браузере.
Поллинг /jpeg/<i> надёжнее multipart MJPEG в Chrome. Плиток = --ports.

С ноута: scripts/preview_jetson_ssh.sh (ssh -L 8765). Нужен ffmpeg в PATH.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from netem_lab import NetemController, choose_netem_iface

# Если UDP/netem «заморозил» ffmpeg без exit — перезапуск по тишине кадров.
_STALE_RESTART_S = 12.0
# LIVE если последний JPEG моложе этого порога (см. /api/cameras signal_stale_ms).
_SIGNAL_LIVE_MAX_MS = 2000
# UI ~8 fps; декод дешевле, чем 4× x264 @ 30 в emulate.
_GRAB_FPS = 12

PRESETS: dict[str, dict[str, Any]] = {
    "emulate": {
        "ports": (8554, 8555, 8556, 8557),
        "labels": ("cam0", "cam1", "cam2", "cam3"),
    },
    "jetson": {
        "ports": (8586, 8587, 8588, 8589),
        "labels": ("cam0", "cam1", "cam2", "cam3"),
    },
}

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_HTTP_PORT = 8765
DEFAULT_MOUNT = "/live"
DEFAULT_CONTROL_WS = "ws://127.0.0.1:8591"
DEFAULT_PID_FILE = Path(
    os.environ.get("PREVIEW_PID_FILE", "/tmp/capture_preview_quad.pid")
)
ABR_LEVELS = ("good", "normal", "degraded", "bad", "critical")
JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def _is_preview_pid(pid: int) -> bool:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    parts = [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]
    return any("preview_quad.py" in p for p in parts)


def _stop_pid(pid: int, *, wait_s: float = 2.0) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _pids_on_port(port: int) -> list[int]:
    """PIDs listening on TCP port (Linux ss). Empty if ss unavailable."""
    try:
        out = subprocess.check_output(
            ["ss", "-ltnp", f"sport = :{port}"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    pids: list[int] = []
    for token in out.replace(",", " ").split():
        if token.startswith("pid="):
            try:
                pids.append(int(token.split("=", 1)[1]))
            except ValueError:
                continue
    return sorted(set(pids))


def _reclaim_http_port(port: int, pid_file: Path) -> None:
    """Снять предыдущий preview_quad и дождаться свободного порта."""
    stale: list[int] = []
    if pid_file.is_file():
        try:
            old = int(pid_file.read_text().strip())
        except ValueError:
            old = 0
        if old and _is_preview_pid(old):
            stale.append(old)
        try:
            pid_file.unlink()
        except OSError:
            pass
    for pid in _pids_on_port(port):
        if _is_preview_pid(pid) and pid not in stale:
            stale.append(pid)
    for pid in stale:
        print(f"stopping previous preview pid={pid} (port {port})", file=sys.stderr)
        _stop_pid(pid)

    deadline = time.time() + 3.0
    while time.time() < deadline:
        left = [p for p in _pids_on_port(port) if p != os.getpid()]
        if not left:
            return
        # Ещё живой наш preview — добить.
        for pid in left:
            if _is_preview_pid(pid):
                _stop_pid(pid, wait_s=0.5)
        time.sleep(0.05)

    left = [p for p in _pids_on_port(port) if p != os.getpid()]
    if left:
        print(
            f"error: port {port} still in use by pid(s) {left}",
            file=sys.stderr,
        )
        raise SystemExit(1)


def _write_pid_file(pid_file: Path) -> None:
    pid_file.write_text(f"{os.getpid()}\n")

    def _unlink() -> None:
        try:
            if pid_file.is_file() and pid_file.read_text().strip() == str(os.getpid()):
                pid_file.unlink()
        except OSError:
            pass

    atexit.register(_unlink)


def jpeg_size(data: bytes) -> tuple[int | None, int | None]:
    """Ширина/высота из JPEG SOF0/SOF2 (без внешних зависимостей)."""
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):  # SOF
            height = (data[i + 5] << 8) | data[i + 6]
            width = (data[i + 7] << 8) | data[i + 8]
            return width, height
        if marker == 0xD9:  # EOI
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD9:
            i += 2
            continue
        if i + 3 >= n:
            break
        seglen = (data[i + 2] << 8) | data[i + 3]
        i += 2 + seglen
    return None, None


def _browser_url(bind: str, port: int) -> str:
    """URL for a local browser; 0.0.0.0/:: are not openable as-is."""
    host = (bind or "").strip().strip("[]")
    if host in ("0.0.0.0", "::", "::0", ""):
        host = "127.0.0.1"
    elif host == "::1":
        host = "127.0.0.1"
    return f"http://{host}:{port}/"


def _parse_ports(raw: str) -> tuple[int, ...]:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise argparse.ArgumentTypeError("need at least one port")
    return tuple(int(p) for p in parts)


def _parse_labels(raw: str | None, n: int) -> tuple[str, ...]:
    if not raw:
        return tuple(f"cam{i}" for i in range(n))
    labels = tuple(x.strip() for x in raw.split(",") if x.strip())
    if len(labels) != n:
        raise argparse.ArgumentTypeError(
            f"labels count ({len(labels)}) must match ports ({n})"
        )
    return labels


def build_cameras(
    *,
    host: str,
    ports: tuple[int, ...],
    mount: str,
    labels: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Собрать метаданные камер для API."""
    cameras: list[dict[str, Any]] = []
    for idx, (port, label) in enumerate(zip(ports, labels)):
        cameras.append(
            {
                "id": f"cam{idx}",
                "index": idx,
                "label": label,
                "port": port,
                "rtsp": f"rtsp://{host}:{port}{mount}",
                "jpeg": f"/jpeg/{idx}",
            }
        )
    return cameras


class FrameGrabber:
    """Фоновый ffmpeg: RTSP → последний JPEG в памяти (low-latency lab path)."""

    def __init__(
        self, rtsp_url: str, *, label: str, rtsp_transport: str = "udp"
    ) -> None:
        self.rtsp_url = rtsp_url
        self.label = label
        self.rtsp_transport = rtsp_transport if rtsp_transport in ("udp", "tcp") else "udp"
        self._lock = threading.Lock()
        self._jpeg: bytes | None = None
        self._error: str | None = None
        self._frame_count = 0
        self._last_frame_at: float | None = None
        self._width: int | None = None
        self._height: int | None = None
        self._started_at = time.time()
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._proc: subprocess.Popen[bytes] | None = None
        self._thread = threading.Thread(
            target=self._run, name=f"grab-{label}", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kick.set()
        self._kill_proc()

    def kick(self) -> None:
        """Сбросить RTSP-буфер ffmpeg (после ABR overlay/bitrate меняются сразу)."""
        self._kick.set()
        self._kill_proc()

    def latest(self) -> tuple[bytes | None, str | None]:
        with self._lock:
            return self._jpeg, self._error

    def snapshot(self) -> dict[str, Any]:
        """Диагностика для /api/cameras (без тела JPEG)."""
        with self._lock:
            now = time.time()
            age_ms = None
            if self._last_frame_at is not None:
                age_ms = int((now - self._last_frame_at) * 1000)
            has_frame = self._jpeg is not None
            # live: кадр был < _SIGNAL_LIVE_MAX_MS назад
            live = (
                has_frame
                and age_ms is not None
                and age_ms < _SIGNAL_LIVE_MAX_MS
            )
            return {
                "has_frame": has_frame,
                "live": live,
                "frame_count": self._frame_count,
                "frame_bytes": len(self._jpeg) if self._jpeg else 0,
                "width": self._width,
                "height": self._height,
                "age_ms": age_ms,
                "error": self._error,
                "uptime_s": int(now - self._started_at),
            }

    def _set_jpeg(self, data: bytes) -> None:
        w, h = jpeg_size(data)
        with self._lock:
            self._jpeg = data
            self._error = None
            self._frame_count += 1
            self._last_frame_at = time.time()
            if w is not None and h is not None:
                self._width = w
                self._height = h

    def _set_error(self, msg: str) -> None:
        with self._lock:
            self._error = msg

    def _kill_proc(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            proc.kill()
        except OSError:
            pass

    def _run(self) -> None:
        """Перезапускать ffmpeg при обрыве / kick, пока не stop."""
        while not self._stop.is_set():
            self._kick.clear()
            # Continuous low-latency decode (как ffplay). Не делаем kick/reconnect
            # после ABR — RTSP handshake как раз и давал 1–3с «тормоза».
            jpeg_path = Path(f"/tmp/zed-preview-{self.label}.jpg")
            try:
                jpeg_path.unlink(missing_ok=True)
            except OSError:
                pass
            # ffmpeg 8 on Jetson: image2pipe + tiny probesize never emits JPEG.
            # -update 1 overwrites one file; we poll it (same SOI/EOI check).
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-fflags",
                "nobuffer+discardcorrupt",
                "-flags",
                "low_delay",
                "-probesize",
                "65536",
                "-analyzeduration",
                "0",
                "-max_delay",
                "0",
                "-rtsp_transport",
                self.rtsp_transport,
                "-i",
                self.rtsp_url,
                "-an",
                "-vf",
                "scale=480:-1",
                "-q:v",
                "5",
                "-update",
                "1",
                "-y",
                str(jpeg_path),
            ]
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as exc:
                self._set_error(str(exc))
                self._stop.wait(2.0)
                continue

            self._proc = proc
            session_started = time.time()
            last_mtime_ns = -1
            try:
                while not self._stop.is_set() and not self._kick.is_set():
                    try:
                        st = jpeg_path.stat()
                    except OSError:
                        st = None
                    if st is not None and st.st_mtime_ns != last_mtime_ns and st.st_size > 100:
                        last_mtime_ns = st.st_mtime_ns
                        try:
                            data = jpeg_path.read_bytes()
                        except OSError:
                            data = b""
                        if data.startswith(JPEG_SOI):
                            self._set_jpeg(data)
                    with self._lock:
                        last = self._last_frame_at
                    ref = last if last is not None else session_started
                    if (time.time() - ref) >= _STALE_RESTART_S:
                        self._set_error(
                            f"stale >{_STALE_RESTART_S:.0f}s — restart ffmpeg"
                        )
                        break
                    if proc.poll() is not None:
                        break
                    time.sleep(max(0.05, 1.0 / _GRAB_FPS))
            finally:
                self._proc = None
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                try:
                    jpeg_path.unlink(missing_ok=True)
                except OSError:
                    pass
                if self._stop.is_set():
                    break
                if self._kick.is_set():
                    continue
                self._stop.wait(0.4)


_ABR_LOCK = threading.Lock()


class ControlWsClient:
    """Держит один WS к control порту — без connect на каждый ABR-клик."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._thread_main, name="control-ws-client", daemon=True
        )

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(timeout=5.0)
        loop = self._loop
        if loop is not None:
            # Прогрев соединения, чтобы первый ABR-клик был без handshake.
            asyncio.run_coroutine_threadsafe(self._ensure_ws(5.0), loop)

    def stop(self) -> None:
        self._stop.set()
        loop = self._loop
        if loop is None or not loop.is_running():
            return

        async def _shutdown() -> None:
            ws = self._ws
            self._ws = None
            if ws is not None:
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001
                    pass
            loop.stop()

        fut = asyncio.run_coroutine_threadsafe(_shutdown(), loop)
        try:
            fut.result(timeout=2.0)
        except Exception:  # noqa: BLE001
            pass
        self._thread.join(timeout=2.0)

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    @staticmethod
    def _ws_alive(ws: Any) -> bool:
        state = getattr(ws, "state", None)
        if state is not None:
            name = getattr(state, "name", str(state))
            return name == "OPEN"
        return bool(getattr(ws, "open", False))

    async def _ensure_ws(self, timeout: float) -> Any:
        import websockets

        if self._ws is not None and self._ws_alive(self._ws):
            return self._ws
        self._ws = None
        self._ws = await websockets.connect(self._url, open_timeout=timeout)
        return self._ws

    async def _rpc(self, request: dict[str, Any], timeout: float) -> dict[str, Any]:
        last_err: Exception | None = None
        for _attempt in range(2):
            try:
                ws = await self._ensure_ws(timeout)
                await ws.send(json.dumps(request))
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                reply = json.loads(raw)
                if not isinstance(reply, dict):
                    return {"ok": False, "error": "non-object reply"}
                return reply
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                self._ws = None
        detail = str(last_err) or repr(last_err)
        return {
            "ok": False,
            "error": f"{type(last_err).__name__}: {detail}",
        }

    def send_link_quality(
        self, level: str, *, force: bool = True, timeout: float = 25.0
    ) -> dict[str, Any]:
        level = str(level).strip().lower()
        if level not in ABR_LEVELS:
            return {
                "ok": False,
                "error": f"unknown level {level!r}, use one of {list(ABR_LEVELS)}",
            }
        payload: dict[str, Any] = {"level": level}
        if force:
            payload["force"] = True
        request = {"event": "link-quality", "payload": payload}
        try:
            import websockets  # noqa: F401
        except ModuleNotFoundError:
            return {
                "ok": False,
                "error": "websockets package required (pip install websockets)",
            }
        loop = self._loop
        if loop is None or not self._ready.is_set():
            return {
                "ok": False,
                "error": "control WS client not ready",
            }
        if not _ABR_LOCK.acquire(timeout=timeout):
            return {
                "ok": False,
                "error": "ABR busy: previous link-quality still in flight",
            }
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._rpc(request, timeout), loop
            )
            return fut.result(timeout=timeout + 1.0)
        except Exception as exc:  # noqa: BLE001
            detail = str(exc) or repr(exc)
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {detail}",
            }
        finally:
            _ABR_LOCK.release()


def send_link_quality(
    control_ws: str,
    level: str,
    *,
    force: bool = True,
    timeout: float = 25.0,
    client: ControlWsClient | None = None,
) -> dict[str, Any]:
    """ABR RPC: предпочитает persistent client, иначе one-shot connect."""
    if client is not None:
        return client.send_link_quality(level, force=force, timeout=timeout)

    level = str(level).strip().lower()
    if level not in ABR_LEVELS:
        return {
            "ok": False,
            "error": f"unknown level {level!r}, use one of {list(ABR_LEVELS)}",
        }
    payload: dict[str, Any] = {"level": level}
    if force:
        payload["force"] = True
    request = {"event": "link-quality", "payload": payload}
    try:
        import websockets
    except ModuleNotFoundError:
        return {
            "ok": False,
            "error": "websockets package required (pip install websockets)",
        }

    async def _once() -> dict[str, Any]:
        async with websockets.connect(control_ws, open_timeout=timeout) as ws:
            await ws.send(json.dumps(request))
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        reply = json.loads(raw)
        if not isinstance(reply, dict):
            return {"ok": False, "error": "non-object reply"}
        return reply

    if not _ABR_LOCK.acquire(timeout=timeout):
        return {
            "ok": False,
            "error": "ABR busy: previous link-quality still in flight",
        }
    try:
        return asyncio.run(_once())
    except Exception as exc:  # noqa: BLE001
        detail = str(exc) or repr(exc)
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {detail}",
        }
    finally:
        _ABR_LOCK.release()


class PreviewHandler(BaseHTTPRequestHandler):
    """HTTP: UI, /api/cameras, /jpeg/<i>, POST /api/link-quality."""

    protocol_version = "HTTP/1.1"
    cameras: list[dict[str, Any]] = []
    grabbers: list[FrameGrabber] = []
    static_dir: Path = STATIC_DIR
    control_ws: str = DEFAULT_CONTROL_WS
    abr_client: ControlWsClient | None = None
    netem: NetemController | None = None
    # Последний известный ABR (UI + curl делят одно состояние).
    abr_state: dict[str, Any] = {
        "profile": "good",
        "fps": 30,
        "bitrate_kbps": 1100,
        "source": "default",
    }

    def log_message(self, fmt: str, *args: Any) -> None:
        # Горячий путь (JPEG / status poll) не логируем при 2xx/3xx.
        path = urlparse(getattr(self, "path", "") or "").path
        code: int | None = None
        if len(args) >= 2:
            try:
                code = int(args[1])
            except (TypeError, ValueError):
                code = None
        if code is not None and code < 400:
            if (
                path.startswith("/jpeg/")
                or path
                in (
                    "/api/cameras",
                    "/api/meta",
                    "/api/link-quality",
                    "/api/netem",
                    "/",
                    "/index.html",
                )
                or path.endswith((".js", ".css", ".map", ".ico", ".woff2"))
            ):
                return
        try:
            msg = fmt % args
        except Exception:  # noqa: BLE001
            msg = f"{fmt} {args}"
        sys.stderr.write("%s - %s\n" % (self.address_string(), msg))

    def handle(self) -> None:
        # Браузер часто рвёт JPEG/poll-соединения — это не ошибка lab-сервера.
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            pass

    def finish(self) -> None:
        try:
            super().finish()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            pass

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_file(
                self.static_dir / "preview_quad.html", "text/html; charset=utf-8"
            )
            return
        if path == "/api/meta":
            body = json.dumps(
                {
                    "control_ws": self.control_ws,
                    "abr_levels": list(ABR_LEVELS),
                    "netem": self.netem.status() if self.netem else None,
                    "purpose": (
                        "Verify capture-pipeline RTSP publish and simulate "
                        "streamer ABR (link-quality) without webrtc-client"
                    ),
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self._send_bytes(body, "application/json; charset=utf-8")
            return
        if path == "/api/netem":
            if self.netem is None:
                self._send_json(
                    {"ok": False, "error": "netem controller not configured"},
                    status=503,
                )
                return
            self._send_json(self.netem.status())
            return
        if path == "/api/cameras":
            cams = []
            live_n = 0
            nosignal_n = 0
            for cam, grabber in zip(self.cameras, self.grabbers):
                entry = dict(cam)
                status = grabber.snapshot()
                # signal: live | stale | no_signal (не «камера выключена»)
                if status.get("live"):
                    signal = "live"
                    live_n += 1
                elif status.get("has_frame"):
                    signal = "stale"
                else:
                    signal = "no_signal"
                    nosignal_n += 1
                status["signal"] = signal
                entry["status"] = status
                cams.append(entry)
            body = json.dumps(
                {
                    "purpose": (
                        "Verify RTSP publish: each tile must be LIVE (fresh frames). "
                        "NO SIGNAL = RTSP unreachable / no frames — not a UI power switch."
                    ),
                    "camera_count": len(cams),
                    "live_count": live_n,
                    "no_signal_count": nosignal_n,
                    "signal_stale_ms": _SIGNAL_LIVE_MAX_MS,
                    "poll_hint_ms": 120,
                    "control_ws": self.control_ws,
                    "abr_levels": list(ABR_LEVELS),
                    "abr": self.abr_state,
                    "netem": self.netem.status() if self.netem else None,
                    "cameras": cams,
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self._send_bytes(body, "application/json; charset=utf-8")
            return
        if path.startswith("/jpeg/"):
            try:
                idx = int(path.rsplit("/", 1)[-1])
            except ValueError:
                self.send_error(404, "bad jpeg index")
                return
            if idx < 0 or idx >= len(self.grabbers):
                self.send_error(404, "unknown camera")
                return
            jpeg, err = self.grabbers[idx].latest()
            if jpeg is None:
                msg = (err or "waiting for first frame").encode("utf-8")
                self.send_response(503)
                self._cors()
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return
            self._send_bytes(jpeg, "image/jpeg")
            return
        safe = path.lstrip("/").replace("..", "")
        asset = self.static_dir / safe
        if asset.is_file():
            ctype = "application/octet-stream"
            if asset.suffix == ".js":
                ctype = "application/javascript"
            elif asset.suffix == ".css":
                ctype = "text/css"
            elif asset.suffix == ".png":
                ctype = "image/png"
            elif asset.suffix in (".jpg", ".jpeg"):
                ctype = "image/jpeg"
            elif asset.suffix == ".svg":
                ctype = "image/svg+xml"
            elif asset.suffix == ".webp":
                ctype = "image/webp"
            self._serve_file(asset, ctype)
            return
        self.send_error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/netem":
            self._handle_netem_post()
            return
        if path != "/api/link-quality":
            self.send_error(404, "not found")
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._send_json({"ok": False, "error": "invalid JSON"}, status=400)
            return
        if not isinstance(payload, dict):
            self._send_json({"ok": False, "error": "JSON object required"}, status=400)
            return
        level = payload.get("level")
        if level is None or not str(level).strip():
            self._send_json({"ok": False, "error": "level required"}, status=400)
            return
        result = send_link_quality(
            self.control_ws, str(level), client=self.abr_client
        )
        # Lab/журнал показывают контракт README (ok/profile/fps/bitrate_kbps).
        if result.get("ok"):
            result = {
                "ok": True,
                "profile": result.get("profile"),
                "fps": result.get("fps"),
                "bitrate_kbps": result.get("bitrate_kbps"),
            }
        else:
            result = {
                "ok": False,
                "error": result.get("error") or "link-quality failed",
            }
        if result.get("ok") and result.get("profile"):
            PreviewHandler.abr_state = {
                "profile": result.get("profile"),
                "fps": result.get("fps"),
                "bitrate_kbps": result.get("bitrate_kbps"),
                "source": "link-quality",
                "level": str(level).strip().lower(),
            }
        status = 200 if result.get("ok") else 502
        if result.get("ok"):
            sys.stderr.write(
                f"ABR level={level} → profile={result.get('profile')} "
                f"fps={result.get('fps')} bitrate_kbps={result.get('bitrate_kbps')}\n"
            )
        else:
            sys.stderr.write(
                f"ABR level={level} FAIL: {result.get('error') or result}\n"
            )
        self._send_json(result, status=status)

    def _handle_netem_post(self) -> None:
        if self.netem is None:
            self._send_json(
                {"ok": False, "error": "netem controller not configured"},
                status=503,
            )
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._send_json({"ok": False, "error": "invalid JSON"}, status=400)
            return
        if not isinstance(payload, dict):
            self._send_json({"ok": False, "error": "JSON object required"}, status=400)
            return
        preset = payload.get("preset")
        if preset is None or not str(preset).strip():
            self._send_json({"ok": False, "error": "preset required"}, status=400)
            return
        result = self.netem.apply(str(preset))
        status = 200 if result.get("ok") else 502
        if result.get("ok"):
            sys.stderr.write(
                f"NETEM preset={preset} iface={result.get('iface')} "
                f"match={result.get('match')} rate_kbps={result.get('rate_kbps')}\n"
            )
            # UDP-сессии после tc часто «живые, но немые» — форсим reconnect.
            for g in self.grabbers:
                g.kick()
        else:
            sys.stderr.write(
                f"NETEM preset={preset} FAIL: {result.get('error') or result}\n"
            )
        self._send_json(result, status=status)

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, obj: dict[str, Any], *, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, content_type: str) -> None:
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_error(404, f"missing {path.name}")
            return
        self._send_bytes(path.read_bytes(), content_type)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="RTSP N-cam preview: HTML + JPEG poll (dynamic camera count)"
    )
    parser.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        default=None,
        help="emulate → 8554–8557; jetson → 8586–8589",
    )
    parser.add_argument("--host", default="127.0.0.1", help="RTSP host")
    parser.add_argument(
        "--rtsp-transport",
        choices=("udp", "tcp"),
        default="udp",
        help="ffmpeg RTSP media transport (default udp — aligns with lab UDP netem)",
    )
    parser.add_argument(
        "--ports",
        type=_parse_ports,
        default=None,
        help="comma-separated RTSP ports (overrides preset)",
    )
    parser.add_argument(
        "--labels",
        default=None,
        help="comma-separated labels, same count as ports",
    )
    parser.add_argument("--mount", default=DEFAULT_MOUNT, help="RTSP mount path")
    parser.add_argument(
        "--http-port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help=f"HTTP listen port (default {DEFAULT_HTTP_PORT})",
    )
    parser.add_argument("--bind", default="127.0.0.1", help="HTTP bind address")
    parser.add_argument(
        "--control-ws",
        default=DEFAULT_CONTROL_WS,
        help=f"zed control WebSocket for ABR simulate (default {DEFAULT_CONTROL_WS})",
    )
    parser.add_argument(
        "--netem-iface",
        default=None,
        help="iface for lab tc/netem (default: lo if RTSP host is loopback, else default-route)",
    )
    parser.add_argument(
        "--netem-match",
        choices=("udp", "udp-sports"),
        default="udp",
        help="udp=all UDP egress (teleop-like); udp-sports=UDP source ports only",
    )
    args = parser.parse_args(argv)

    if args.ports is not None:
        ports = args.ports
    elif args.preset is not None:
        ports = PRESETS[args.preset]["ports"]
    else:
        ports = PRESETS["emulate"]["ports"]

    if args.labels is not None:
        try:
            labels = _parse_labels(args.labels, len(ports))
        except argparse.ArgumentTypeError as exc:
            parser.error(str(exc))
    elif args.preset is not None and len(ports) == len(PRESETS[args.preset]["labels"]):
        labels = PRESETS[args.preset]["labels"]
    else:
        labels = tuple(f"cam{i}" for i in range(len(ports)))

    if shutil.which("ffmpeg") is None:
        print("error: ffmpeg not found in PATH", file=sys.stderr)
        return 1

    cameras = build_cameras(
        host=args.host, ports=ports, mount=args.mount, labels=labels
    )
    html_path = STATIC_DIR / "preview_quad.html"
    if not html_path.is_file():
        print(f"error: missing {html_path}", file=sys.stderr)
        return 1

    grabbers = [
        FrameGrabber(
            cam["rtsp"], label=cam["label"], rtsp_transport=args.rtsp_transport
        )
        for cam in cameras
    ]
    for g in grabbers:
        g.start()

    abr_client = ControlWsClient(args.control_ws)
    abr_client.start()

    netem_iface = choose_netem_iface(args.host, args.netem_iface)
    netem = NetemController(
        iface=netem_iface,
        ports=ports,
        match=args.netem_match,
    )

    PreviewHandler.cameras = cameras
    PreviewHandler.grabbers = grabbers
    PreviewHandler.static_dir = STATIC_DIR
    PreviewHandler.control_ws = args.control_ws
    PreviewHandler.abr_client = abr_client
    PreviewHandler.netem = netem

    pid_file = DEFAULT_PID_FILE
    _reclaim_http_port(args.http_port, pid_file)
    _write_pid_file(pid_file)

    server = ReusableThreadingHTTPServer((args.bind, args.http_port), PreviewHandler)
    stop_lock = threading.Lock()
    stopping = False

    def _cleanup() -> None:
        """Stop grabbers / ABR / netem and unblock serve_forever.

        Must not run heavy work on the signal thread (subprocess/tc can deadlock).
        """
        nonlocal stopping
        with stop_lock:
            if stopping:
                return
            stopping = True
        try:
            netem.apply("clear")
        except Exception:  # noqa: BLE001
            pass
        for g in grabbers:
            g.stop()
        abr_client.stop()
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            server.server_close()
        except Exception:  # noqa: BLE001
            pass

    def _on_signal(signum: int, _frame: object) -> None:
        # Defer cleanup; second signal uses default (hard kill).
        # SIGTSTP (Ctrl+Z) тоже гасим: иначе процесс «висит» и держит порт.
        threading.Thread(target=_cleanup, name="preview-shutdown", daemon=True).start()
        signal.signal(signum, signal.SIG_DFL)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGTSTP, _on_signal)

    print(
        f"preview cameras={len(cameras)} preset={args.preset or 'custom'} "
        f"control_ws={args.control_ws}  "
        f"rtsp_transport={args.rtsp_transport}  "
        f"netem_iface={netem_iface} netem_match={args.netem_match}"
    )
    for cam in cameras:
        print(f"  {cam['label']}: {cam['rtsp']} → {cam['jpeg']}")
    print()
    print(f"Open preview: {_browser_url(args.bind, args.http_port)}")
    print()

    # brief wait so first frames likely ready before browser opens
    time.sleep(0.8)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _cleanup()
    finally:
        _cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
