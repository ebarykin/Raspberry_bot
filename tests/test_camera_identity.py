"""Unit-тесты serial -> sensor_id resolver (без реального Jetson)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from capture_pipeline import camera_identity as ci

_LIST_DEVICES_OUTPUT = """\
NVIDIA Tegra Video Input Device (platform:tegra-camrtc-ca):
\t/dev/media0

vi-output, zedx 13-0010 (platform:tegra-capture-vi:0):
\t/dev/video2

vi-output, zedx 14-0011 (platform:tegra-capture-vi:1):
\t/dev/video3
"""

# Реальный вывод с борта: 5 камер, VI-каналы 0..4.
_BOARD_LIST_DEVICES_OUTPUT = """\
NVIDIA Tegra Video Input Device (platform:tegra-camrtc-ca):
\t/dev/media0

vi-output, zedx 13-0010 (platform:tegra-capture-vi:0):
\t/dev/video2

vi-output, zedx 13-0011 (platform:tegra-capture-vi:1):
\t/dev/video1

vi-output, zedx 14-0010 (platform:tegra-capture-vi:2):
\t/dev/video4

vi-output, zedx 14-0011 (platform:tegra-capture-vi:3):
\t/dev/video3

vi-output, zedx 9-0010 (platform:tegra-capture-vi:4):
\t/dev/video0
"""

# Серийники тех же модулей (dmesg: zedx <bus>: zedx_probe: Serial Number).
_BOARD_SERIALS = {
    "/dev/video2": 305776185,
    "/dev/video1": 309797923,
    "/dev/video4": 307361814,
    "/dev/video3": 300400271,
    "/dev/video0": 307049560,
}


def _fake_run(responses: dict[tuple[str, ...], SimpleNamespace]):
    def run(cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
        key = tuple(cmd)
        if key not in responses:
            raise AssertionError(f"unexpected command: {cmd}")
        return responses[key]

    return run


def _ok(stdout: str) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr="", returncode=0)


def _eeprom_response(serial: int) -> SimpleNamespace:
    """zed_eeprom_data: серийник — первые 4 байта little-endian."""
    return _ok(f"zed_eeprom_data: '{serial.to_bytes(4, 'little').hex()}aabbccdd'")


def _eeprom_cmd(devpath: str) -> tuple[str, ...]:
    return ("v4l2-ctl", "-d", devpath, "-C", "zed_eeprom_data")


def test_list_video_devices_parses_vi_output_blocks() -> None:
    run = _fake_run({("v4l2-ctl", "--list-devices"): _ok(_LIST_DEVICES_OUTPUT)})
    devices = ci._list_video_devices(run)
    assert devices == {
        ("13", "0010"): ci.VideoDevice(devpath="/dev/video2", vi_channel=0),
        ("14", "0011"): ci.VideoDevice(devpath="/dev/video3", vi_channel=1),
    }


def test_list_video_devices_marks_missing_vi_channel_as_none() -> None:
    listing = (
        "vi-output, zedx 13-0010 (platform:something-else):\n\t/dev/video2\n"
    )
    run = _fake_run({("v4l2-ctl", "--list-devices"): _ok(listing)})
    assert ci._list_video_devices(run) == {
        ("13", "0010"): ci.VideoDevice(devpath="/dev/video2", vi_channel=None)
    }


def test_list_video_devices_returns_empty_on_command_failure() -> None:
    def run(cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
        raise FileNotFoundError("v4l2-ctl not installed")

    assert ci._list_video_devices(run) == {}


def test_read_serial_decodes_first_4_bytes_little_endian() -> None:
    run = _fake_run({_eeprom_cmd("/dev/video2"): _ok("zed_eeprom_data: '01000000aabbccdd'")})
    assert ci._read_serial("/dev/video2", run) == 1


def test_read_serial_returns_none_on_nonzero_exit() -> None:
    run = _fake_run(
        {
            _eeprom_cmd("/dev/video2"): SimpleNamespace(
                stdout="", stderr="control not found", returncode=1
            )
        }
    )
    assert ci._read_serial("/dev/video2", run) is None


def test_discover_maps_serial_to_vi_channel() -> None:
    # Argus нумерует только заprobленные камеры, и этот порядок совпадает с
    # VI-каналом — поэтому sensor_id всегда в диапазоне 0..N-1. Нумерация
    # moduleN в devicetree для этого не годится: на этой плате 34 слота,
    # и позиция физических камер в них (16..20) Argus-ом не принимается.
    responses: dict[tuple[str, ...], SimpleNamespace] = {
        ("v4l2-ctl", "--list-devices"): _ok(_BOARD_LIST_DEVICES_OUTPUT)
    }
    for devpath, serial in _BOARD_SERIALS.items():
        responses[_eeprom_cmd(devpath)] = _eeprom_response(serial)

    assert ci.discover_serial_to_sensor_id(run=_fake_run(responses)) == {
        305776185: 0,
        309797923: 1,
        307361814: 2,
        300400271: 3,
        307049560: 4,
    }


def test_discover_ranks_sparse_vi_channels() -> None:
    # Одна камера во втором порту ZED Link: vi-channel 1, но Argus видит одну
    # камеру и открывает её как sensor-id 0.
    listing = "vi-output, zedx 9-0010 (platform:tegra-capture-vi:1):\n\t/dev/video0\n"
    run = _fake_run(
        {
            ("v4l2-ctl", "--list-devices"): _ok(listing),
            _eeprom_cmd("/dev/video0"): _eeprom_response(307049560),
        }
    )
    assert ci.discover_serial_to_sensor_id(run=run) == {307049560: 0}


def test_discover_ranks_by_channel_not_listing_order() -> None:
    listing = (
        "vi-output, zedx 14-0011 (platform:tegra-capture-vi:3):\n\t/dev/video3\n\n"
        "vi-output, zedx 13-0010 (platform:tegra-capture-vi:1):\n\t/dev/video2\n"
    )
    run = _fake_run(
        {
            ("v4l2-ctl", "--list-devices"): _ok(listing),
            _eeprom_cmd("/dev/video2"): _eeprom_response(42),
            _eeprom_cmd("/dev/video3"): _eeprom_response(43),
        }
    )
    assert ci.discover_serial_to_sensor_id(run=run) == {42: 0, 43: 1}


def test_discover_skips_device_without_vi_channel() -> None:
    listing = (
        "vi-output, zedx 13-0010 (platform:tegra-capture-vi:0):\n\t/dev/video2\n\n"
        "vi-output, zedx 14-0011 (platform:no-channel-here):\n\t/dev/video3\n"
    )
    run = _fake_run(
        {
            ("v4l2-ctl", "--list-devices"): _ok(listing),
            _eeprom_cmd("/dev/video2"): _eeprom_response(42),
        }
    )
    # /dev/video3 has no VI channel, so its serial is never read.
    assert ci.discover_serial_to_sensor_id(run=run) == {42: 0}


def test_discover_skips_device_whose_serial_cannot_be_read() -> None:
    run = _fake_run(
        {
            ("v4l2-ctl", "--list-devices"): _ok(_LIST_DEVICES_OUTPUT),
            _eeprom_cmd("/dev/video2"): _eeprom_response(42),
            _eeprom_cmd("/dev/video3"): SimpleNamespace(
                stdout="", stderr="control not found", returncode=1
            ),
        }
    )
    assert ci.discover_serial_to_sensor_id(run=run) == {42: 0}
