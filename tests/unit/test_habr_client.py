import httpx
import pytest

from vacancy_agent.config.settings import HabrCareerSettings
from vacancy_agent.integrations.habr_client import HabrCareerSource
from vacancy_agent.integrations.habr_errors import (
    HabrCareerApiError,
    HabrCareerBlockedError,
    HabrCareerConfigError,
    HabrCareerNetworkError,
)


def _settings(**overrides: object) -> HabrCareerSettings:
    defaults: dict[str, object] = {
        "base_url": "https://career.habr.com",
        "user_agent": "test-agent/1.0",
        "search_query": "AI",
        "max_pages": 2,
        "max_vacancies": 40,
        "request_timeout_seconds": 1.0,
        "request_delay_seconds": 1.0,
        "max_retries": 2,
        "retry_backoff_seconds": 0.001,
    }
    defaults.update(overrides)
    return HabrCareerSettings(**defaults)


def _card(external_id: str) -> str:
    return f"""
    <div class="vacancy-card">
      <a class="vacancy-card__title-link" href="/vacancies/{external_id}">Vacancy {external_id}</a>
      <time datetime="2026-08-20T10:00:00+03:00"></time>
    </div>
    """


def _list_page(cards: list[str], *, no_results: bool = False) -> str:
    if no_results:
        return "<html><body>По вашему запросу ничего не найдено</body></html>"
    return f"<html><body>{''.join(cards)}</body></html>"


