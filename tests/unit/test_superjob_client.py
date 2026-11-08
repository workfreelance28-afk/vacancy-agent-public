import httpx
import pytest
from pydantic import SecretStr

from vacancy_agent.config.settings import SuperJobSettings
from vacancy_agent.integrations.superjob_client import SuperJobClient
from vacancy_agent.integrations.superjob_errors import SuperJobApiError, SuperJobNetworkError


def _settings(**overrides: object) -> SuperJobSettings:
    defaults: dict[str, object] = {
        "api_key": SecretStr("test-key-should-never-appear-in-a-url-or-log"),
        "base_url": "https://api.superjob.ru/2.0",
        "request_timeout_seconds": 1.0,
        "count": 2,
        "max_retries": 2,
        "retry_backoff_seconds": 0.001,
    }
    defaults.update(overrides)
    return SuperJobSettings(**defaults)


def _item(id_: int) -> dict[str, object]:
    return {
        "id": id_,
        "profession": f"Vacancy {id_}",
        "candidat": "текст",
        "payment_from": 0,
        "payment_to": 0,
        "currency": "rub",
        "town": {"title": "Москва"},
        "firm_name": "OOO Тест",
        "link": f"https://superjob.ru/vakansii/{id_}.html",
        "experience": {"title": "Не требуется"},
        "type_of_work": {"title": "Полная занятость"},
        "date_published": 1755673200,
    }


def test_search_vacancies_paginates_while_more_is_true() -> None:
    pages = {
        "0": {"objects": [_item(1), _item(2)], "more": True},
        "1": {"objects": [_item(3)], "more": False},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        return httpx.Response(200, json=pages[page])

    with SuperJobClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        vacancies = list(client.search_vacancies({"keyword": "AI"}))

    assert [v.hh_id for v in vacancies] == ["superjob:1", "superjob:2", "superjob:3"]


def test_search_vacancies_stops_when_more_is_false_even_with_items() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"objects": [_item(1)], "more": False})

    with SuperJobClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        vacancies = list(client.search_vacancies({}))

    assert [v.hh_id for v in vacancies] == ["superjob:1"]


def test_search_vacancies_handles_empty_objects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"objects": [], "more": True})

    with SuperJobClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        vacancies = list(client.search_vacancies({}))

    assert vacancies == []


def test_search_vacancies_sends_api_key_as_header_not_query_param() -> None:
    seen_headers: list[httpx.Headers] = []
    seen_urls: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        seen_urls.append(request.url)
        return httpx.Response(200, json={"objects": [], "more": False})

    settings = _settings()
    with SuperJobClient(settings, transport=httpx.MockTransport(handler)) as client:
        list(client.search_vacancies({"keyword": "AI"}))

    assert seen_headers[0]["X-Api-App-Id"] == settings.api_key.get_secret_value()
    assert "test-key-should-never-appear-in-a-url-or-log" not in str(seen_urls[0])


def test_search_vacancies_passes_query_params_and_default_count() -> None:
    seen_params: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.append(dict(request.url.params))
        return httpx.Response(200, json={"objects": [], "more": False})

    with SuperJobClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        list(client.search_vacancies({"keyword": "AI, LLM", "town": "Москва"}))

    assert seen_params[0]["keyword"] == "AI, LLM"
    assert seen_params[0]["town"] == "Москва"
    assert seen_params[0]["count"] == "2"


def test_raises_superjob_api_error_without_retry_on_401() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(401, json={"error": {"message": "Invalid app id"}})

    with SuperJobClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SuperJobApiError) as exc_info:
            list(client.search_vacancies({}))

    assert attempts["count"] == 1
    assert exc_info.value.status_code == 401


def test_raises_superjob_api_error_without_retry_on_403() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "forbidden"}})

    with SuperJobClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SuperJobApiError) as exc_info:
            list(client.search_vacancies({}))

    assert exc_info.value.status_code == 403


def test_get_page_retries_on_5xx_then_succeeds() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 2:
            return httpx.Response(503, json={"error": {"message": "unavailable"}})
        return httpx.Response(200, json={"objects": [_item(1)], "more": False})

    with SuperJobClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        vacancies = list(client.search_vacancies({}))

    assert attempts["count"] == 2
    assert [v.hh_id for v in vacancies] == ["superjob:1"]


def test_raises_superjob_api_error_after_exhausting_retries_on_5xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    settings = _settings(max_retries=1)
    with SuperJobClient(settings, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SuperJobApiError):
            list(client.search_vacancies({}))


def test_raises_superjob_network_error_after_exhausting_retries_on_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated timeout", request=request)

    settings = _settings(max_retries=1)
    with SuperJobClient(settings, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SuperJobNetworkError):
            list(client.search_vacancies({}))


def test_get_page_raises_superjob_network_error_on_malformed_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not valid json{{{")

    settings = _settings(max_retries=0)
    with SuperJobClient(settings, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SuperJobNetworkError):
            list(client.search_vacancies({}))
