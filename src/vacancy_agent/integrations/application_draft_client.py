"""Синхронный клиент подготовки черновика отклика через LM Studio (TASK-011,
ADR-034/ADR-035). Реализует `vacancy_agent.domain.ports.ApplicationDraftGenerator`.

Отдельный adapter от `integrations/qwen_client.py`: `QwenClient.assess` не
расширяется (`docs/TASK-011.md`, «Порты и границы»). Структура — тот же
retry/backoff и разбор ответа, что `LMStudioQwenClient`, но НЕ вынесены в общий
модуль: `docs/TASK-011.md` разрешает это только "если существующие
`LMStudioQwenClient.assess` и его тесты сохраняют поведение", а рефакторинг уже
проверенного adapter'а ради одного небольшого переиспользуемого блока (~40
строк) на этом этапе рискованнее, чем сама выгода — решение зафиксировано в
handoff TASK-011, «Решения и допущения»."""

import json
import time
from typing import Any

import httpx
from pydantic import ValidationError

from vacancy_agent.config.settings import QwenSettings
from vacancy_agent.domain.application_draft import MasterProfile
from vacancy_agent.domain.application_draft_errors import (
    ApplicationDraftApiError,
    ApplicationDraftNetworkError,
    ApplicationDraftResponseError,
)
from vacancy_agent.domain.application_draft_plan import ApplicationDraftPlan
from vacancy_agent.domain.assessment import VacancyAssessment
from vacancy_agent.domain.vacancy import Vacancy
from vacancy_agent.integrations.application_draft_prompt import (
    SYSTEM_PROMPT,
    build_response_json_schema,
    build_user_message,
)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class LMStudioApplicationDraftGenerator:
    """`transport` позволяет подменить сеть в тестах (`httpx.MockTransport`) —
    тот же приём, что `LMStudioQwenClient`."""

    def __init__(
        self, settings: QwenSettings, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._settings = settings
        self.model = settings.model
        self._client = httpx.Client(
            base_url=settings.base_url,
            timeout=settings.draft_request_timeout_seconds,
            headers={"Authorization": f"Bearer {settings.api_key.get_secret_value()}"},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LMStudioApplicationDraftGenerator":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def generate(
        self, vacancy: Vacancy, assessment: VacancyAssessment, profile: MasterProfile
    ) -> ApplicationDraftPlan:
        # `profile` строит и prompt (`build_user_message`), и schema
        # (`build_response_json_schema`) — тот же объект, не глобальный
        # `MASTER_PROFILE`, чтобы evidence `enum` в схеме всегда совпадал с
        # тем, что реально сериализовано в MASTER PROFILE промпта
        # (TASK-011, ADR-041).
        body = {
            "model": self._settings.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_message(vacancy, assessment, profile)},
            ],
            "temperature": 0,
            "max_tokens": self._settings.draft_max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": build_response_json_schema(profile),
            },
        }
        payload = self._post_with_retry(body)
        content = _extract_content(payload)
        return _parse_plan(content)

    def _post_with_retry(self, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self._settings.max_retries + 1):
            response: httpx.Response | None = None
            error: Exception

            try:
                response = self._client.post("/chat/completions", json=body)
            except httpx.TimeoutException as exc:
                error = ApplicationDraftNetworkError(f"Тайм-аут запроса к LM Studio: {exc}")
            except httpx.HTTPError as exc:
                error = ApplicationDraftNetworkError(f"Не удалось подключиться к LM Studio: {exc}")
            else:
                if response.status_code < 400:
                    try:
                        result: dict[str, Any] = response.json()
                    except ValueError as exc:
                        # LM Studio уже ответил (HTTP < 400) — повреждённый
                        # JSON-конверт здесь не сетевая проблема, которую
                        # решил бы повтор, а невалидный ответ (`502`, не
                        # `503`) — найдено ревью TASK-011.
                        raise ApplicationDraftResponseError(
                            "Ответ LM Studio — не валидный JSON",
                            category="envelope_json_invalid",
                        ) from exc
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


def _api_error(response: httpx.Response) -> ApplicationDraftApiError:
    description = response.text[:200]
    try:
        parsed = response.json()
    except ValueError:
        pass
    else:
        if isinstance(parsed, dict):
            error_field = parsed.get("error")
            if isinstance(error_field, dict):
                description = str(error_field.get("message") or description)
            elif isinstance(error_field, str):
                description = error_field

    return ApplicationDraftApiError(response.status_code, description)


