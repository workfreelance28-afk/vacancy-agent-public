"""Ошибки контракта `VacancyRepository` (см. `domain/ports.py`).

Живут в домене, а не в `persistence/`: любая реализация `VacancyRepository`
(сейчас — SQLite, потенциально другая позже) обязана поднимать именно эти
типы, чтобы вызывающий код (например, `integrations/telegram_bot.py`)
мог обрабатывать ошибки хранилища, не зная о конкретной СУБД.
"""


class PersistenceError(Exception):
    """Базовая ошибка репозитория вакансий."""


class VacancyNotFoundError(PersistenceError):
    """Операция запрошена для `hh_id`, которого нет в хранилище."""
