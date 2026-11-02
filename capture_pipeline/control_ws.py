"""Control WebSocket: ABR (link-quality) и снимок профиля (status).

Streamer шлёт level; gst берёт abr.profiles[level]. Toggle камер и
per-cam bitrate сюда не ходят. 127.0.0.1 без auth; 0.0.0.0 без защиты нельзя.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

from .glib_bridge import run_on_glib

logger = logging.getLogger(__name__)


def dispatch_control_message(
    msg: dict[str, Any],
    *,
    camera_service: Any,
    glib: Any | None = None,
) -> dict[str, Any]:
    """Разобрать один JSON-запрос: ABR (link-quality) или снимок профиля (status).

    Протокол без framing id: один кадр — один ответ. Gst-мутации
    только через run_on_glib.

    Провод:
      → {"event":"link-quality","payload":{"level":"bad"}}
      ← {"ok":true,"profile":"bad","fps":18,"bitrate_kbps":450}
      → {"event":"status"}
      ← {"ok":true,"profile":"good","fps":30,"bitrate_kbps":1200}
    """
    if not isinstance(msg, dict):
        return {"ok": False, "error": "JSON object required"}

    def _run(fn: Any) -> Any:
        # Мутации GStreamer только с GLib MainLoop (иначе гонки с пайплайном).
        # ABR на 4×x264 под ffmpeg-клиентами часто >5s — даём запас.
        if glib is None:
            return fn()
        return run_on_glib(glib, fn, timeout=20.0)

    if msg.get("event") == "status":
        try:
            snap = camera_service.status()
        except Exception as exc:  # noqa: BLE001
            logger.exception("control_ws status failed")
            detail = str(exc) or repr(exc)
            return {"ok": False, "error": f"{type(exc).__name__}: {detail}"}
        return {
            "ok": True,
            "profile": snap.get("profile"),
            "fps": snap.get("fps"),
            "bitrate_kbps": snap.get("bitrate_kbps"),
            "cameras": snap.get("cameras", []),
        }

    if msg.get("event") != "link-quality":
        return {"ok": False, "error": 'only event "link-quality" or "status" is supported'}

    payload = msg.get("payload")
    if not isinstance(payload, dict):
        return {"ok": False, "error": "payload object required"}
    level = payload.get("level")
    if level is None or (isinstance(level, str) and not str(level).strip()):
        return {"ok": False, "error": "payload.level required"}
    force = bool(payload.get("force", False))

    def _apply() -> dict[str, Any]:
        # min_dwell живёт в CameraService / abr.*
        # force=true (lab preview) пропускает dwell — не для production streamer.
        return camera_service.set_profile_from_level(
            str(level).strip(), force=force
        )

    try:
        status = _run(_apply)
    except ValueError as err:
        return {"ok": False, "error": str(err) or repr(err)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("control_ws link-quality failed")
        detail = str(exc) or repr(exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {detail}"}

    # profile/fps/bitrate — факт после вызова (в т.ч. если dwell/already_active).
    return {
        "ok": True,
        "profile": status.get("profile"),
        "fps": status.get("fps"),
        "bitrate_kbps": status.get("bitrate_kbps"),
    }


class ControlWsServer:
    """Фоновый WS-сервер (:8591): один клиентский кадр → один ответ.

    Listen в daemon-thread со своим asyncio loop, чтобы не блокировать GLib.
    ABR уходит через to_thread + run_on_glib.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        camera_service: Any,
        glib: Any,
    ) -> None:
        """Сохранить параметры; listen начинается только в ``start()``."""
        self._host = host
        self._port = port
        self._camera_service = camera_service
        self._glib = glib
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: Any = None

    def start(self) -> None:
        """Поднятие listen в daemon-thread, чтобы не блокировать GLib MainLoop.

        Ждёт bind (или ошибку): иначе app.main мог бы залогировать «starting»,
        а порт уже занят — ABR молча не работает.
        """
        try:
            import websockets  # type: ignore[import-untyped]
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "websockets package required for control WebSocket. "
                "Install: python3 -m pip install websockets"
            ) from exc

        service = self._camera_service
        glib = self._glib
        ready = threading.Event()
        start_error: list[BaseException] = []

        def handle_raw(raw: str) -> dict[str, Any]:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                return {"ok": False, "error": "invalid JSON"}
            if not isinstance(msg, dict):
                return {"ok": False, "error": "JSON object required"}
            return dispatch_control_message(msg, camera_service=service, glib=glib)

        async def handler(websocket: Any) -> None:
            peer = getattr(websocket, "remote_address", None)
            # k8s-пробы дёргают status каждые 2с: на INFO это заливает лог.
            logger.debug("control WS client connected: %s", peer)
            try:
                async for message in websocket:
                    if not isinstance(message, str):
                        message = message.decode("utf-8", errors="replace")
                    # GLib-вызов блокирующий — уводим с asyncio loop.
                    reply = await asyncio.to_thread(handle_raw, message.strip())
                    await websocket.send(json.dumps(reply))
            except Exception as exc:  # noqa: BLE001
                logger.debug("control WS client closed: %s", exc)
            finally:
                logger.debug("control WS client disconnected: %s", peer)

        def thread_main() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)

            async def _serve() -> None:
                self._server = await websockets.serve(handler, self._host, self._port)
                logger.info("control WebSocket on ws://%s:%s", self._host, self._port)
                ready.set()
                await self._server.wait_closed()

            try:
                loop.run_until_complete(_serve())
            except Exception as exc:  # noqa: BLE001
                start_error.append(exc)
                ready.set()
                logger.exception("control WebSocket server failed")
            finally:
                loop.close()
                self._loop = None

        self._thread = threading.Thread(
            target=thread_main, name="capture-pipeline-control-ws", daemon=True
        )
        self._thread.start()
        if not ready.wait(timeout=5.0):
            self.stop()
            raise RuntimeError(
                f"control WebSocket did not bind within 5s "
                f"(ws://{self._host}:{self._port})"
            )
        if start_error:
            if self._thread is not None:
                self._thread.join(timeout=2.0)
                self._thread = None
            self._server = None
            raise RuntimeError(
                f"control WebSocket failed to bind "
                f"{self._host}:{self._port}: {start_error[0]}"
            ) from start_error[0]

    def stop(self) -> None:
        """Закрыть listen до CameraService.shutdown().

        Иначе in-flight ABR ещё пишет в уже останавливаемые пайплайны.
        """
        loop = self._loop
        server = self._server
        if loop is not None:

            async def _close() -> None:
                if server is not None:
                    server.close()
                    await server.wait_closed()

            try:
                fut = asyncio.run_coroutine_threadsafe(_close(), loop)
                fut.result(timeout=3.0)
            except Exception as exc:  # noqa: BLE001
                logger.debug("control WS stop: %s", exc)
            if server is None:
                loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._server = None
