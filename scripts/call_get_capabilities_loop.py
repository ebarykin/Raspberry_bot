#!/usr/bin/env python3
"""Call /robot/get_capabilities over rosbridge once per second."""

from __future__ import annotations

import asyncio
import json
import time

import websockets

URI = "ws://127.0.0.1:9090"
SERVICE = "/robot/get_capabilities"


async def once() -> str:
    async with websockets.connect(URI) as ws:
        await ws.send(
            json.dumps(
                {
                    "op": "call_service",
                    "id": "1",
                    "service": SERVICE,
                    "args": {},
                }
            )
        )
        return await ws.recv()


async def main() -> None:
    n = 0
    while True:
        n += 1
        started = time.monotonic()
        try:
            raw = await once()
            elapsed_ms = (time.monotonic() - started) * 1000
            print(f"{n:4d}  {elapsed_ms:6.1f} ms  {raw}", flush=True)
        except Exception as exc:
            print(f"{n:4d}  error: {exc}", flush=True)
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
