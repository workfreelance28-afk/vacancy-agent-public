"""Ошибки интеграции с Habr Career (TASK-013). Структура — по образцу
`hh_errors.py`/`superjob_errors.py`, с двумя добавлениями, специфичными
для публичного HTML-источника без официального API (ADR-056):
`HabrCareerBlockedError` (антибот/CAPTCHA/`403`) и `HabrCareerParsingError`
(разметка изменилась и её нельзя разобрать детерминированно) —
`domain/ports.VacancySource` не различает типы ошибок источника, но эти
подклассы дают вызывающему коду (`application/wiring.py`,
`integrations/habr_client.py`) возможность логировать причину точно."""

from vacancy_agent.domain.vacancy_source_errors import VacancySourceError


class HabrCareerClientError(VacancySourceError):
    """Базовая ошибка Habr Career-клиента и разбора его HTML-ответа.
    Наследует `VacancySourceError` (TASK-015) — combined-режим ловит сбой
    источника этим общим типом, не перечисляя HH/SuperJob/Habr по отдельности."""


class HabrCareerNetworkError(HabrCareerClientError):
    """Сбой сети или тайм-аут при обращении к career.habr.com."""


class HabrCareerApiError(HabrCareerClientError):
    """career.habr.com вернул HTTP-ошибку (после исчерпания повторов, если
    применимо) — не `403` (см. `HabrCareerBlockedError`)."""

    def __init__(self, status_code: int, description: str) -> None:
        self.status_code = status_code
        super().__init__(f"career.habr.com вернул {status_code}: {description}")


class HabrCareerBlockedError(HabrCareerClientError):
    """`403` или обнаруженная антибот/CAPTCHA-страница вместо ожидаемого
    контента (TASK-013, «Ограничения безопасности»: без обхода антибот-
    защиты). Не ретраится — повтор того же запроса не изменит исход, а
    попытка его обойти запрещена задачей."""


class HabrCareerParsingError(HabrCareerClientError):
    """Обязательное поле (`external_id`/`title`/`url`/`published_at`)
    отсутствует, или страница не содержит ни одной вакансии и ни явного
    маркера «ничего не найдено» — вероятный признак того, что разметка
    career.habr.com изменилась (TASK-013: «изменение контракта, а не ноль
    вакансий»), а не что источник действительно пуст."""


class HabrCareerConfigError(HabrCareerClientError):
    """Пустой `HABR_SEARCH_QUERY` при выбранном `VACANCY_SOURCE=habr` —
    обнаруживается до первого сетевого запроса (TASK-013: «фейл-фаст
    ошибка до сети, а не загрузка всего каталога»)."""
