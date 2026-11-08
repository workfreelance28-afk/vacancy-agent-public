"""Синхронный HH-клиент на `httpx`. Реализует `vacancy_agent.domain.ports.VacancySource`.

Переключаемый источник вакансий (не основной с ADR-013/ADR-014 —
HH закрыл анонимный доступ к поиску; см. `SuperJobClient` в
`superjob_client.py`, основной источник, и `application/wiring.py`,
где источник выбирается через `Settings.vacancy_source`).

Отвечает за запросы, пагинацию, тайм-ауты и повторные попытки (см.
`docs/ARCHITECTURE.md`, разделы «Ответственность компонентов» и
«Надёжность и безопасность»). Не отвечает за оценку вакансий и не
преобразует ответ сам — преобразование делегировано
`vacancy_agent.integrations.hh_normalize.normalize_vacancy`.
"""

import time
from collections.abc import Iterator, Mapping
from typing import Any

import httpx

from vacancy_agent.config.settings import HHSettings
from vacancy_agent.domain.vacancy import Vacancy
from vacancy_agent.integrations.hh_errors import HHApiError, HHClientError, HHNetworkError
from vacancy_agent.integrations.hh_normalize import normalize_vacancy

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class HttpxHHClient:
    """HH-клиент. `transport` позволяет подменить сеть в тестах (`httpx.MockTransport`)."""

    def __init__(self, settings: HHSettings, transport: httpx.BaseTransport | None = None) -> None:
        self._settings = settings
        self._client = httpx.Client(
            base_url=settings.base_url,
            timeout=settings.request_timeout_seconds,
            headers={"User-Agent": settings.user_agent},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpxHHClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def search_vacancies(self, params: Mapping[str, str]) -> Iterator[Vacancy]:
        """Отдаёт вакансии по всем страницам поиска, пока HH их возвращает.

        `page`/`per_page` можно передать в `params` как отправную точку;
        по умолчанию используется `HHSettings.per_page` и страница 0.

        Основной признак конца — пустой `items`: поле `pages` в ответе HH не
        гарантировано (может отсутствовать или быть `null`) и используется
        только как дополнительное ограничение, если оно валидно.
        """

        query = {key: value for key, value in params.items() if key not in {"page", "per_page"}}
        query.setdefault("per_page", str(self._settings.per_page))
        page = int(params.get("page", 0))

        while True:
            payload = self._get_page({**query, "page": str(page)})
            items = payload.get("items") or []
            for item in items:
                yield normalize_vacancy(item)

            if not items:
                break

            total_pages = payload.get("pages")
            page += 1
            if isinstance(total_pages, int) and page >= total_pages:
                break

    def _get_page(self, query: dict[str, str]) -> dict[str, Any]:
        for attempt in range(self._settings.max_retries + 1):
            response: httpx.Response | None = None
            error: HHClientError

            try:
                response = self._client.get("/vacancies", params=query)
            except httpx.TimeoutException as exc:
                error = HHNetworkError(f"Тайм-аут запроса к HH API: {exc}")
            except httpx.HTTPError as exc:
                error = HHNetworkError(f"Ошибка сети при запросе к HH API: {exc}")
            else:
                if response.status_code < 400:
                    try:
                        result: dict[str, Any] = response.json()
                    except ValueError as exc:
                        error = HHNetworkError(f"Не удалось разобрать ответ HH API: {exc}")
                    else:
                        return result
                elif response.status_code not in _RETRYABLE_STATUS_CODES:
                    raise _api_error(response)
                else:
                    error = _api_error(response)

            if attempt == self._settings.max_retries:
                raise error
            self._sleep_before_retry(attempt, response)

        raise AssertionError("недостижимо: цикл всегда возвращает или бросает исключение выше")

    def _sleep_before_retry(self, attempt: int, response: httpx.Response | None) -> None:
        """Ждёт перед повтором. Не короче `Retry-After` от HH, если он указан."""

        delay = self._settings.retry_backoff_seconds * (attempt + 1)
        retry_after = response.headers.get("Retry-After") if response is not None else None
        if retry_after is not None:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass  # HTTP-date формат Retry-After не поддерживаем — используем свою задержку
        time.sleep(delay)


def _api_error(response: httpx.Response) -> HHApiError:
    body: dict[str, Any] = {}
    try:
        parsed = response.json()
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        body = parsed

    return HHApiError(
        response.status_code,
        body.get("request_id"),
        body.get("description") or response.text[:200],
    )
