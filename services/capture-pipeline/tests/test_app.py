"""Проверки ошибок верхнего уровня приложения без GStreamer."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from capture_pipeline import app


ROOT = Path(__file__).resolve().parents[1]


def test_enabled_control_ws_bind_failure_is_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MagicMock()
    monkeypatch.setattr(app, "configure_logging", MagicMock())
    glib = MagicMock()
    monkeypatch.setattr(
        app,
        "_import_gst",
        MagicMock(return_value=(object(), glib, object(), object())),
    )
    monkeypatch.setattr(app, "CameraService", MagicMock(return_value=service))
    control = MagicMock()
    control.start.side_effect = RuntimeError("address already in use")
    monkeypatch.setattr(app, "ControlWsServer", MagicMock(return_value=control))
    run_loop = MagicMock()
    monkeypatch.setattr(app, "_run_loop", run_loop)

    result = app.main(["--config", str(ROOT / "configs" / "laptop-emulate.yaml")])

    assert result == 1
    service.shutdown.assert_called_once_with()
    run_loop.assert_not_called()
