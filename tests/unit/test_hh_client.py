import httpx
import pytest

from vacancy_agent.config.settings import HHSettings
from vacancy_agent.integrations.hh_client import HttpxHHClient
from vacancy_agent.integrations.hh_errors import HHApiError, HHNetworkError


def _settings(**overrides: object) -> HHSettings:
    defaults: dict[str, object] = {
        "base_url": "https://api.hh.ru",
        "user_agent": "test-agent/1.0",
        "request_timeout_seconds": 1.0,
        "per_page": 2,
        "max_retries": 2,
        "retry_backoff_seconds": 0.001,
    }
    defaults.update(overrides)
    return HHSettings(**defaults)


def _item(id_: str) -> dict[str, object]:
    return {
        "id": id_,
        "name": f"Vacancy {id_}",
        "alternate_url": f"https://hh.ru/vacancy/{id_}",
        "published_at": "2026-08-20T10:00:00+0300",
    }


def test_search_vacancies_paginates_until_last_page() -> None:
    pages = {
        "0": {"items": [_item("1"), _item("2")], "pages": 2, "page": 0},
        "1": {"items": [_item("3")], "pages": 2, "page": 1},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        return httpx.Response(200, json=pages[page])

    with HttpxHHClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        vacancies = list(client.search_vacancies({"text": "python"}))

    assert [v.hh_id for v in vacancies] == ["1", "2", "3"]


def test_search_vacancies_passes_query_params_and_default_per_page() -> None:
    seen_params: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.append(dict(request.url.params))
        return httpx.Response(200, json={"items": [], "pages": 1, "page": 0})

    with HttpxHHClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        list(client.search_vacancies({"text": "python", "area": "1"}))

    assert seen_params[0]["text"] == "python"
    assert seen_params[0]["area"] == "1"
    assert seen_params[0]["per_page"] == "2"


def test_search_vacancies_stops_on_empty_items_even_if_pages_says_more() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [], "pages": 5, "page": 0})

    with HttpxHHClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        vacancies = list(client.search_vacancies({}))

    assert vacancies == []


def test_get_page_retries_on_5xx_then_succeeds() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 2:
            return httpx.Response(503, json={"description": "temporarily unavailable"})
        return httpx.Response(200, json={"items": [_item("1")], "pages": 1, "page": 0})

    with HttpxHHClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        vacancies = list(client.search_vacancies({}))

    assert attempts["count"] == 2
    assert [v.hh_id for v in vacancies] == ["1"]


def test_raises_hh_api_error_without_retry_on_client_error() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(400, json={"description": "bad request", "request_id": "abc"})

    with HttpxHHClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HHApiError) as exc_info:
            list(client.search_vacancies({}))

    assert attempts["count"] == 1
    assert exc_info.value.status_code == 400
    assert exc_info.value.request_id == "abc"


def test_raises_hh_api_error_after_exhausting_retries_on_5xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"description": "boom"})

    settings = _settings(max_retries=1)
    with HttpxHHClient(settings, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HHApiError):
            list(client.search_vacancies({}))


def test_raises_hh_network_error_after_exhausting_retries_on_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated timeout", request=request)

    settings = _settings(max_retries=1)
    with HttpxHHClient(settings, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HHNetworkError):
            list(client.search_vacancies({}))


def test_search_vacancies_does_not_crash_on_null_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        if page == "0":
            return httpx.Response(200, json={"items": [_item("1")], "pages": None})
        return httpx.Response(200, json={"items": [], "pages": None})

    with HttpxHHClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        vacancies = list(client.search_vacancies({}))

    assert [v.hh_id for v in vacancies] == ["1"]


def test_search_vacancies_keeps_fetching_when_pages_is_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        if page == "0":
            return httpx.Response(200, json={"items": [_item("1")]})
        return httpx.Response(200, json={"items": []})

    with HttpxHHClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        vacancies = list(client.search_vacancies({}))

    assert [v.hh_id for v in vacancies] == ["1"]


def test_get_page_retries_on_malformed_json_body_then_succeeds() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 2:
            return httpx.Response(200, content=b"not valid json{{{")
        return httpx.Response(200, json={"items": [_item("1")], "pages": 1})

    with HttpxHHClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        vacancies = list(client.search_vacancies({}))

    assert attempts["count"] == 2
    assert [v.hh_id for v in vacancies] == ["1"]


def test_get_page_raises_hh_network_error_after_exhausting_retries_on_malformed_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not valid json{{{")

    settings = _settings(max_retries=1)
    with HttpxHHClient(settings, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HHNetworkError):
            list(client.search_vacancies({}))


def test_retry_waits_at_least_as_long_as_retry_after_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(
        "vacancy_agent.integrations.hh_client.time.sleep", lambda seconds: sleeps.append(seconds)
    )

    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 2:
            return httpx.Response(
                429, headers={"Retry-After": "5"}, json={"description": "slow down"}
            )
        return httpx.Response(200, json={"items": [_item("1")], "pages": 1})

    settings = _settings(retry_backoff_seconds=0.001)
    with HttpxHHClient(settings, transport=httpx.MockTransport(handler)) as client:
        vacancies = list(client.search_vacancies({}))

    assert [v.hh_id for v in vacancies] == ["1"]
    assert sleeps == [5.0]


def test_retry_after_header_does_not_shorten_computed_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(
        "vacancy_agent.integrations.hh_client.time.sleep", lambda seconds: sleeps.append(seconds)
    )

    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 2:
            return httpx.Response(
                429, headers={"Retry-After": "0"}, json={"description": "slow down"}
            )
        return httpx.Response(200, json={"items": [_item("1")], "pages": 1})

    settings = _settings(retry_backoff_seconds=10.0)
    with HttpxHHClient(settings, transport=httpx.MockTransport(handler)) as client:
        list(client.search_vacancies({}))

    assert sleeps == [10.0]
