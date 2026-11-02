"""YAML envelope becomes the GetCapabilities JSON string."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from robot_gateway.get_capabilities import (
    envelope_to_json,
    load_capabilities_envelope,
)

REPO_CAPABILITIES = (
    Path(__file__).resolve().parents[4] / "config" / "capabilities.yaml"
)


def test_repo_capabilities_roundtrip() -> None:
    envelope = load_capabilities_envelope(REPO_CAPABILITIES)
    payload = json.loads(envelope_to_json(envelope))
    assert payload["event"] == "robot-config"
    assert set(payload["payload"]) == {
        "schema_version",
        "units",
        "limits",
        "start_velocity",
        "commands",
    }
    assert payload["payload"]["schema_version"] == "1"
    assert payload["payload"]["units"] == "si"
    assert payload["payload"]["limits"] == {}
    assert payload["payload"]["start_velocity"] == {}
    assert payload["payload"]["commands"] == []


def test_non_mapping_root_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "capabilities.yaml"
    path.write_text("- not a mapping\n", encoding="utf-8")
    with pytest.raises(TypeError, match="must contain a mapping"):
        load_capabilities_envelope(path)


def test_missing_payload_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "capabilities.yaml"
    path.write_text(
        "event: robot-config\npayload:\n  schema_version: '1'\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing keys"):
        load_capabilities_envelope(path)