def _extract_content(payload: dict[str, Any]) -> str:
    try:
        return str(payload["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise ApplicationDraftResponseError(
            "Не удалось извлечь ответ модели из payload LM Studio",
            category="content_missing",
        ) from exc


def _parse_plan(content: str) -> ApplicationDraftPlan:
    parse_target = _strip_markdown_fence(content)
    try:
        data = json.loads(parse_target)
    except json.JSONDecodeError as exc:
        raise ApplicationDraftResponseError(
            "Ответ модели — не валидный JSON",
            category="content_json_invalid",
            diagnostics=_diagnose_json_shape(content, parse_target, exc),
        ) from exc

    if not isinstance(data, dict):
        raise ApplicationDraftResponseError(
            "Ответ модели — не JSON-объект", category="content_not_object"
        )

    try:
        return ApplicationDraftPlan(**data)
    except ValidationError as exc:
        raise ApplicationDraftResponseError(
            "Ответ модели не прошёл валидацию ApplicationDraftPlan",
            category=_validation_category(exc),
        ) from exc
    except TypeError as exc:
        raise ApplicationDraftResponseError(
            "Ответ модели не прошёл валидацию ApplicationDraftPlan",
            category="pydantic_type_error",
        ) from exc


def _validation_category(exc: ValidationError) -> str:
    """Сворачивает Pydantic errors в безопасную категорию без input/текста.

    При нескольких нарушениях возвращается наиболее приоритетная категория;
    ни `loc`, ни `msg`, ни `input` наружу не попадают."""

    error_types = {
        str(error["type"])
        for error in exc.errors(include_url=False, include_input=False)
    }
    if error_types & {"string_too_long", "too_long"}:
        return "pydantic_max_length"
    if "missing" in error_types:
        return "pydantic_missing_field"
    if "extra_forbidden" in error_types:
        return "pydantic_extra_field"
    if "value_error" in error_types:
        return "pydantic_cross_field"
    return "pydantic_schema"


# `.msg` — фиксированная строка стандартного `json`-модуля Python
# (`json/decoder.py`), не производная от ответа модели, но некоторые её
# варианты в отдельных версиях/реализациях (C-ускоритель vs pure-Python
# fallback) исторически интерполировали фрагмент разбираемого текста
# (например, репрезентацию невалидного управляющего символа). Явный allow-
# list по префиксу — не `exc.msg` напрямую — гарантирует, что наружу
# никогда не попадёт ничего, кроме одного из этих коротких кодов, даже если
# будущая версия Python добавит текст к уже известному сообщению
# (TASK-011, ADR-044).
_JSON_ERROR_CODE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("Expecting value", "expecting_value"),
    ("Extra data", "extra_data"),
    ("Unterminated string starting at", "unterminated_string"),
    ("Invalid control character", "invalid_control_character"),
    ("Invalid \\uXXXX escape", "invalid_unicode_escape"),
    ("Invalid \\escape", "invalid_escape"),
    ("Expecting ':' delimiter", "expecting_colon_delimiter"),
    ("Expecting ',' delimiter", "expecting_comma_delimiter"),
    ("Expecting property name enclosed in double quotes", "expecting_property_name"),
)


def _normalize_json_error_code(msg: str) -> str:
    for prefix, code in _JSON_ERROR_CODE_PREFIXES:
        if msg.startswith(prefix):
            return code
    return "other_decode_error"


def _diagnose_json_shape(
    raw_content: str, parse_target: str, error: json.JSONDecodeError
) -> dict[str, object]:
    """Безопасная JSON-shape диагностика (TASK-011, ADR-044) — живая
    находка после ADR-043 показала `category=content_json_invalid`, но
    дальше классифицировать причину (обрезан ли ответ, `<think>`-обёртка,
    markdown fence, текст до/после JSON, конкретный тип синтаксической
    ошибки) было нечем: raw content намеренно не логируется и не
    сохраняется (`docs/TASK-011.md`).

    Возвращает ТОЛЬКО числа/позиции/booleans о ФОРМЕ `content` — ни одного
    значения, символа или подстроки из самого content. `raw_content` —
    как извлечён из payload LM Studio (для длины и наличия fence/`<think>`
    где угодно в тексте); `parse_target` — то же самое после
    `_strip_markdown_fence` (то, что реально передано в `json.loads`, той
    же строке принадлежат `error.pos`); `error` — исключение из этого
    самого вызова `json.loads(parse_target)`.

    Не делает parser терпимее и не пытается сама распарсить content другим
    способом — это чистое наблюдение уже случившегося сбоя, отдельное от
    любого решения смягчить парсинг (`docs/TASK-011.md`: сначала
    наблюдаемость, решение — отдельным шагом)."""

    trimmed = parse_target.strip()
    first_brace = trimmed.find("{")
    last_brace = trimmed.rfind("}")

    return {
        "json_error_code": _normalize_json_error_code(error.msg),
        "content_length": len(raw_content),
        "error_pos": error.pos,
        "distance_from_end": len(parse_target) - error.pos,
        "starts_with_brace": trimmed.startswith("{"),
        "ends_with_brace": trimmed.endswith("}"),
        "has_markdown_fence": "```" in raw_content,
        "has_think_tags": "<think>" in raw_content or "</think>" in raw_content,
        "has_leading_text": first_brace > 0,
        "has_trailing_text": last_brace != -1 and last_brace < len(trimmed) - 1,
    }


def _strip_markdown_fence(content: str) -> str:
    """Та же терпимость к ```json ... ``` обёртке, что `qwen_client.py` —
    некоторые модели оборачивают JSON в markdown-фенс вопреки промпту."""

    text = content.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()
