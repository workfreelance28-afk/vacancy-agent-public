"""Синхронный клиент Qwen через LM Studio (OpenAI-compatible `/chat/completions`).

Реализует `vacancy_agent.domain.ports.QwenClient`. Структура — по образцу
`hh_client.py`/`superjob_client.py` (тайм-ауты, ограниченные повторы),
но с добавлением разбора/валидации содержимого ответа модели в
`VacancyAssessment` (`docs/DECISIONS.md`, ADR-015) — невалидный ответ
LLM не считается успешным анализом (`docs/ARCHITECTURE.md`, «Надёжность
и безопасность»).
"""

import json
import time
from collections.abc import Mapping
from typing import Any, NamedTuple

import httpx
from pydantic import ValidationError

from vacancy_agent.config.settings import QwenSettings
from vacancy_agent.domain.assessment import VacancyAssessment
from vacancy_agent.domain.qwen_errors import QwenApiError, QwenNetworkError, QwenResponseError
from vacancy_agent.domain.vacancy import Vacancy
from vacancy_agent.integrations.qwen_prompt import (
    RESPONSE_JSON_SCHEMA,
    SCHEMA_VERSION,
    SYSTEM_PROMPT,
    build_user_message,
)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
# 800 оказалось недостаточно живьём (ADR-017): ответ модели обрывался
# на середине строки JSON («Unterminated string») — вероятная причина:
# рассуждающие модели (Qwen3, thinking-режим) тратят часть лимита на
# скрытые reasoning-токены до самого JSON-ответа. Поднято с запасом.
_MAX_TOKENS = 4000


class AssessmentOutcome(NamedTuple):
    """Возврат `assess_with_metadata` (TASK-S2.4): та же `VacancyAssessment`,
    что и `assess()`, плюс honest metadata для eval-отчётов — elapsed time и
    сырой `usage` из ответа LM Studio (`None`, если сервер его не прислал;
    `evaluation/records.py` превращает это в `"unknown"`, а не выдумывает
    числа)."""

    assessment: VacancyAssessment
    elapsed_seconds: float
    usage: Mapping[str, Any] | None


class LMStudioQwenClient:
    """Клиент Qwen. `transport` позволяет подменить сеть в тестах (`httpx.MockTransport`)."""

    def __init__(
        self, settings: QwenSettings, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._settings = settings
        self.model = settings.model
        self._client = httpx.Client(
            base_url=settings.base_url,
            timeout=settings.request_timeout_seconds,
            headers={"Authorization": f"Bearer {settings.api_key.get_secret_value()}"},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LMStudioQwenClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def assess(self, vacancy: Vacancy) -> VacancyAssessment:
        return self.assess_with_metadata(vacancy).assessment

    def assess_with_metadata(self, vacancy: Vacancy) -> AssessmentOutcome:
        """Тот же вызов, что `assess()` (одинаковый `body`, те же
        исключения) — `assess()` — тонкая обёртка над этим методом, её
        поведение и контракт не изменились (TASK-S2.4: «изменение
        production scan/API/queue... не входит»). Дополнительно измеряет
        elapsed time и возвращает сырой `usage` из ответа LM Studio, если
        он есть — нужно только `evaluation/manual_run.py`, обычный
        `daily_scan.py`/production-путь по-прежнему вызывает `assess()`."""

        body = self._build_body(vacancy)
        start = time.monotonic()
        payload = self._post_with_retry(body)
        elapsed = time.monotonic() - start
        content = _extract_content(payload)
        assessment = _parse_assessment(content)
        usage = payload.get("usage")
        return AssessmentOutcome(
            assessment=assessment,
            elapsed_seconds=elapsed,
            usage=usage if isinstance(usage, dict) else None,
        )

    def _build_body(self, vacancy: Vacancy) -> dict[str, Any]:
        return {
            "model": self._settings.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_message(vacancy)},
            ],
            "temperature": 0,
            "max_tokens": _MAX_TOKENS,
            "response_format": {"type": "json_schema", "json_schema": RESPONSE_JSON_SCHEMA},
        }

    def _post_with_retry(self, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self._settings.max_retries + 1):
            response: httpx.Response | None = None
            error: Exception

            try:
                response = self._client.post("/chat/completions", json=body)
            except httpx.TimeoutException as exc:
                error = QwenNetworkError(f"Тайм-аут запроса к LM Studio: {exc}")
            except httpx.HTTPError as exc:
                # Покрывает и «LM Studio выключен» (connection refused).
                error = QwenNetworkError(f"Не удалось подключиться к LM Studio: {exc}")
            else:
                if response.status_code < 400:
                    try:
                        result: dict[str, Any] = response.json()
                    except ValueError as exc:
                        error = QwenNetworkError(f"Не удалось разобрать ответ LM Studio: {exc}")
                    else:
                        return result
                elif response.status_code not in _RETRYABLE_STATUS_CODES:
                    raise _api_error(response)
                else:
                    error = _api_error(response)

            if attempt == self._settings.max_retries:
                raise error
            time.sleep(self._settings.retry_backoff_seconds * (attempt + 1))

        raise AssertionError("недостижимо: цикл всегда возвращает или бросает исключение выше")


def _api_error(response: httpx.Response) -> QwenApiError:
    description = response.text[:200]
    try:
        parsed = response.json()
    except ValueError:
        pass
    else:
        if isinstance(parsed, dict):
            # LM Studio отдаёт `error` то как строку ({"error": "..."}), то
            # как объект ({"error": {"message": "..."}}) в зависимости от
            # типа ошибки — оба варианта наблюдались вживую (см.
            # `docs/DECISIONS.md`, ADR-015).
            error_field = parsed.get("error")
            if isinstance(error_field, dict):
                description = str(error_field.get("message") or description)
            elif isinstance(error_field, str):
                description = error_field

    return QwenApiError(response.status_code, description)


def _extract_content(payload: dict[str, Any]) -> str:
    try:
        return str(payload["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise QwenResponseError(
            f"Не удалось извлечь ответ модели из payload LM Studio: {exc}"
        ) from exc


def _parse_assessment(content: str) -> VacancyAssessment:
    try:
        data = json.loads(_strip_markdown_fence(content))
    except json.JSONDecodeError as exc:
        raise QwenResponseError(f"Ответ модели — не валидный JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise QwenResponseError("Ответ модели — не JSON-объект")

    data.pop("schema_version", None)

    try:
        return VacancyAssessment(schema_version=SCHEMA_VERSION, **data)
    except (ValidationError, TypeError) as exc:
        raise QwenResponseError(
            f"Ответ модели не прошёл валидацию VacancyAssessment: {exc}"
        ) from exc


def _strip_markdown_fence(content: str) -> str:
    """Некоторые модели оборачивают JSON в ```json ... ``` вопреки промпту —
    снимаем фенс, если он есть, вместо того чтобы считать это невалидным
    ответом."""

    text = content.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()
