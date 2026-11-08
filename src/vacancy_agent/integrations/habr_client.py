"""Синхронный клиент career.habr.com на `httpx` (TASK-013, ADR-056).
Реализует `vacancy_agent.domain.ports.VacancySource` поверх публичных
HTML-страниц поиска и вакансий — без официального API/OAuth (Habr его не
предоставляет для публичного поиска, см. ADR-056), без cookies/логина,
без Playwright/Selenium и без обхода антибот-защиты (TASK-013,
«Ограничения безопасности»).

Структура — по образцу `hh_client.py`/`superjob_client.py` (тайм-ауты,
ограниченные повторы, `Retry-After`), с двумя добавлениями, специфичными
для этого источника:

1. Обязательная задержка между ПОСЛЕДОВАТЕЛЬНЫМИ запросами
   (`HabrCareerSettings.request_delay_seconds`, минимум 1с) — HH/SuperJob
   как официальные API её не требуют, publичный HTML-каталог без неё
   выглядел бы как автоматизированный обход защиты.
2. `403` и обнаруженная на `200`-ответе CAPTCHA/антибот-страница
   (`habr_normalize.page_looks_blocked`) — не ретраятся: повтор того же
   запроса не изменит исход, а попытка обойти защиту запрещена задачей.

Разбор HTML делегирован `integrations.habr_normalize` — этот модуль
отвечает только за запросы, пагинацию, дедупликацию, лимиты и retry.
"""

import time
from collections.abc import Iterator, Mapping

import httpx

from vacancy_agent.config.settings import HabrCareerSettings
from vacancy_agent.domain.vacancy import Vacancy
from vacancy_agent.integrations.habr_errors import (
    HabrCareerApiError,
    HabrCareerBlockedError,
    HabrCareerClientError,
    HabrCareerConfigError,
    HabrCareerNetworkError,
)
from vacancy_agent.integrations.habr_normalize import (
    HabrListing,
    page_looks_blocked,
    parse_search_results_page,
    parse_vacancy_detail_page,
)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class HabrCareerSource:
    """Habr Career-клиент. `transport` позволяет подменить сеть в тестах
    (`httpx.MockTransport`) — тот же приём, что `HttpxHHClient`/`SuperJobClient`."""

    def __init__(
        self, settings: HabrCareerSettings, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._client = httpx.Client(
            base_url=settings.base_url,
            timeout=settings.request_timeout_seconds,
            headers={"User-Agent": settings.user_agent},
            transport=transport,
        )
        self._made_request = False

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HabrCareerSource":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def search_vacancies(self, params: Mapping[str, str]) -> Iterator[Vacancy]:
        """Отдаёт вакансии со страниц поиска Habr Career, до
        `HabrCareerSettings.max_pages` страниц и
        `HabrCareerSettings.max_vacancies` уникальных вакансий.

        Пустой `q` — фейл-фаст ошибка ДО первого сетевого запроса
        (TASK-013): пустой поиск на career.habr.com отдаёт весь публичный
        каталог, не ноль результатов, а это явно не то, что подразумевает
        отсутствие настроенного `HABR_SEARCH_QUERY`."""

        query = (params.get("q") or "").strip()
        if not query:
            raise HabrCareerConfigError(
                "HABR_SEARCH_QUERY пуст — поиск Habr Career невозможен"
            )

        for listing in self._collect_listings(query):
            html = self._get(listing.url)
            yield parse_vacancy_detail_page(html, listing)

    def _collect_listings(self, query: str) -> list[HabrListing]:
        collected: list[HabrListing] = []
        seen_ids: set[str] = set()

        for page_num in range(1, self._settings.max_pages + 1):
            html = self._get(
                "/vacancies", {"q": query, "type": "all", "page": str(page_num)}
            )
            page_listings = parse_search_results_page(html, self._settings.base_url)
            if not page_listings:
                break

            for listing in page_listings:
                if listing.external_id in seen_ids:
                    continue
                seen_ids.add(listing.external_id)
                collected.append(listing)

            if len(collected) >= self._settings.max_vacancies:
                break

        return collected[: self._settings.max_vacancies]

    def _get(self, url: str, params: Mapping[str, str] | None = None) -> str:
        self._apply_rate_limit()

        for attempt in range(self._settings.max_retries + 1):
            response: httpx.Response | None = None
            error: HabrCareerClientError

            try:
                response = self._client.get(url, params=params)
            except httpx.TimeoutException as exc:
                error = HabrCareerNetworkError(f"Тайм-аут запроса к career.habr.com: {exc}")
            except httpx.HTTPError as exc:
                error = HabrCareerNetworkError(
                    f"Ошибка сети при запросе к career.habr.com: {exc}"
                )
            else:
                if response.status_code == 403:
                    raise HabrCareerBlockedError(
                        "career.habr.com вернул 403 — доступ заблокирован (антибот/защита)"
                    )
                if response.status_code < 400:
                    text = response.text
                    if page_looks_blocked(text):
                        raise HabrCareerBlockedError(
                            "career.habr.com вернул страницу с признаками "
                            "CAPTCHA/антибот-защиты вместо ожидаемого контента"
                        )
                    return text
                if response.status_code not in _RETRYABLE_STATUS_CODES:
                    raise _api_error(response)
                error = _api_error(response)

            if attempt == self._settings.max_retries:
                raise error
            self._sleep_before_retry(attempt, response)

        raise AssertionError("недостижимо: цикл всегда возвращает или бросает исключение выше")

    def _apply_rate_limit(self) -> None:
        """Не короче `request_delay_seconds` между ЛЮБЫМИ двумя
        последовательными запросами этого клиента (TASK-013: «только
        последовательные запросы с настраиваемой задержкой», не меньше
        секунды) — первый запрос отправляется без ожидания."""

        if self._made_request:
            time.sleep(self._settings.request_delay_seconds)
        self._made_request = True

    def _sleep_before_retry(self, attempt: int, response: httpx.Response | None) -> None:
        """Ждёт перед повтором. Не короче `Retry-After` от Habr, если он
        указан (тот же приём, что `HttpxHHClient._sleep_before_retry`,
        TASK-013: «`429` учитывает `Retry-After`»), и не короче
        `request_delay_seconds` (Codex review-fix [P2]: `_apply_rate_limit`
        срабатывает один раз ДО retry-цикла — при типичном
        `retry_backoff_seconds=0.5` повтор после `5xx`/timeout уходил через
        0,5с, хотя TASK-013 требует минимум секунду между ЛЮБЫМИ двумя
        запросами, включая повторы одного и того же)."""

        delay = self._settings.retry_backoff_seconds * (attempt + 1)
        retry_after = response.headers.get("Retry-After") if response is not None else None
        if retry_after is not None:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass  # HTTP-date формат Retry-After не поддерживаем — используем свою задержку
        delay = max(delay, self._settings.request_delay_seconds)
        time.sleep(delay)


def _api_error(response: httpx.Response) -> HabrCareerApiError:
    return HabrCareerApiError(response.status_code, response.text[:200])
