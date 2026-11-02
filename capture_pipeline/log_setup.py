"""Python-логи и GST_DEBUG. Вызывать до Gst.init, иначе фильтры Argus/RTSP не встанут."""

from __future__ import annotations

import logging
import os

from .config import Config, LoggingConfig


class _HandshakeClosedFilter(logging.Filter):
    """Обрыв рукопожатия — одна строка WARNING, без трейсбека."""

    def filter(self, record: logging.LogRecord) -> bool:
        if "opening handshake failed" not in record.getMessage():
            return True
        exc_name = "client closed"
        if record.exc_info and record.exc_info[0] is not None:
            exc_name = record.exc_info[0].__name__
        record.msg = "control websocket handshake closed: %s"
        record.args = (exc_name,)
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        record.levelno = logging.WARNING
        record.levelname = "WARNING"
        return True


def configure_logging(cfg: Config) -> None:
    """Уровень Python-лога и GST_DEBUG из YAML. Один раз, до ``_import_gst()``."""
    apply_gst_debug(cfg.logging)
    level = getattr(logging, cfg.logging.level, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        force=True,
    )
    # websockets логирует connection open/closed на INFO для каждого запроса;
    # k8s-пробы ходят в status каждые 2с, так что это чистый шум.
    # Фильтр на handler, а не на логгере websockets: запись создаёт дочерний
    # websockets.server, и фильтр родителя до неё не доходит.
    logging.getLogger("websockets").setLevel(max(level, logging.WARNING))
    for handler in logging.getLogger().handlers:
        if any(isinstance(item, _HandshakeClosedFilter) for item in handler.filters):
            continue
        handler.addFilter(_HandshakeClosedFilter())


def apply_gst_debug(logging_cfg: LoggingConfig) -> None:
    """Записать GST_DEBUG в environ. Пустая строка не трогает уже заданный фильтр."""
    if logging_cfg.gst_debug:
        os.environ["GST_DEBUG"] = logging_cfg.gst_debug
    os.environ.setdefault("GST_DEBUG_NO_COLOR", "1")
