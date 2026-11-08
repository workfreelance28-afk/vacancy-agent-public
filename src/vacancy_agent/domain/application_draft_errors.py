"""Ошибки генерации/применимости черновика отклика (TASK-011, ADR-034/ADR-035).

В домене, а не в `integrations/` — по той же причине, что `qwen_errors.py`
(`docs/DECISIONS.md`, ADR-012): `application/application_drafts.py` должно
различать типы ошибок, не зная о конкретной реализации adapter'а.

Сообщения этих ошибок никогда не содержат сырой ответ модели, полный
prompt или Master Profile целиком (`docs/TASK-011.md`: "Секреты, полный
prompt, Master Profile и raw ответ не логируются") — только короткое,
безопасное описание категории сбоя. Исходное исключение остаётся доступно
через `__cause__` (`raise ... from exc`) для локальной отладки, но не
попадает в текст сообщения, которое может быть залогировано."""

from collections.abc import Callable, Mapping
from types import MappingProxyType

# Замкнутый список нормализованных кодов `json_error_code` (ADR-044,
# `integrations/application_draft_client.py: _normalize_json_error_code`) —
# ЕДИНСТВЕННЫЙ источник истины для допустимых значений этого поля. Codex
# review-fix (ADR-046): прежняя граница ADR-045 проверяла только
# `isinstance(value, str)` для `json_error_code` — то есть ЛЮБАЯ строка,
# включая raw-текст модели, проходила бы через уже разрешённый ключ. Тип —
# не значение: allow-list здесь обязан быть перечислением конкретных
# строк, не просто "это str".
JSON_ERROR_CODES = frozenset(
    {
        "expecting_value",
        "extra_data",
        "unterminated_string",
        "invalid_control_character",
        "invalid_unicode_escape",
        "invalid_escape",
        "expecting_colon_delimiter",
        "expecting_comma_delimiter",
        "expecting_property_name",
        "other_decode_error",
    }
)


def _is_valid_error_code(value: object) -> bool:
    return isinstance(value, str) and value in JSON_ERROR_CODES


def _is_bool(value: object) -> bool:
    return isinstance(value, bool)


def _is_plain_int(value: object) -> bool:
    # `bool` — подкласс `int` в Python, поэтому исключаем его явно: иначе
    # `content_length=True` тихо прошёл бы под видом числа.
    return isinstance(value, int) and not isinstance(value, bool)


# Allow-list JSON-shape полей ADR-044/`_diagnose_json_shape`
# (`integrations/application_draft_client.py`) — по одному валидатору на
# ключ, не просто по ожидаемому типу значения. Единственное место,
# определяющее, что вообще может попасть в
# `ApplicationDraftResponseError.diagnostics` — и, значит, в лог
# (Codex review-fix ADR-045/ADR-046: прежняя версия принимала `diagnostics`
# как произвольный `dict[str, object]` без проверки на границе, полагаясь
# только на то, что единственный существующий вызывающий код
# (`_diagnose_json_shape`) сам формирует безопасный словарь; это не
# граница, а совпадение).
_DIAGNOSTICS_VALIDATORS: Mapping[str, Callable[[object], bool]] = {
    "json_error_code": _is_valid_error_code,
    "content_length": _is_plain_int,
    "error_pos": _is_plain_int,
    "distance_from_end": _is_plain_int,
    "starts_with_brace": _is_bool,
    "ends_with_brace": _is_bool,
    "has_markdown_fence": _is_bool,
    "has_think_tags": _is_bool,
    "has_leading_text": _is_bool,
    "has_trailing_text": _is_bool,
}


def _sanitize_diagnostics(raw: Mapping[str, object] | None) -> dict[str, object]:
    """Строгая граница на конструкторе `ApplicationDraftResponseError`
    (Codex review-fix, ADR-045/ADR-046): любой ключ вне явного allow-list
    JSON-shape полей ADR-044 — или значение, не прошедшее валидатор для
    уже известного ключа (неверный тип, ЛИБО, для `json_error_code`,
    строка вне `JSON_ERROR_CODES`) — молча отбрасывается, не поднимает
    исключение и никогда не попадает в `self.diagnostics`, а значит и в
    лог (`api/app.py`)."""

    if not raw:
        return {}

    safe: dict[str, object] = {}
    for key, is_valid in _DIAGNOSTICS_VALIDATORS.items():
        if key in raw and is_valid(raw[key]):
            safe[key] = raw[key]
    return safe


class ApplicationDraftGenerationError(Exception):
    """Базовая ошибка генерации черновика отклика через локальный Qwen."""


class ApplicationDraftNetworkError(ApplicationDraftGenerationError):
    """Сбой сети/тайм-аут при обращении к LM Studio, либо ответ, который не
    удалось разобрать как JSON вообще (LM Studio недоступен → HTTP `503`)."""


