"""Unit-тесты минимального control WS (link-quality only)."""

from __future__ import annotations

from unittest.mock import MagicMock

from capture_pipeline.control_ws import dispatch_control_message


def test_link_quality_ok() -> None:
    svc = MagicMock()
    svc.set_profile_from_level.return_value = {
        "profile": "bad",
        "fps": 18,
        "bitrate_kbps": 350,
        "bitrate_bps": 350000,
    }
    out = dispatch_control_message(
        {"event": "link-quality", "payload": {"level": "bad"}},
        camera_service=svc,
    )
    assert out == {
        "ok": True,
        "profile": "bad",
        "fps": 18,
        "bitrate_kbps": 350,
    }
    svc.set_profile_from_level.assert_called_once_with("bad", force=False)


def test_link_quality_force() -> None:
    svc = MagicMock()
    svc.set_profile_from_level.return_value = {
        "profile": "good",
        "fps": 30,
        "bitrate_kbps": 1100,
        "abr_applied": True,
    }
    out = dispatch_control_message(
        {"event": "link-quality", "payload": {"level": "good", "force": True}},
        camera_service=svc,
    )
    assert out["ok"] is True
    svc.set_profile_from_level.assert_called_once_with("good", force=True)


def test_rejects_unknown_event() -> None:
    out = dispatch_control_message(
        {"event": "health", "payload": {}},
        camera_service=MagicMock(),
    )
    assert out["ok"] is False
    assert "link-quality" in out["error"]


def test_status_returns_current_profile() -> None:
    svc = MagicMock()
    svc.status.return_value = {
        "profile": "good",
        "fps": 30,
        "bitrate_kbps": 1100,
    }
    out = dispatch_control_message({"event": "status"}, camera_service=svc)
    assert out == {
        "ok": True,
        "profile": "good",
        "fps": 30,
        "bitrate_kbps": 1100,
        "cameras": [],
    }
    svc.status.assert_called_once()


def test_status_does_not_queue_on_glib() -> None:
    svc = MagicMock()
    svc.status.return_value = {"profile": "good", "fps": 30, "bitrate_kbps": 1100}
    glib = MagicMock()
    dispatch_control_message({"event": "status"}, camera_service=svc, glib=glib)
    glib.idle_add.assert_not_called()


def test_rejects_missing_level() -> None:
    out = dispatch_control_message(
        {"event": "link-quality", "payload": {}},
        camera_service=MagicMock(),
    )
    assert out["ok"] is False
    assert "level" in out["error"]


def test_rejects_legacy_type_messages() -> None:
    out = dispatch_control_message(
        {"id": "1", "type": "abr.set", "level": "bad"},
        camera_service=MagicMock(),
    )
    assert out["ok"] is False


def test_unknown_level_value_error() -> None:
    svc = MagicMock()
    svc.set_profile_from_level.side_effect = ValueError("unknown level: nope")
    out = dispatch_control_message(
        {"event": "link-quality", "payload": {"level": "nope"}},
        camera_service=svc,
    )
    assert out == {"ok": False, "error": "unknown level: nope"}
