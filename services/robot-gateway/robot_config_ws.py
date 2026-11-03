#!/usr/bin/env python3
"""Publish capabilities.yaml as robot-config on ws://127.0.0.1:4000/.

webrtc-client-go still reads the Last Mile board socket, not rosbridge.
The payload is the same envelope as /robot/get_capabilities.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import struct
from pathlib import Path

import yaml

HOST = "127.0.0.1"
PORT = 4000
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
logger = logging.getLogger("robot_config_ws")


def load_envelope(path: Path) -> str:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("event") != "robot-config":
        raise ValueError(f"{path} must be a robot-config YAML mapping")
    return json.dumps(raw, ensure_ascii=False, separators=(",", ":"))


def _header(request: bytes) -> dict[str, str]:
    text = request.decode("iso-8859-1", errors="replace")
    lines = text.split("\r\n")
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        name, value = line.split(":", 1)
        fields[name.strip().lower()] = value.strip()
    return fields


def _accept_key(key: str) -> str:
    digest = hashlib.sha1((key + WS_MAGIC).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _text_frame(payload: str) -> bytes:
    data = payload.encode("utf-8")
    n = len(data)
    if n < 126:
        return bytes([0x81, n]) + data
    if n < 65536:
        return bytes([0x81, 126]) + struct.pack("!H", n) + data
    return bytes([0x81, 127]) + struct.pack("!Q", n) + data


async def _read_frame(reader: asyncio.StreamReader) -> bytes | None:
    header = await reader.readexactly(2)
    opcode = header[0] & 0x0F
    masked = bool(header[1] & 0x80)
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4) if masked else b""
    data = await reader.readexactly(length)
    if masked:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    if opcode == 0x8:
        return None
    if opcode == 0x9:
        return data
    return data


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, envelope: str) -> None:
    peer = writer.get_extra_info("peername")
    try:
        request = await reader.readuntil(b"\r\n\r\n")
        fields = _header(request)
        key = fields.get("sec-websocket-key")
        if fields.get("upgrade", "").lower() != "websocket" or not key:
            writer.close()
            return
        accept = _accept_key(key)
        writer.write(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n"
                "\r\n"
            ).encode("ascii")
        )
        writer.write(_text_frame(envelope))
        await writer.drain()
        logger.info("sent robot-config to %s", peer)
        while True:
            frame = await _read_frame(reader)
            if frame is None:
                return
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
        logger.info("client %s disconnected", peer)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    path = Path(os.environ.get("CAPABILITIES_FILE", "/config/capabilities.yaml"))
    envelope = load_envelope(path)
    server = await asyncio.start_server(
        lambda r, w: handle(r, w, envelope),
        HOST,
        PORT,
    )
    logger.info("robot-config websocket on ws://%s:%s/ file=%s", HOST, PORT, path)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
