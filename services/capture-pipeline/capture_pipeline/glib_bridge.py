"""Выполнить функцию на потоке GLib и вернуть результат в вызывающий поток.

GStreamer и GstRTSPServer живут в GLib MainLoop. Control WS — другой поток
(asyncio). Менять пайплайн/энкодер из WS нельзя: гонка с bus и RTSP.

Этот модуль только переносит вызов: очередь на MainLoop, результат обратно.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from typing import Any, TypeVar

T = TypeVar("T")


def run_on_glib(glib: Any, fn: Callable[[], T], *, timeout: float = 5.0) -> T:
    """Вызвать ``fn`` на GLib MainLoop и дождаться ответа в этом потоке.

    ``idle_add`` ставит работу в очередь цикла (не запускает её здесь).
    ``Future`` — ящик между потоками: MainLoop кладёт результат или ошибку,
    WS-поток блокируется на ``result()``, пока ящик не заполнят. Таймаут
    отменяет только ещё не начатую работу. Начавшуюся GStreamer-операцию
    безопасно прервать нельзя, поэтому её результат ожидается до завершения.
    """
    fut: Future[T] = Future()

    def _idle() -> bool:
        if not fut.set_running_or_notify_cancel():
            return False
        try:
            fut.set_result(fn())
        except Exception as exc:  # noqa: BLE001
            fut.set_exception(exc)
        return False

    # Обычный idle-приоритет может бесконечно голодать под непрерывными
    # событиями четырёх RTSP-серверов. Обычный приоритет GLib сохраняет
    # асинхронный вызов, но не даёт status/ABR застрять под видеонагрузкой.
    source_id = glib.idle_add(_idle, priority=glib.PRIORITY_DEFAULT)
    try:
        return fut.result(timeout=timeout)
    except TimeoutError as exc:
        if fut.cancel():
            glib.source_remove(source_id)
            raise TimeoutError(
                f"GLib callback did not start within {timeout:.0f}s; request cancelled"
            ) from exc
        # Callback уже выполняется или завершился одновременно с таймаутом.
        # Возврат ошибки здесь соврал бы клиенту: ABR мог примениться позднее.
        return fut.result()
