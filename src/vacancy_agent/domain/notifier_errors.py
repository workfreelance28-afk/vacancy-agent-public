"""Ошибки контракта `TelegramNotifier` (см. `domain/ports.py`).

Живут в домене, а не в `integrations/`, по той же причине, что и
`repository_errors.py`: вызывающий код (например, `application/daily_scan.py`)
должен уметь отличить «отправка не удалась, можно повторить позже» от
программной ошибки, не зная, что уведомитель реализован на `aiogram`.
"""


class TelegramNotifierError(Exception):
    """Базовая ошибка Telegram-уведомителя."""


class TelegramSendError(TelegramNotifierError):
    """Не удалось отправить сообщение после исчерпания повторов."""
