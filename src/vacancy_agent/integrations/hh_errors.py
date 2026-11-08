"""Ошибки интеграции с HH API."""

from vacancy_agent.domain.vacancy_source_errors import VacancySourceError


class HHClientError(VacancySourceError):
    """Базовая ошибка HH-клиента и нормализации его ответа. Наследует
    `VacancySourceError` (TASK-015) — combined-режим ловит сбой источника
    этим общим типом, не перечисляя HH/SuperJob/Habr по отдельности."""


class HHNetworkError(HHClientError):
    """Сбой сети или тайм-аут при обращении к HH API."""


class HHApiError(HHClientError):
    """HH API вернул ответ с кодом ошибки (после исчерпания повторов, если применимо)."""

    def __init__(self, status_code: int, request_id: str | None, description: str) -> None:
        self.status_code = status_code
        self.request_id = request_id
        super().__init__(f"HH API вернул {status_code}: {description}")


class HHNormalizationError(HHClientError):
    """Ответ HH не удалось преобразовать во внутреннюю модель `Vacancy`."""
