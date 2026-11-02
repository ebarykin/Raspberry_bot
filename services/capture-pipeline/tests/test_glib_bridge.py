"""Tests for cross-thread dispatch onto the GLib main context."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from capture_pipeline.glib_bridge import run_on_glib


class ImmediateGLib:
    """Заглушка GLib: запоминает приоритет постановки в очередь."""

    PRIORITY_DEFAULT = 0

    def __init__(self) -> None:
        self.priority: int | None = None

    def idle_add(self, callback: Callable[[], bool], *, priority: int) -> int:
        self.priority = priority
        assert callback() is False
        return 1


class DeferredGLib:
    """Заглушка GLib: callback остаётся в очереди, пока тест его не вызовет."""

    PRIORITY_DEFAULT = 0

    def __init__(self) -> None:
        self.callback: Callable[[], bool] | None = None
        self.removed: list[int] = []

    def idle_add(self, callback: Callable[[], bool], *, priority: int) -> int:
        assert priority == self.PRIORITY_DEFAULT
        self.callback = callback
        return 17

    def source_remove(self, source_id: int) -> None:
        self.removed.append(source_id)


def test_run_on_glib_uses_default_priority() -> None:
    glib = ImmediateGLib()

    result: Any = run_on_glib(glib, lambda: {"ok": True})

    assert result == {"ok": True}
    assert glib.priority == glib.PRIORITY_DEFAULT


def test_run_on_glib_cancels_callback_that_did_not_start() -> None:
    glib = DeferredGLib()
    called = False

    def operation() -> None:
        nonlocal called
        called = True

    try:
        run_on_glib(glib, operation, timeout=0.001)
    except TimeoutError as exc:
        assert "request cancelled" in str(exc)
    else:
        raise AssertionError("timeout expected")

    assert glib.removed == [17]
    assert glib.callback is not None
    assert glib.callback() is False
    assert called is False
