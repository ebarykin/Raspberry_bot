"""serial -> живой Argus sensor_id для камер ZED X One на этой плате.

Argus нумерует сенсоры только среди реально заprobленных модулей, в порядке
VI-канала (``platform:tegra-capture-vi:N``), который ``v4l2-ctl --list-devices``
печатает рядом с каждым ``/dev/videoN``. Сам номер канала индексом не является:
каналы бывают с дырами (одна камера во втором порту ZED Link сидит на
vi-channel 1), а Argus всё равно отдаёт ``0..N-1``. Поэтому sensor_id — это
позиция канала среди найденных, а не его номер.

Нумерация ``moduleN`` в devicetree для этого не годится: там объявлены слоты
под все варианты плат (на борту их 34, включая дубли ``module0`` и
``module00``), так что позиция модуля в этом списке не имеет отношения к
индексу, который примет Argus.

Серийник каждого модуля читается с его EEPROM через кастомный V4L2-контрол
``zed_eeprom_data`` драйвера ``sl_zedxone_uhd`` и не меняется при пересборке
devicetree. Сопоставляя serial -> sensor_id на каждом старте, конфиг камеры
может ссылаться на физический модуль по серийнику вместо хрупкого sensor_id,
подобранного руками под плату.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

# "vi-output, zedx 13-0010 (platform:tegra-capture-vi:0):\n        /dev/video2"
_VI_OUTPUT_RE = re.compile(
    r"vi-output,\s*zedx\s+(\d+)-([0-9a-fA-F]+)([^\n]*):\s*\n\s*(/dev/video\d+)"
)
_VI_CHANNEL_RE = re.compile(r"tegra-capture-vi:(\d+)")
_EEPROM_VALUE_RE = re.compile(r"'([0-9a-fA-F]+)'")


class VideoDevice(NamedTuple):
    devpath: str
    vi_channel: int | None


def _list_video_devices(run: Any) -> dict[tuple[str, str], VideoDevice]:
    """{(i2c_bus, i2c_addr_hex4): VideoDevice} из ``v4l2-ctl --list-devices``."""
    try:
        result = run(
            ["v4l2-ctl", "--list-devices"], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.error("camera discovery: v4l2-ctl --list-devices failed: %s", exc)
        return {}
    devices: dict[tuple[str, str], VideoDevice] = {}
    for bus, addr, tail, devpath in _VI_OUTPUT_RE.findall(result.stdout):
        channel = _VI_CHANNEL_RE.search(tail)
        devices[(bus, addr.lower().zfill(4))] = VideoDevice(
            devpath=devpath,
            vi_channel=int(channel.group(1)) if channel else None,
        )
    return devices


def _read_serial(devpath: str, run: Any) -> int | None:
    """Серийник из EEPROM: первые 4 байта ``zed_eeprom_data``, little-endian."""
    result = run(
        ["v4l2-ctl", "-d", devpath, "-C", "zed_eeprom_data"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.warning(
            "camera discovery: zed_eeprom_data read failed on %s: %s",
            devpath,
            (result.stderr or "").strip(),
        )
        return None
    match = _EEPROM_VALUE_RE.search(result.stdout)
    if not match:
        return None
    first_4_bytes = match.group(1)[:8]
    if len(first_4_bytes) < 8:
        return None
    reversed_hex = "".join(
        first_4_bytes[i : i + 2] for i in range(6, -2, -2)
    )
    return int(reversed_hex, 16)


def main(argv: list[str] | None = None) -> int:
    """``python3 -m capture_pipeline.camera_identity``: таблица serial -> sensor_id."""
    del argv
    mapping = discover_serial_to_sensor_id()
    if not mapping:
        print("no zedx cameras discovered", file=sys.stderr)
        return 1
    for serial, sensor_id in sorted(mapping.items(), key=lambda item: item[1]):
        print(f"sensor_id={sensor_id} serial={serial}")
    return 0


def discover_serial_to_sensor_id(
    *, run: Any = subprocess.run
) -> dict[int, int]:
    """{serial: sensor_id} для всех zedx-модулей, видимых прямо сейчас.

    Камера, у которой не удалось определить VI-канал или прочитать серийник,
    просто не попадает в результат — вызывающий код сам решает, что делать с
    камерой, чей serial не нашёлся.
    """
    mapping: dict[int, int] = {}
    devices = _list_video_devices(run)
    channelled: list[tuple[int, VideoDevice]] = []
    for (bus, addr), device in devices.items():
        if device.vi_channel is None:
            logger.warning(
                "camera discovery: no tegra-capture-vi channel for %s-%s (%s)",
                bus,
                addr,
                device.devpath,
            )
            continue
        channelled.append((device.vi_channel, device))
    # Argus addresses only the cameras that actually probed, as 0..N-1 in
    # VI-channel order, so the index is the channel's rank, not its number:
    # a lone camera on vi-channel 1 is opened as sensor-id 0.
    channelled.sort(key=lambda item: item[0])
    for sensor_id, (vi_channel, device) in enumerate(channelled):
        if sensor_id != vi_channel:
            logger.info(
                "camera discovery: %s on vi-channel %d -> sensor_id %d",
                device.devpath,
                vi_channel,
                sensor_id,
            )
        serial = _read_serial(device.devpath, run)
        if serial is None:
            logger.warning(
                "camera discovery: no serial read from %s (sensor_id %d)",
                device.devpath,
                sensor_id,
            )
            continue
        if serial in mapping and mapping[serial] != sensor_id:
            logger.error(
                "camera discovery: serial %s seen on both sensor_id %d and %d",
                serial,
                mapping[serial],
                sensor_id,
            )
            continue
        mapping[serial] = sensor_id
    return mapping


if __name__ == "__main__":
    raise SystemExit(main())
