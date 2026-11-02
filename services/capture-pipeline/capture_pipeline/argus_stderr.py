"""Слушать stdout/stderr процесса и поймать отказ camera provider.

NVIDIA печатает ``Cannot create camera provider`` и ``Failed to create
CameraProvider`` в обход GStreamer bus, поэтому слот видит только EOS.
Тот же поток несёт баннеры NVENC, каталог режимов ``GST_ARGUS`` и предупреждения
``gst-plugin-scanner``: их в лог не пишем. Строки отказов Argus пропускаем.
"""

from __future__ import annotations

import os
import re
import threading
from typing import Callable

from .pipeline_recovery import is_argus_provider_failure

FatalFn = Callable[[str], None]

# Строки, которые NVIDIA и gst-plugin-scanner печатают на каждый старт камеры.
# На диагноз не влияют. Отказы Argus в этот список не входят.
_NOISE_SUBSTRINGS = (
    "Failed to load plugin",
    "gstcuvidloader",
    "libnvcuvid",
    "gstsfelement",
    "Opening in BLOCKING MODE",
    "NvMMLiteOpen",
    "NvMMLiteBlockCreate",
    "===== NvVideo",
    "NvVideo:",
    "H264:",
    "NVMEDIA:",
    "not multicast-capable",
)
_ARGUS_KEEP = ("cleaning", "error", "fail", "timeout", "invalid")
_SETTING_LINE = re.compile(
    r"^\s+(Camera |Output Stream|seconds to Run|Frame Rate|Exposure|Gain Range|Sensor Mode|Analog Gain)"
)


def stdout_line_is_noise(line: str) -> bool:
    """Баннер NVIDIA / отсутствующий опциональный плагин, не отказ камеры."""
    text = line.strip()
    if not text:
        return True
    if text.startswith("GST_ARGUS:"):
        lowered = text.lower()
        return not any(token in lowered for token in _ARGUS_KEEP)
    if text.startswith("CONSUMER:"):
        return True
    if any(piece in text for piece in _NOISE_SUBSTRINGS):
        return True
    return _SETTING_LINE.match(line) is not None


class ArgusStderrWatch:
    """Склеивает fd 1 и fd 2 и пишет в исходный stdout только полезные строки."""

    def __init__(self) -> None:
        self.fatal: str | None = None
        self._on_fatal: FatalFn | None = None
        self._saved_out: int | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        saved_out = os.dup(1)
        saved_err = os.dup(2)
        read_fd, write_fd = os.pipe()
        os.dup2(write_fd, 1)
        os.dup2(write_fd, 2)
        os.close(write_fd)
        self._saved_out = saved_out
        # Исходный stderr больше не нужен: kubectl и так собирает stdout.
        os.close(saved_err)
        self._thread = threading.Thread(
            target=self._pump,
            args=(read_fd,),
            name="argus-stderr",
            daemon=True,
        )
        self._thread.start()

    def arm(self, on_fatal: FatalFn) -> None:
        """Вызвать ``on_fatal`` на первой строке провайдера. Повторно — после ``acknowledge``."""
        self._on_fatal = on_fatal
        if self.fatal is not None:
            on_fatal(self.fatal)

    def acknowledge(self) -> None:
        """Сбросить флаг, чтобы следующий отказ демона снова поймался."""
        self.fatal = None

    def _pump(self, read_fd: int) -> None:
        pending = b""
        try:
            while True:
                chunk = os.read(read_fd, 4096)
                if not chunk:
                    break
                pending += chunk
                while b"\n" in pending:
                    raw, pending = pending.split(b"\n", 1)
                    self._handle_line(raw.decode("utf-8", "replace"))
        except OSError:
            return
        finally:
            os.close(read_fd)

    def _handle_line(self, line: str) -> None:
        self._note(line)
        if stdout_line_is_noise(line):
            return
        saved = self._saved_out
        if saved is None:
            return
        os.write(saved, (line.rstrip("\r") + "\n").encode("utf-8", "replace"))

    def _note(self, line: str) -> None:
        if self.fatal is not None or not is_argus_provider_failure(line):
            return
        self.fatal = line.strip()
        callback = self._on_fatal
        if callback is not None:
            callback(self.fatal)
