"""Ошибки интеграции с SuperJob API. Структура — по образцу `hh_errors.py`."""

from vacancy_agent.domain.vacancy_source_errors import VacancySourceError


class SuperJobClientError(VacancySourceError):
    """Базовая ошибка SuperJob-клиента и нормализации его ответа. Наследует
    `VacancySourceError` (TASK-015) — combined-режим ловит сбой источника
    этим общим типом, не перечисляя HH/SuperJob/Habr по отдельности."""


class SuperJobNetworkError(SuperJobClientError):
    """Сбой сети или тайм-аут при обращении к SuperJob API."""


class SuperJobApiError(SuperJobClientError):
    """SuperJob API вернул ответ с кодом ошибки (после исчерпания повторов, если применимо)."""

    def __init__(self, status_code: int, description: str) -> None:
        self.status_code = status_code
        super().__init__(f"SuperJob API вернул {status_code}: {description}")


class SuperJobNormalizationError(SuperJobClientError):
    """Ответ SuperJob не удалось преобразовать во внутреннюю модель `Vacancy`."""
