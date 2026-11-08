"""Доменные модели проверяемого черновика отклика (TASK-011, ADR-034/ADR-035).

Все модели — `frozen=True, extra="forbid", str_strip_whitespace=True`: лишние
поля от модели отклоняются, а не тихо отбрасываются (та же причина, что у
`VacancyAssessment`, `docs/DECISIONS.md` ADR-016 — ответ LLM считается
недоверенным вводом, а не доверенным внутренним JSON); строка, состоящая
только из пробелов, считается пустой ДО проверки `min_length` (см.
`docs/TASK-011.md`, «Доменный контракт») — подтверждено эмпирически:
`str_strip_whitespace` применяется и внутри `tuple[str, ...]`-полей, не
только к строковым полям верхнего уровня. Границы длины строк/списков —
дословно зафиксированные в спецификации значения, не произвольные лимиты.

Списочные поля объявлены как `tuple[X, ...]`, не `list[X]`: `frozen=True`
запрещает только переприсвоение атрибута (`model.field = ...`), но не
мутацию объекта, на который атрибут уже ссылается — `model.field.append(...)`
на изменяемом `list` тихо обходил бы уже пройденные инварианты (unique
evidence IDs, coverage/evidence правило, evidence-проверку по Master Profile)
без нового вызова валидации. `tuple` неизменяем сам по себе — обходного пути
такого рода не остаётся (найдено ревью TASK-011, подтверждено эмпирически:
`profile.facts.append(...)` до этой правки успешно мутировал уже
провалидированный `MasterProfile`).

`ApplicationDraft` (envelope) специально НЕ создаётся моделью — `schema_version`,
`vacancy_id` и `profile_version` проставляет только доверенный Python-код
(`application/application_drafts.py`), поэтому envelope не участвует в JSON
schema, которую видит Qwen (`integrations/application_draft_prompt.py`).

С TASK-012 (ADR-052/053) модель больше не генерирует `ApplicationDraftContent`
вовсе — она генерирует только `ApplicationDraftPlan`
(`domain/application_draft_plan.py`): точные цитаты требований вакансии и
ссылки на evidence facts. `ApplicationDraftContent` теперь ВСЕГДА строит
доверенный Python-renderer (`domain/application_draft_renderer.py`) из
фиксированных шаблонов и дословных `EvidenceFact.statement` уже
провалидированного плана — свободного текста о кандидате в ответе модели
больше нет в принципе, поэтому его нельзя ни проверить, ни выдумать заново.
"""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Coverage = Literal["full", "partial", "gap"]

_VACANCY_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"

# `gaps`/`questions_for_user`/`warnings` — тип-уровневое ограничение
# (`Annotated`), не отдельный `@field_validator`: границы длины элемента
# должны попасть в сгенерированную JSON schema (`integrations/
# application_draft_prompt.py: ApplicationDraftContent.model_json_schema()`),
# а ручной валидатор для этого невидим — тот же класс проблемы, что нашла
# живая приёмка TASK-011 для `evidence_fact_ids` (см. ADR-039).
_BoundedString = Annotated[str, Field(min_length=1, max_length=2000)]


class EvidenceFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=100)
    category: str = Field(min_length=1, max_length=100)
    statement: str = Field(min_length=1, max_length=2000)
    source: str = Field(min_length=1, max_length=500)


class MasterProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    version: str = Field(min_length=1, max_length=200)
    facts: tuple[EvidenceFact, ...] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _unique_fact_ids(self) -> Self:
        ids = [fact.id for fact in self.facts]
        if len(set(ids)) != len(ids):
            raise ValueError("MasterProfile.facts должен иметь уникальные id")
        return self


class RequirementMatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    requirement: str = Field(min_length=1, max_length=1000)
    coverage: Coverage
    # 6500, не 2000 (TASK-011): с TASK-012 `explanation` больше не пишет
    # модель — его строит renderer (`domain/application_draft_renderer.py`)
    # из фиксированного префикса и ДОСЛОВНЫХ `EvidenceFact.statement` (до
    # `_REQUIREMENT_EVIDENCE_MAX_FACTS`, каждый до `EvidenceFact.statement`
    # max_length=2000) — обрезать statement нельзя, это может изменить
    # смысл (`docs/TASK-012.md`, «Детерминированный renderer»), поэтому
    # граница расширена под точный худший случай, не подобрана произвольно
    # (см. `test_application_draft_renderer.py`, тест на факты максимальной
    # длины).
    explanation: str = Field(min_length=1, max_length=6500)
    evidence_fact_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=20)

    @field_validator("evidence_fact_ids")
    @classmethod
    def _unique_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return require_unique_strings(value)

    @model_validator(mode="after")
    def _coverage_requires_matching_evidence(self) -> Self:
        if self.coverage in ("full", "partial") and not self.evidence_fact_ids:
            raise ValueError(f"coverage={self.coverage!r} требует хотя бы один evidence_fact_id")
        if self.coverage == "gap" and self.evidence_fact_ids:
            raise ValueError("coverage='gap' не должен ссылаться на evidence_fact_ids")
        return self


class ResumeSuggestion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    target_section: str = Field(min_length=1, max_length=200)
    suggested_text: str = Field(min_length=1, max_length=4000)
    evidence_fact_ids: tuple[str, ...] = Field(min_length=1, max_length=20)

    @field_validator("evidence_fact_ids")
    @classmethod
    def _unique_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return require_unique_strings(value)


class EvidenceParagraph(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=4000)
    evidence_fact_ids: tuple[str, ...] = Field(min_length=1, max_length=20)

    @field_validator("evidence_fact_ids")
    @classmethod
    def _unique_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return require_unique_strings(value)


class ApplicationDraftContent(BaseModel):
    """То, что генерирует модель — без envelope-полей (см. модуль docstring)."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    match_summary: str = Field(min_length=1, max_length=4000)
    requirements: tuple[RequirementMatch, ...] = Field(min_length=1, max_length=50)
    resume_suggestions: tuple[ResumeSuggestion, ...] = Field(default_factory=tuple, max_length=20)
    cover_letter_paragraphs: tuple[EvidenceParagraph, ...] = Field(min_length=1, max_length=10)
    gaps: tuple[_BoundedString, ...] = Field(default_factory=tuple, max_length=20)
    questions_for_user: tuple[_BoundedString, ...] = Field(default_factory=tuple, max_length=20)
    warnings: tuple[_BoundedString, ...] = Field(default_factory=tuple, max_length=20)


class ApplicationDraft(BaseModel):
    """Envelope, который создаёт Python — `schema_version`/`vacancy_id`/
    `profile_version` не входят в то, что генерирует модель."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    schema_version: str = Field(min_length=1, max_length=200)
    vacancy_id: str = Field(min_length=1, max_length=255, pattern=_VACANCY_ID_PATTERN)
    profile_version: str = Field(min_length=1, max_length=200)
    content: ApplicationDraftContent


def require_unique_strings(value: tuple[str, ...]) -> tuple[str, ...]:
    """Общий `@field_validator`-helper для всех `tuple[str, ...]` полей
    evidence ID этого модуля и `domain/application_draft_plan.py`
    (TASK-012) — не приватный (без ведущего `_`) именно потому, что
    используется за пределами этого модуля."""

    if len(set(value)) != len(value):
        raise ValueError("evidence_fact_ids должен содержать уникальные значения")
    return value
