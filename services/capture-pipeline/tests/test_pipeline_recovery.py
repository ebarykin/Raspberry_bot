"""Tests for pipeline restart backoff."""

from __future__ import annotations

from capture_pipeline.pipeline_recovery import (
    is_argus_provider_failure,
    is_ignorable_argus_bus_error,
    next_backoff_ms,
)


def test_argus_correctable_error_is_ignored() -> None:
    assert is_ignorable_argus_bus_error(
        "INVALID_SETTINGS (Argus Correctable Error Status)"
    )
    assert is_ignorable_argus_bus_error("Argus Correctable Error Status")
    assert not is_ignorable_argus_bus_error("No cameras available")
    assert not is_ignorable_argus_bus_error("")


def test_argus_provider_failure_is_not_a_slot_restart() -> None:
    assert is_argus_provider_failure(
        "(Argus) Error Timeout: Cannot create camera provider"
    )
    assert is_argus_provider_failure(
        "Error generated. Failed to create CameraProvider"
    )
    assert not is_argus_provider_failure("end-of-stream")
    assert not is_argus_provider_failure("Argus Correctable Error Status")
    assert not is_argus_provider_failure("")


def test_next_backoff_first_attempt() -> None:
    assert next_backoff_ms(1, base_ms=500, max_ms=30_000) == 500


def test_next_backoff_exponential() -> None:
    assert next_backoff_ms(2, base_ms=500, max_ms=30_000) == 1000
    assert next_backoff_ms(3, base_ms=500, max_ms=30_000) == 2000
    assert next_backoff_ms(4, base_ms=500, max_ms=30_000) == 4000


def test_next_backoff_capped() -> None:
    assert next_backoff_ms(10, base_ms=500, max_ms=3000) == 3000