def _detail_page() -> str:
    # `.vacancy-company__title` — подтверждённый live smoke-test'ом маркер
    # detail-страницы (TASK-013, повторное ревью [P1]); неподтверждённый
    # `.vacancy-description__text` больше не входит в обязательный маркер.
    return '<html><body><div class="vacancy-company__title">Acme Corp</div></body></html>'


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Каждый функциональный тест здесь совершает минимум два
    последовательных запроса (список + detail), а `request_delay_seconds`
    не может быть меньше 1с (`HabrCareerSettings`, TASK-013) — без
    подмены `time.sleep` весь файл реально ждал бы секунды между запросами.
    `autouse`, в отличие от точечного `monkeypatch` в `test_hh_client.py`:
    там обязательная межзапросная задержка не участвует почти нигде,
    здесь — почти везде."""

    sleeps: list[float] = []
    monkeypatch.setattr(
        "vacancy_agent.integrations.habr_client.time.sleep", lambda seconds: sleeps.append(seconds)
    )
    return sleeps


# --- empty query: fail-fast before any network call ---


def test_search_vacancies_rejects_empty_query_before_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("не должно быть ни одного запроса при пустом search_query")

    with HabrCareerSource(
        _settings(search_query=""), transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(HabrCareerConfigError):
            list(client.search_vacancies({"q": ""}))


def test_search_vacancies_rejects_whitespace_only_query_before_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("не должно быть ни одного запроса при пустом search_query")

    with HabrCareerSource(_settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HabrCareerConfigError):
            list(client.search_vacancies({"q": "   "}))


# --- pagination, dedup, limits ---


def test_search_vacancies_paginates_across_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/vacancies":
            page = request.url.params.get("page")
            if page == "1":
                return httpx.Response(200, text=_list_page([_card("1"), _card("2")]))
            return httpx.Response(200, text=_list_page([_card("3")]))
        return httpx.Response(200, text=_detail_page())

    with HabrCareerSource(_settings(), transport=httpx.MockTransport(handler)) as client:
        vacancies = list(client.search_vacancies({"q": "AI"}))

    assert [v.hh_id for v in vacancies] == ["habr:1", "habr:2", "habr:3"]


def test_search_vacancies_stops_when_a_page_has_no_listings() -> None:
    calls = {"page": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/vacancies":
            page = request.url.params.get("page")
            calls["page"].append(page)
            if page == "1":
                return httpx.Response(200, text=_list_page([_card("1")]))
            return httpx.Response(200, text=_list_page([], no_results=True))
        return httpx.Response(200, text=_detail_page())

    with HabrCareerSource(
        _settings(max_pages=5), transport=httpx.MockTransport(handler)
    ) as client:
        vacancies = list(client.search_vacancies({"q": "AI"}))

    assert [v.hh_id for v in vacancies] == ["habr:1"]
    assert calls["page"] == ["1", "2"]


def test_search_vacancies_respects_max_pages_limit() -> None:
    seen_pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/vacancies":
            page = request.url.params.get("page", "?")
            seen_pages.append(page)
            return httpx.Response(200, text=_list_page([_card(f"{page}-1")]))
        return httpx.Response(200, text=_detail_page())

    with HabrCareerSource(
        _settings(max_pages=2, max_vacancies=100), transport=httpx.MockTransport(handler)
    ) as client:
        list(client.search_vacancies({"q": "AI"}))

    assert seen_pages == ["1", "2"]


def test_search_vacancies_deduplicates_repeated_vacancy_across_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/vacancies":
            page = request.url.params.get("page")
            if page == "1":
                return httpx.Response(200, text=_list_page([_card("1"), _card("2")]))
            return httpx.Response(200, text=_list_page([_card("2"), _card("3")]))
        return httpx.Response(200, text=_detail_page())

    with HabrCareerSource(_settings(), transport=httpx.MockTransport(handler)) as client:
        vacancies = list(client.search_vacancies({"q": "AI"}))

    assert [v.hh_id for v in vacancies] == ["habr:1", "habr:2", "habr:3"]


def test_search_vacancies_respects_max_vacancies_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/vacancies":
            return httpx.Response(200, text=_list_page([_card("1"), _card("2"), _card("3")]))
        return httpx.Response(200, text=_detail_page())

    with HabrCareerSource(
        _settings(max_pages=1, max_vacancies=2), transport=httpx.MockTransport(handler)
    ) as client:
        vacancies = list(client.search_vacancies({"q": "AI"}))

    assert [v.hh_id for v in vacancies] == ["habr:1", "habr:2"]


def test_search_vacancies_sends_configured_user_agent() -> None:
    seen_headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/vacancies":
            seen_headers.append(request.headers.get("user-agent"))
            return httpx.Response(200, text=_list_page([], no_results=True))
        return httpx.Response(200, text=_detail_page())

    with HabrCareerSource(_settings(), transport=httpx.MockTransport(handler)) as client:
        list(client.search_vacancies({"q": "AI"}))

    assert seen_headers[0] == "test-agent/1.0"


def test_search_vacancies_sends_q_and_type_all_query_params() -> None:
    seen_params: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/vacancies":
            seen_params.append(dict(request.url.params))
            return httpx.Response(200, text=_list_page([], no_results=True))
        return httpx.Response(200, text=_detail_page())

    with HabrCareerSource(
        _settings(search_query="AI Integrator"), transport=httpx.MockTransport(handler)
    ) as client:
        list(client.search_vacancies({"q": "AI Integrator"}))

    assert seen_params[0]["q"] == "AI Integrator"
    assert seen_params[0]["type"] == "all"


# --- rate-limit delay ---


def test_search_vacancies_applies_delay_between_requests_not_before_the_first(
    _no_real_sleep: list[float],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/vacancies":
            return httpx.Response(200, text=_list_page([_card("1")], no_results=False))
        return httpx.Response(200, text=_detail_page())

    with HabrCareerSource(
        _settings(max_pages=1, request_delay_seconds=1.5), transport=httpx.MockTransport(handler)
    ) as client:
        list(client.search_vacancies({"q": "AI"}))

    # Два запроса всего (одна страница списка + одна detail-страница) —
    # ровно одна задержка МЕЖДУ ними, не перед первым запросом.
    assert _no_real_sleep == [1.5]


def test_search_vacancies_delays_between_every_pair_of_requests(
    _no_real_sleep: list[float],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/vacancies":
            page = request.url.params.get("page")
            if page == "1":
                return httpx.Response(200, text=_list_page([_card("1")]))
            return httpx.Response(200, text=_list_page([], no_results=True))
        return httpx.Response(200, text=_detail_page())

    with HabrCareerSource(
        _settings(max_pages=5, request_delay_seconds=1.0), transport=httpx.MockTransport(handler)
    ) as client:
        list(client.search_vacancies({"q": "AI"}))

    # 3 запроса (страница 1, страница 2 — пустая, detail вакансии 1) → 2 задержки.
    assert _no_real_sleep == [1.0, 1.0]


# --- 403 / CAPTCHA: blocked, no retry ---


def test_search_vacancies_raises_blocked_error_on_403_without_retry() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(403, text="Forbidden")

    with HabrCareerSource(
        _settings(max_retries=2), transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(HabrCareerBlockedError):
            list(client.search_vacancies({"q": "AI"}))

    assert attempts["count"] == 1


def test_search_vacancies_raises_blocked_error_on_captcha_content_without_retry() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(200, text="<html><body>Подтвердите, что вы не робот</body></html>")

    with HabrCareerSource(
        _settings(max_retries=2), transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(HabrCareerBlockedError):
            list(client.search_vacancies({"q": "AI"}))

    assert attempts["count"] == 1


# --- retry: 5xx, timeout, Retry-After ---


def test_search_vacancies_retries_on_5xx_then_succeeds() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/vacancies":
            attempts["count"] += 1
            if attempts["count"] < 2:
                return httpx.Response(503, text="unavailable")
            return httpx.Response(200, text=_list_page([_card("1")]))
        return httpx.Response(200, text=_detail_page())

    with HabrCareerSource(
        _settings(max_pages=1), transport=httpx.MockTransport(handler)
    ) as client:
        vacancies = list(client.search_vacancies({"q": "AI"}))

    assert attempts["count"] == 2
    assert [v.hh_id for v in vacancies] == ["habr:1"]


def test_search_vacancies_raises_api_error_after_exhausting_retries_on_5xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with HabrCareerSource(
        _settings(max_retries=1), transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(HabrCareerApiError) as exc_info:
            list(client.search_vacancies({"q": "AI"}))

    assert exc_info.value.status_code == 500


def test_search_vacancies_raises_api_error_without_retry_on_non_retryable_status() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(404, text="not found")

    with HabrCareerSource(
        _settings(max_retries=2), transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(HabrCareerApiError) as exc_info:
            list(client.search_vacancies({"q": "AI"}))

    assert attempts["count"] == 1
    assert exc_info.value.status_code == 404


def test_search_vacancies_raises_network_error_after_exhausting_retries_on_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated timeout", request=request)

    with HabrCareerSource(
        _settings(max_retries=1), transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(HabrCareerNetworkError):
            list(client.search_vacancies({"q": "AI"}))


def test_retry_delay_is_never_shorter_than_request_delay_seconds(
    _no_real_sleep: list[float],
) -> None:
    """Codex review-fix [P2]: `_apply_rate_limit` срабатывает один раз ДО
    retry-цикла — при типичном `retry_backoff_seconds=0.5` повтор после
    `5xx`/timeout уходил через 0,5с, хотя TASK-013 требует минимум секунду
    между ЛЮБЫМИ двумя запросами, включая повторы одного и того же."""

    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/vacancies":
            attempts["count"] += 1
            if attempts["count"] < 2:
                return httpx.Response(503, text="unavailable")
            return httpx.Response(200, text=_list_page([], no_results=True))
        return httpx.Response(200, text=_detail_page())

    with HabrCareerSource(
        _settings(max_pages=1, request_delay_seconds=2.0, retry_backoff_seconds=0.001),
        transport=httpx.MockTransport(handler),
    ) as client:
        list(client.search_vacancies({"q": "AI"}))

    assert attempts["count"] == 2
    assert all(delay >= 2.0 for delay in _no_real_sleep)


def test_retry_waits_at_least_as_long_as_retry_after_header(
    _no_real_sleep: list[float],
) -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/vacancies":
            attempts["count"] += 1
            if attempts["count"] < 2:
                return httpx.Response(429, headers={"Retry-After": "5"}, text="slow down")
            return httpx.Response(200, text=_list_page([], no_results=True))
        return httpx.Response(200, text=_detail_page())

    with HabrCareerSource(
        _settings(max_pages=1, retry_backoff_seconds=0.001),
        transport=httpx.MockTransport(handler),
    ) as client:
        list(client.search_vacancies({"q": "AI"}))

    assert 5.0 in _no_real_sleep


# --- resource lifecycle ---


def test_close_is_safe_to_call_and_idempotent() -> None:
    client = HabrCareerSource(_settings())
    client.close()
    client.close()
