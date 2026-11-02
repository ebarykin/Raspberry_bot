#!/usr/bin/env python3
"""Lab-only: tc/netem на uplink.

Режет UDP egress (как WebRTC/RTP). TCP (SSH, preview, control WS) остаётся
на default prio. Нужны ``tc`` и CAP_NET_ADMIN. Не часть production control WS.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any, Literal


# Только whitelist пресетов: свободный tc из UI не принимаем.
NETEM_PRESETS: dict[str, dict[str, Any] | None] = {
    "clear": None,
    "lte-1m": {
        "delay_ms": 100,
        "jitter_ms": 30,
        "loss_pct": 3.0,
        "rate": "1mbit",
        "hint": "100ms ±30 · 3% · 1 Mbit",
    },
    "lte-1p5m": {
        "delay_ms": 90,
        "jitter_ms": 25,
        "loss_pct": 2.5,
        "rate": "1.5mbit",
        "hint": "90ms ±25 · 2.5% · 1.5 Mbit",
    },
    "lte-bad": {
        "delay_ms": 120,
        "jitter_ms": 40,
        "loss_pct": 2.0,
        "rate": "2mbit",
        "hint": "120ms ±40 · 2% · 2 Mbit",
    },
    "sat": {
        "delay_ms": 550,
        "jitter_ms": 50,
        "loss_pct": 1.0,
        "rate": "3mbit",
        "hint": "550ms ±50 · 1% · 3 Mbit",
    },
    "lossy": {
        "delay_ms": 60,
        "jitter_ms": 20,
        "loss_pct": 8.0,
        "rate": "4mbit",
        "hint": "60ms ±20 · 8% · 4 Mbit",
    },
    "lte-ok": {
        "delay_ms": 40,
        "jitter_ms": 10,
        "loss_pct": 0.1,
        "rate": "8mbit",
        "hint": "40ms ±10 · 0.1% · 8 Mbit",
    },
}

# udp: all IPv4 UDP egress (teleop uplink). udp-sports: only listed UDP sports.
NetemMatch = Literal["udp", "udp-sports"]


def detect_default_iface() -> str:
    """Best-effort default-route iface; fall back to lo."""
    if shutil.which("ip") is None:
        return "lo"
    try:
        out = subprocess.check_output(
            ["ip", "-o", "route", "show", "default"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return "lo"
    parts = out.split()
    if "dev" in parts:
        i = parts.index("dev")
        if i + 1 < len(parts):
            return parts[i + 1]
    return "lo"


def choose_netem_iface(rtsp_host: str, explicit: str | None = None) -> str:
    """Iface for lab netem (egress UDP shaping).

    Loopback RTSP host → ``lo``. Remote host → default-route iface.
    Override with ``--netem-iface`` for Wi‑Fi/LTE uplink tests.
    """
    if explicit:
        return explicit
    host = (rtsp_host or "").strip().lower().strip("[]")
    if host in ("127.0.0.1", "localhost", "::1") or host.startswith("127."):
        return "lo"
    return detect_default_iface()


def _run_once(cmd: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    err = (proc.stderr or proc.stdout or "").strip()
    return proc.returncode, err


def _run(cmd: list[str]) -> tuple[int, str]:
    """Run tc; on EPERM retry once with ``sudo -n`` (passwordless)."""
    code, err = _run_once(cmd)
    if code == 0:
        return 0, err
    low = err.lower()
    if "operation not permitted" not in low and "permission denied" not in low:
        return code, err
    if shutil.which("sudo") is None:
        return code, err
    code2, err2 = _run_once(["sudo", "-n", *cmd])
    if code2 == 0:
        return 0, err2
    hint = (
        "need CAP_NET_ADMIN — restart preview with sudo "
        "(make preview-emulate-sudo) or allow passwordless: "
        "sudo -n tc"
    )
    return code, f"{err} ({hint})"


def can_elevate_tc() -> bool:
    """True if bare tc works or ``sudo -n tc`` is allowed."""
    if shutil.which("tc") is None:
        return False
    code, err = _run_once(["tc", "qdisc", "show", "dev", "lo"])
    if code == 0:
        return True
    low = err.lower()
    if "operation not permitted" not in low and "permission denied" not in low:
        return False
    if shutil.which("sudo") is None:
        return False
    code2, _err2 = _run_once(["sudo", "-n", "tc", "qdisc", "show", "dev", "lo"])
    return code2 == 0


def _is_benign_clear_error(err: str) -> bool:
    """True when ``tc qdisc del … root`` failed only because nothing is configured."""
    low = (err or "").lower()
    return any(
        s in low
        for s in (
            "no such file",
            "cannot find",
            "handle of zero",
            "invalid handle",
            "does not exist",
        )
    )


def _rate_to_kbps(rate: str) -> int | None:
    """Parse tc-style rate (``4mbit``, ``800kbit``) → integer kbps."""
    s = (rate or "").strip().lower().replace(" ", "")
    if not s:
        return None
    try:
        if s.endswith("gbit"):
            return int(float(s[:-4]) * 1_000_000)
        if s.endswith("mbit"):
            return int(float(s[:-4]) * 1000)
        if s.endswith("kbit"):
            return int(float(s[:-4]))
        if s.endswith("bit"):
            return max(1, int(float(s[:-3]) / 1000))
    except ValueError:
        return None
    return None


def _preset_rate_kbps(preset: str) -> int | None:
    spec = NETEM_PRESETS.get(preset)
    if not spec:
        return None
    return _rate_to_kbps(str(spec.get("rate") or ""))


def presets_for_ui() -> list[dict[str, str]]:
    """Presets for UI: clear first, then ascending by tc rate (bitrate)."""
    out: list[dict[str, str]] = [
        {"id": "clear", "hint": "сброс qdisc"},
    ]
    ranked = sorted(
        (
            (key, spec)
            for key, spec in NETEM_PRESETS.items()
            if key != "clear" and spec is not None
        ),
        key=lambda item: _rate_to_kbps(str(item[1].get("rate") or "")) or 0,
    )
    for key, spec in ranked:
        out.append({"id": key, "hint": str(spec.get("hint") or key)})
    return out


@dataclass
class NetemController:
    """Thread-safe lab netem applicator (prio + UDP → netem band)."""

    iface: str
    ports: tuple[int, ...] = ()
    # udp = all UDP egress (WebRTC-like). udp-sports = only UDP source ports.
    match: NetemMatch = "udp"
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _preset: str = "clear"
    _last_error: str | None = None
    _active: bool = False

    def status(self) -> dict[str, Any]:
        elevated = can_elevate_tc()
        with self._lock:
            return self._status_unlocked(elevated=elevated)

    def _status_unlocked(self, *, elevated: bool | None = None) -> dict[str, Any]:
        if elevated is None:
            elevated = can_elevate_tc()
        return {
            "ok": True,
            "available": shutil.which("tc") is not None,
            "elevated": elevated,
            "iface": self.iface,
            "match": self.match,
            "ports": list(self.ports) if self.match == "udp-sports" else [],
            "preset": self._preset,
            "active": self._active,
            "rate_kbps": (
                _preset_rate_kbps(self._preset) if self._active else None
            ),
            "error": self._last_error,
            "hint": (
                "UDP egress only · TCP (SSH/HTTP/control) not shaped"
                if self.match == "udp"
                else "UDP sport filter · TCP not shaped"
            ),
            "presets": presets_for_ui(),
        }

    def apply(self, preset: str) -> dict[str, Any]:
        key = str(preset or "").strip().lower()
        if key not in NETEM_PRESETS:
            return {
                "ok": False,
                "error": f"unknown netem preset: {preset!r}",
                "preset": self._preset,
            }
        if shutil.which("tc") is None:
            return {
                "ok": False,
                "error": "tc not found in PATH (install iproute2)",
                "preset": self._preset,
            }
        if self.match == "udp-sports" and not self.ports:
            return {
                "ok": False,
                "error": "udp-sports match requires ports",
                "preset": self._preset,
            }

        with self._lock:
            if key == "clear":
                code, err = self._clear_unlocked()
                if code != 0 and err and not _is_benign_clear_error(err):
                    self._last_error = err
                    return {
                        "ok": False,
                        "error": err,
                        "preset": self._preset,
                    }
                self._preset = "clear"
                self._active = False
                self._last_error = None
                return self._status_unlocked(elevated=True) | {
                    "ok": True,
                    "applied": "clear",
                }

            self._clear_unlocked()
            spec = NETEM_PRESETS[key]
            assert spec is not None
            ok, err = self._apply_unlocked(spec)
            if not ok:
                self._clear_unlocked()
                self._preset = "clear"
                self._active = False
                self._last_error = err or "netem apply failed"
                return {
                    "ok": False,
                    "error": self._last_error,
                    "preset": self._preset,
                }
            self._preset = key
            self._active = True
            self._last_error = None
            return self._status_unlocked(elevated=True) | {
                "ok": True,
                "applied": key,
            }

    def _clear_unlocked(self) -> tuple[int, str]:
        return _run(["tc", "qdisc", "del", "dev", self.iface, "root"])

    def _apply_unlocked(self, spec: dict[str, Any]) -> tuple[bool, str]:
        """prio root: default bands untouched; band 1:3 = netem for UDP only."""
        delay = int(spec["delay_ms"])
        jitter = int(spec["jitter_ms"])
        loss = float(spec["loss_pct"])
        rate = str(spec["rate"])

        code, err = _run(
            [
                "tc",
                "qdisc",
                "add",
                "dev",
                self.iface,
                "root",
                "handle",
                "1:",
                "prio",
                "bands",
                "3",
            ]
        )
        if code != 0:
            return False, err or "tc qdisc add prio failed"

        netem = [
            "tc",
            "qdisc",
            "add",
            "dev",
            self.iface,
            "parent",
            "1:3",
            "handle",
            "30:",
            "netem",
            "delay",
            f"{delay}ms",
            f"{jitter}ms",
            "loss",
            f"{loss}%",
            "rate",
            rate,
        ]
        code, err = _run(netem)
        if code != 0:
            netem_no_rate = netem[:-2]
            code, err = _run(netem_no_rate)
            if code != 0:
                return False, err or "tc qdisc add netem failed"

        if self.match == "udp":
            code, err = _run(
                [
                    "tc",
                    "filter",
                    "add",
                    "dev",
                    self.iface,
                    "protocol",
                    "ip",
                    "parent",
                    "1:",
                    "prio",
                    "1",
                    "u32",
                    "match",
                    "ip",
                    "protocol",
                    "17",
                    "0xff",
                    "flowid",
                    "1:3",
                ]
            )
            if code != 0:
                return False, err or "tc filter UDP failed"
            return True, ""

        for port in self.ports:
            code, err = _run(
                [
                    "tc",
                    "filter",
                    "add",
                    "dev",
                    self.iface,
                    "protocol",
                    "ip",
                    "parent",
                    "1:",
                    "prio",
                    "1",
                    "u32",
                    "match",
                    "ip",
                    "protocol",
                    "17",
                    "0xff",
                    "match",
                    "udp",
                    "sport",
                    str(port),
                    "0xffff",
                    "flowid",
                    "1:3",
                ]
            )
            if code != 0:
                return False, err or f"tc filter UDP sport {port} failed"
        return True, ""
