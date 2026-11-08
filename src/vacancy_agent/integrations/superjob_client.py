"""Синхронный SuperJob-клиент на `httpx`. Реализует `vacancy_agent.domain.ports.VacancySource`.

Структура и обработка ошибок/повторов — по образцу `hh_client.py`
(тайм-ауты, ограниченные повторы, `docs/ARCHITECTURE.md`, «Надёжность и
безопасность»). Отличия от HH: авторизация заголовком `X-Api-App-Id`
(не query-параметром — ключ не попадёт в URL и, соответственно, не
попадёт в лог `httpx` уровня INFO, который логирует только метод/URL/
статус, без заголовков), и пагинация через булево поле `more` в ответе
(у HH — по пустому `items`).
"""

import time
from collections.abc import Iterator, Mapping
from typing import Any

import httpx

from vacancy_agent.config.settings import SuperJobSettings
from vacancy_agent.domain.vacancy import Vacancy
from vacancy_agent.integrations.superjob_errors import (
    SuperJobApiError,
    SuperJobClientError,
    SuperJobNetworkError,
)
from vacancy_agent.integrations.superjob_normalize import normalize_vacancy

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class SuperJobClient:
    """SuperJob-клиент. `transport` позволяет подменить сеть в тестах (`httpx.MockTransport`)."""

    def __init__(
        self, settings: SuperJobSettings, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._client = httpx.Client(
            base_url=settings.base_url,
            timeout=settings.request_timeout_seconds,
            headers={"X-Api-App-Id": settings.api_key.get_secret_value()},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SuperJobClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def search_vacancies(self, params: Mapping[str, str]) -> Iterator[Vacancy]:
        """Отдаёт вакансии по всем страницам поиска, пока SuperJob их возвращает.

        `page`/`count` можно передать в `params` как отправную точку; по
        умолчанию используется `SuperJobSettings.count` и страница 0.
        Конец пагинации — `more: false` в ответе, либо пустой `objects`
        (на случай, если `more` отсутствует/не булево — тот же защитный
        подход, что у `HttpxHHClient` с полем `pages`).
        """

        query = {key: value for key, value in params.items() if key not in {"page", "count"}}
        query.setdefault("count", str(self._settings.count))
        page = int(params.get("page", 0))

        while True:
            payload = self._get_page({**query, "page": str(page)})
            items = payload.get("objects") or []
            for item in items:
                yield normalize_vacancy(item)

            if not items or payload.get("more") is not True:
                break

            page += 1

    def _get_page(self, query: dict[str, str]) -> dict[str, Any]:
        for attempt in range(self._settings.max_retries + 1):
            response: httpx.Response | None = None
            error: SuperJobClientError

            try:
                response = self._client.get("/vacancies/", params=query)
            except httpx.TimeoutException as exc:
                error = SuperJobNetworkError(f"Тайм-аут запроса к SuperJob API: {exc}")
            except httpx.HTTPError as exc:
                error = SuperJobNetworkError(f"Ошибка сети при запросе к SuperJob API: {exc}")
            else:
                if response.status_code < 400:
                    try:
                        result: dict[str, Any] = response.json()
                    except ValueError as exc:
                        error = SuperJobNetworkError(
                            f"Не удалось разобрать ответ SuperJob API: {exc}"
                        )
                    else:
                        return result
                elif response.status_code not in _RETRYABLE_STATUS_CODES:
                    raise _api_error(response)
                else:
                    error = _api_error(response)

            if attempt == self._settings.max_retries:
                raise error
            self._sleep_before_retry(attempt)

        raise AssertionError("недостижимо: цикл всегда возвращает или бросает исключение выше")

    def _sleep_before_retry(self, attempt: int) -> None:
        time.sleep(self._settings.retry_backoff_seconds * (attempt + 1))


def _api_error(response: httpx.Response) -> SuperJobApiError:
    description = response.text[:200]
    try:
        parsed = response.json()
    except ValueError:
        pass
    else:
        if isinstance(parsed, dict):
            description = str(parsed.get("error", {}).get("message") or description)

    return SuperJobApiError(response.status_code, description)
