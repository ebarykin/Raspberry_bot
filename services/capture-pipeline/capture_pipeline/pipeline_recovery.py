"""Параметры и функции восстановления capture-пайплайна."""

from __future__ import annotations

PLAYING_TIMEOUT_MS = 25_000
# Короткий вспышка PLAYING перед EOS — не здоровье. Backoff не сбрасываем,
# пока пайплайн не продержится STABLE_PLAYING_MS.
STABLE_PLAYING_MS = 10_000


def is_ignorable_argus_bus_error(detail: str) -> bool:
    """Нефатальный шум nvarguscamerasrc (AE / setup) на L4T.

    На старте и при EOS элемент пишет ``Argus Correctable Error Status``,
    хотя capture доходит до PLAYING. Считать это падением слота — цикл
    рестартов. Настоящий отказ Argus приходит с другой debug-строкой.
    """
    text = (detail or "").lower()
    return "argus correctable error" in text


def is_argus_provider_failure(detail: str) -> bool:
    """Демон Argus не отдал camera provider.

    Повтор ``nvarguscamerasrc`` этого не чинит: клиент не открыл RPC-сокет
    ``/tmp/argus_socket``. Нужен перезапуск ``nvargus-daemon``, а не backoff слота.
    """
    text = (detail or "").lower()
    return (
        "cannot create camera provider" in text
        or "failed to create cameraprovider" in text
        or "failed to create camera provider" in text
    )


def next_backoff_ms(
    attempt: int,
    *,
    base_ms: int,
    max_ms: int,
) -> int:
    """Экспоненциальный backoff, потолок ``max_ms`` (попытка с 1).

    attempt=1 → base, дальше ×2. Без jitter: логи предсказуемы, старт
    нескольких камер уже разводит ``stagger_ms``.
    """
    if attempt <= 1:
        return max(0, base_ms)
    delay = base_ms * (2 ** (attempt - 1))
    return min(max_ms, delay)
