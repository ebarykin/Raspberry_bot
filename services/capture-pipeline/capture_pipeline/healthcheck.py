"""Probe k8s: local control WS и состояние камер (liveness / readiness)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any


def status_is_healthy(payload: Any, *, require_playing: bool) -> bool:
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return False
    cameras = payload.get("cameras")
    if not isinstance(cameras, list) or not cameras:
        return False
    if not all(isinstance(camera, dict) for camera in cameras):
        return False
    if not require_playing:
        return True
    # Хотя бы одна: требование "все в playing" делало startupProbe
    # самоподдерживающимся циклом — одна залипшая камера убивала контейнер
    # каждые failureThreshold×periodSeconds, что её не лечило, зато гоняло
    # Argus по кругу. Сломанные камеры видны в status как error, а случай
    # "не поднялась ни одна" ловится при старте через exit(1).
    return any(camera.get("state") == "playing" for camera in cameras)


async def _query(url: str, timeout_s: float) -> Any:
    import websockets  # type: ignore[import-untyped]

    async with websockets.connect(url, open_timeout=timeout_s) as websocket:
        await websocket.send('{"event":"status"}')
        raw = await asyncio.wait_for(websocket.recv(), timeout=timeout_s)
    return json.loads(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="capture-pipeline health probe")
    parser.add_argument("--url", default="ws://127.0.0.1:8591")
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--require-playing", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = asyncio.run(_query(args.url, max(0.1, args.timeout)))
    except Exception as exc:  # noqa: BLE001
        print(f"healthcheck failed: {exc}", file=sys.stderr)
        return 1
    if not status_is_healthy(payload, require_playing=args.require_playing):
        print(f"unhealthy status: {payload}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
