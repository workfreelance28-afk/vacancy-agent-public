import json
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from vacancy_agent.config.settings import QwenSettings
from vacancy_agent.domain.qwen_errors import QwenApiError, QwenNetworkError, QwenResponseError
from vacancy_agent.domain.vacancy import Vacancy
from vacancy_agent.integrations.qwen_client import LMStudioQwenClient


def _settings(**overrides: object) -> QwenSettings:
    defaults: dict[str, object] = {
        "base_url": "http://localhost:1234/v1",
        "model": "qwen-test-model",
        "api_key": SecretStr("test-key-should-never-appear-in-a-url-or-log"),
        "request_timeout_seconds": 1.0,
        "max_retries": 2,
        "retry_backoff_seconds": 0.001,
    }
    defaults.update(overrides)
    return QwenSettings(**defaults)


def _vacancy(**overrides: object) -> Vacancy:
    defaults: dict[str, object] = {
        "hh_id": "1",
        "title": "AI Integration Engineer",
        "company": "OOO Ромашка",
        "description": "Интеграция LLM в продукт, работа с API и RAG.",
        "salary_from": 200_000,
        "salary_to": 300_000,
        "salary_currency": "rub",
        "area": "Москва",
        "experience": "От 1 года до 3 лет",
        "url": "https://superjob.ru/vakansii/1.html",
        "published_at": datetime(2026, 8, 20, tzinfo=UTC),
        "fetched_at": datetime(2026, 8, 20, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Vacancy(**defaults)


def _chat_response(content_obj: object) -> dict[str, object]:
    return {
        "choices": [{"message": {"content": json.dumps(content_obj, ensure_ascii=False)}}],
    }


_VALID_ASSESSMENT = {
    "score": 82,
    "relevant": True,
    "strengths": [{"skill": "LLM", "evidence": "Интеграция LLM в продукт"}],
    "gaps": [{"skill": "Docker", "importance": "medium"}],
    "risks": ["требуется сильный Python"],
    "recommendation": "review",
}


def test_assess_parses_valid_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chat_response(_VALID_ASSESSMENT))

    with LMStudioQwenClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        assessment = client.assess(_vacancy())

    assert assessment.score == 82
    assert assessment.relevant is True
    assert assessment.schema_version == "1"
    assert assessment.strengths[0].skill == "LLM"
    assert assessment.gaps[0].importance == "medium"
    assert assessment.recommendation == "review"


def test_assess_sends_only_required_vacancy_fields() -> None:
    seen_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_body.update(json.loads(request.content))
        return httpx.Response(200, json=_chat_response(_VALID_ASSESSMENT))

    vacancy = _vacancy(key_skills=["Python", "Docker"], employment="Полная занятость")
    with LMStudioQwenClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        client.assess(vacancy)

    user_message = seen_body["messages"][1]["content"]
    assert vacancy.title in user_message
    assert vacancy.area in user_message
    assert vacancy.experience in user_message
    assert vacancy.company in user_message
    assert "key_skills" not in user_message
    assert "employment" not in user_message
    assert "url" not in user_message


def test_assess_sends_api_key_as_bearer_header_not_in_url() -> None:
    seen_headers: list[httpx.Headers] = []
    seen_urls: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        seen_urls.append(request.url)
        return httpx.Response(200, json=_chat_response(_VALID_ASSESSMENT))

    settings = _settings()
    with LMStudioQwenClient(settings, transport=httpx.MockTransport(handler)) as client:
        client.assess(_vacancy())

    assert seen_headers[0]["Authorization"] == f"Bearer {settings.api_key.get_secret_value()}"
    assert "test-key-should-never-appear-in-a-url-or-log" not in str(seen_urls[0])


def test_assess_strips_markdown_fence_around_json() -> None:
    fenced = "```json\n" + json.dumps(_VALID_ASSESSMENT, ensure_ascii=False) + "\n```"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": fenced}}]})

    with LMStudioQwenClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        assessment = client.assess(_vacancy())

    assert assessment.score == 82


def test_assess_raises_qwen_response_error_on_invalid_json_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "not valid json{{{"}}]}
        )

    with LMStudioQwenClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(QwenResponseError):
            client.assess(_vacancy())


def test_assess_raises_qwen_response_error_on_out_of_range_score() -> None:
    bad = dict(_VALID_ASSESSMENT, score=150)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chat_response(bad))

    with LMStudioQwenClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(QwenResponseError):
            client.assess(_vacancy())


def test_assess_raises_qwen_response_error_on_missing_field() -> None:
    bad = dict(_VALID_ASSESSMENT)
    del bad["recommendation"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chat_response(bad))

    with LMStudioQwenClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(QwenResponseError):
            client.assess(_vacancy())


def test_assess_raises_qwen_response_error_on_missing_choices() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    with LMStudioQwenClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(QwenResponseError):
            client.assess(_vacancy())


def test_assess_raises_qwen_network_error_when_lm_studio_is_down() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    settings = _settings(max_retries=1)
    with LMStudioQwenClient(settings, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(QwenNetworkError):
            client.assess(_vacancy())


def test_assess_raises_qwen_network_error_on_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated timeout", request=request)

    settings = _settings(max_retries=1)
    with LMStudioQwenClient(settings, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(QwenNetworkError):
            client.assess(_vacancy())


def test_assess_raises_qwen_api_error_when_model_not_loaded() -> None:
    """Типичный ответ LM Studio, когда сервер поднят, но модель не
    загружена — HTTP-ошибка на уровне API, не сеть."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"message": "No models loaded"}}
        )

    with LMStudioQwenClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(QwenApiError) as exc_info:
            client.assess(_vacancy())

    assert exc_info.value.status_code == 400


def test_assess_retries_on_5xx_then_succeeds() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 2:
            return httpx.Response(503, json={"error": {"message": "unavailable"}})
        return httpx.Response(200, json=_chat_response(_VALID_ASSESSMENT))

    with LMStudioQwenClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        assessment = client.assess(_vacancy())

    assert attempts["count"] == 2
    assert assessment.score == 82


def test_assess_raises_qwen_api_error_after_exhausting_retries_on_5xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    settings = _settings(max_retries=1)
    with LMStudioQwenClient(settings, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(QwenApiError):
            client.assess(_vacancy())
