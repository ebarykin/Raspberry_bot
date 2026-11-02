#!/usr/bin/env python3
"""CLI: push link-quality level to capture-pipeline control WebSocket.

Intent: ручная проверка ABR на борту/ноутбуке без UI
(``python3 scripts/set_profile.py critical``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys


async def _main(uri: str, level: str) -> int:
    """Один request/response к control WS; exit 0 при ok=true."""
    try:
        import websockets
    except ModuleNotFoundError:
        print("Install websockets: python3 -m pip install websockets", file=sys.stderr)
        return 1

    msg = {"event": "link-quality", "payload": {"level": level}}
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps(msg))
        raw = await ws.recv()
    print(raw)
    data = json.loads(raw)
    return 0 if data.get("ok") else 1


def main() -> int:
    """Parse CLI args and run one ABR level push."""
    parser = argparse.ArgumentParser(
        description="Set capture-pipeline ABR via link-quality level (control WS)"
    )
    parser.add_argument(
        "level",
        nargs="?",
        help="link-quality level; must match an abr.profiles key",
    )
    parser.add_argument(
        "--level",
        dest="level_opt",
        help="same as positional level",
    )
    parser.add_argument("--uri", default="ws://127.0.0.1:8591")
    args = parser.parse_args()
    level = args.level_opt or args.level
    if not level:
        parser.error("provide level (e.g. critical) or --level …")
    return asyncio.run(_main(args.uri, level))


if __name__ == "__main__":
    raise SystemExit(main())
