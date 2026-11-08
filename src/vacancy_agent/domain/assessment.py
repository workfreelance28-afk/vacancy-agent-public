from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Recommendation = Literal["skip", "review", "strong_match"]
Importance = Literal["low", "medium", "high"]

# Границы длины свободного текста модели (threat-model review, 2026-08-29):
# `min_length=1` уже был, `max_length` не было вовсе — компрометированная
# модель (prompt injection из недоверенного текста вакансии) могла впихнуть
# сюда неограниченно длинную строку; она дословно попадает в SQLite и
# отдаётся через `GET /vacancies/{id}` (`api/schemas.py:
# VacancyDetailResponse.assessment`). Границы — только в Pydantic, НЕ в
# `RESPONSE_JSON_SCHEMA` (`integrations/qwen_prompt.py`): живая
# compatibility-диагностика TASK-011 (ADR-040) подтвердила, что `maxLength`
# в JSON schema, которую видит LM Studio, ломает grammar-компиляцию
# (`HTTP 400`) — тот же приём, что и `ApplicationDraftContent`
# (`domain/application_draft.py`): верхняя граница проверяется только
# после ответа модели, не участвует в схеме, которую видит модель.
_ShortText = Annotated[str, Field(min_length=1, max_length=200)]
_JustificationText = Annotated[str, Field(min_length=1, max_length=1000)]


class Strength(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    skill: _ShortText
    evidence: _JustificationText


class Gap(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    skill: _ShortText
    importance: Importance


class VacancyAssessment(BaseModel):
    """Оценка вакансии моделью. См. `docs/DATA_CONTRACTS.md`.

    Невалидный JSON от LLM не должен превращаться в эту модель — ответ
    считается недоверенным вводом и проверяется до создания объекта.
    `extra="forbid"`: pydantic по умолчанию молча отбрасывает незнакомые
    поля (`docs/DECISIONS.md`, ADR-016) — для доверенного внутреннего
    JSON это нормально, но для ответа LLM означает, что модель могла
    вернуть непредусмотренные/лишние поля (признак несоблюдения формата
    или манипуляции через инъекцию), а мы бы этого не заметили. Строгий
    JSON-schema-запрос к LM Studio (`integrations/qwen_prompt.py`,
    `additionalProperties: false`) уже должен это предотвращать на
    стороне модели — `extra="forbid"` здесь работает как независимая
    вторая линия защиты на стороне валидации, а не полагается только на
    добросовестность модели.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(min_length=1)
    score: int = Field(ge=0, le=100)
    relevant: bool
    strengths: list[Strength] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    risks: list[_JustificationText] = Field(default_factory=list)
    recommendation: Recommendation


def passes_relevance_threshold(assessment: VacancyAssessment, min_score: int) -> bool:
    """Детерминированный порог поверх оценки модели (`docs/DECISIONS.md`,
    ADR-015) — `assessment.relevant` в одиночку не решает, идёт ли вакансия
    дальше по конвейеру: числовой порог настраивается отдельно
    (`QwenSettings.min_score`) и не должен зависеть от того, насколько
    охотно модель ставит `relevant=true`."""

    return assessment.relevant and assessment.score >= min_score
