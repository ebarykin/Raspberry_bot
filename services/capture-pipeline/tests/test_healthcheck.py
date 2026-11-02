"""Tests for Kubernetes probe status evaluation."""

from capture_pipeline.healthcheck import status_is_healthy


def test_liveness_accepts_responsive_service_during_camera_restart() -> None:
    payload = {"ok": True, "cameras": [{"enabled": True, "state": "restarting"}]}
    assert status_is_healthy(payload, require_playing=False)
    payload["cameras"][0]["enabled"] = False
    payload["cameras"][0]["state"] = "error"
    assert status_is_healthy(payload, require_playing=False)


def test_readiness_requires_at_least_one_camera_playing() -> None:
    healthy = {
        "ok": True,
        "cameras": [
            {"enabled": True, "state": "playing"},
            {"enabled": True, "state": "playing"},
        ],
    }
    assert status_is_healthy(healthy, require_playing=True)
    # Одна залипшая камера не должна валить пробу: иначе startupProbe убивает
    # контейнер по таймауту, что камеру не лечит и только гоняет Argus.
    healthy["cameras"][1]["state"] = "error"
    assert status_is_healthy(healthy, require_playing=True)


def test_readiness_rejects_status_with_no_camera_playing() -> None:
    payload = {
        "ok": True,
        "cameras": [
            {"enabled": True, "state": "error"},
            {"enabled": True, "state": "restarting"},
        ],
    }
    assert not status_is_healthy(payload, require_playing=True)
    assert status_is_healthy(payload, require_playing=False)


def test_readiness_rejects_empty_or_failed_status() -> None:
    assert not status_is_healthy({"ok": True, "cameras": []}, require_playing=True)
    assert not status_is_healthy({"ok": True, "cameras": []}, require_playing=False)
    assert not status_is_healthy({"ok": False}, require_playing=False)