class ApplicationDraftApiError(ApplicationDraftGenerationError):
    """LM Studio вернул ответ с кодом ошибки после исчерпания повторов —
    сервер отвечает, но не может выполнить запрос (HTTP `503`)."""

    def __init__(self, status_code: int, description: str) -> None:
        self.status_code = status_code
        # `description` хранится для программного доступа, но НЕ попадает в
        # текст исключения — LM Studio может отражать часть запроса/ответа
        # модели в теле своей ошибки, а сообщение исключения — то, что
        # обычно логируется без дополнительной фильтрации.
        self.description = description
        super().__init__(f"LM Studio вернул {status_code}")


class ApplicationDraftResponseError(ApplicationDraftGenerationError):
    """LM Studio ответил, но содержимое (`content`) не удалось разобрать как
    `ApplicationDraftContent` — невалидный JSON, не JSON-объект, нарушение
    schema/типов/границ (HTTP `502`).

    `category` — безопасный машинный код без значений из ответа модели. Он
    нужен для эксплуатационной диагностики ADR-043: raw content по-прежнему
    не логируется и не сохраняется.

    `diagnostics` — опциональная безопасная JSON-shape метаинформация
    (ADR-044): длины/позиции/числа и booleans о ФОРМЕ `content`
    (обрезан ли JSON, есть ли markdown fence/`<think>`-обёртка/текст до
    первой `{`/после последней `}`, нормализованный код
    `JSONDecodeError.msg`). Заполняется ТОЛЬКО для
    `category="content_json_invalid"` (`integrations/
    application_draft_client.py: _diagnose_json_shape`) — для любой другой
    `category` конструктор игнорирует переданный `diagnostics` целиком и
    хранит пустой словарь, независимо от того, что было передано
    (Codex review-fix ADR-046: раньше диагностика принималась для ЛЮБОЙ
    категории, хотя по смыслу применима только к одной). Никогда не
    содержит сам `content` или его фрагменты, только числа/позиции/
    booleans о его структуре.

    Что бы ни было передано в `diagnostics` (в том числе напрямую, минуя
    `_diagnose_json_shape`), конструктор пропускает его через
    `_sanitize_diagnostics` — итоговый `self.diagnostics` гарантированно
    содержит только пары ключ/значение из allow-list JSON-shape полей,
    прошедшие валидатор конкретного ключа (ADR-045/ADR-046; для
    `json_error_code` — членство в `JSON_ERROR_CODES`, не просто `str`);
    любой лишний ключ или непрошедшее валидацию значение отбрасывается до
    того, как объект вообще существует. `self.diagnostics` — неизменяемый
    `MappingProxyType`: попытка присвоить в него ключ после создания
    исключения поднимает `TypeError`, а не тихо мутирует уже
    провалидированный результат."""

    def __init__(
        self,
        message: str,
        *,
        category: str = "response_invalid",
        diagnostics: Mapping[str, object] | None = None,
    ) -> None:
        self.category = category
        sanitized = (
            _sanitize_diagnostics(diagnostics) if category == "content_json_invalid" else {}
        )
        self.diagnostics: Mapping[str, object] = MappingProxyType(sanitized)
        super().__init__(message)


class EvidenceValidationError(ApplicationDraftGenerationError):
    """`ApplicationDraftPlan` прошёл JSON/Pydantic-schema (TASK-012), но
    ссылается на `evidence_fact_id`, которого нет в точной версии
    переданного Master Profile (HTTP `502`) —
    `domain/application_draft_plan.py: validate_plan`, вторая линия защиты
    после `enum` в JSON schema (ADR-041/ADR-052)."""


class RequirementQuoteError(ApplicationDraftGenerationError):
    """`ApplicationDraftPlan` прошёл JSON/Pydantic-schema, но
    `requirement_quote` не является точной дословной подстрокой реального
    `vacancy.requirements` (TASK-012) — пересказ или новая фраза вместо
    цитаты (HTTP `502`). Проверяется `domain/application_draft_plan.py:
    validate_plan` ДО evidence-проверки (`docs/TASK-012.md`,
    «Детерминированные проверки», пункт 2)."""


class ApplicationDraftConcurrencyError(ApplicationDraftGenerationError):
    """Проиграна локальная гонка за вставку, и после одной повторной попытки
    сохранения победившая запись всё ещё не совпадает по
    `profile_version`/`prompt_version`/`model` с этой генерацией (TASK-011,
    ADR-050 — Codex review-fix: race-ветка раньше сравнивала только
    `model`, из-за чего могла отдать как `reused=True` черновик с чужим
    `profile_version`, и безусловно принимала результат повторной попытки
    без проверки вовсе).

    Это не программная ошибка, а редкое транзиентное состояние
    однопользовательского локального сценария — минимум два конкурентных
    воркера сохранили несовпадающие версии подряд. Возвращать такой
    черновик как `reused=True` значило бы выдать чужую версию за
    актуальную; вместо этого вызывающий код должен получить безопасную
    ошибку и повторить запрос (HTTP `409`) — к этому моменту конкуренция
    почти наверняка уляжется."""


class VacancyNotEligibleError(Exception):
    """Вакансия существует, но не готова для генерации черновика: не принята
    hard filters, не оценена Qwen, не прошла порог или её статус —
    `skipped` (HTTP `409`)."""
