"""Детерминированный план черновика (TASK-012, ADR-052/053).

С TASK-012 локальная модель больше не пишет ни одного слова о кандидате —
она выбирает только точные цитаты требований вакансии и ссылки на evidence
facts. `ApplicationDraftPlan` — единственное, что генерирует
`ApplicationDraftGenerator.generate` (`domain/ports.py`); весь итоговый
`ApplicationDraftContent` строит доверенный Python-renderer
(`domain/application_draft_renderer.py`) из фиксированных шаблонов и
дословных `EvidenceFact.statement`.

Тот же принцип, что `ApplicationDraftContent` (`domain/application_draft.py`,
TASK-011): `frozen=True, extra="forbid", str_strip_whitespace=True` — план
тоже недоверенный вывод модели, лишние поля отклоняются, а не отбрасываются;
списочные поля — `tuple[str, ...]`, не `list`, по той же причине (обход уже
пройденных инвариантов через мутацию изменяемого списка)."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vacancy_agent.domain.application_draft import MasterProfile, require_unique_strings
from vacancy_agent.domain.application_draft_errors import (
    EvidenceValidationError,
    RequirementQuoteError,
)

# Максимум evidence ID на одну цитату требования (`docs/TASK-012.md`,
# «Доменный контракт плана»: "0..3, unique") — используется и здесь для
# границы поля, и в `application_draft_renderer.py` для расчёта верхней
# границы длины `RequirementMatch.explanation`.
MAX_EVIDENCE_IDS_PER_REQUIREMENT = 3


class RequirementEvidenceSelection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    requirement_quote: str = Field(min_length=1, max_length=1000)
    evidence_fact_ids: tuple[str, ...] = Field(
        default_factory=tuple, max_length=MAX_EVIDENCE_IDS_PER_REQUIREMENT
    )

    @field_validator("evidence_fact_ids")
    @classmethod
    def _unique_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return require_unique_strings(value)


class ApplicationDraftPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    requirements: tuple[RequirementEvidenceSelection, ...] = Field(min_length=1, max_length=50)
    resume_fact_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    cover_letter_fact_ids: tuple[str, ...] = Field(min_length=1, max_length=10)

    @field_validator("resume_fact_ids")
    @classmethod
    def _unique_resume_fact_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return require_unique_strings(value)

    @field_validator("cover_letter_fact_ids")
    @classmethod
    def _unique_cover_letter_fact_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return require_unique_strings(value)

    @model_validator(mode="after")
    def _unique_requirement_quotes(self) -> Self:
        quotes = [selection.requirement_quote for selection in self.requirements]
        if len(set(quotes)) != len(quotes):
            raise ValueError("requirements должен иметь уникальные requirement_quote")
        return self


def validate_plan(
    plan: ApplicationDraftPlan, vacancy_requirements: str, profile: MasterProfile
) -> None:
    """Вторая линия защиты после Pydantic-bounds и JSON-schema `enum`
    (`docs/TASK-012.md`, «Детерминированные проверки», пункты 2 и 4) —
    единственные два инварианта плана, которые нельзя проверить в самих
    Pydantic-моделях выше, потому что ни реальный текст `vacancy.requirements`,
    ни точная версия `MasterProfile` им не передаются при парсинге ответа
    модели. Уникальность quotes/ID и границы количества уже обеспечены
    `ApplicationDraftPlan`/`RequirementEvidenceSelection` выше — не
    дублируются здесь.

    Порядок проверок — сначала все цитаты, потом все evidence ID — совпадает
    с порядком пунктов 2 и 4 спецификации TASK-012. Сообщения об ошибках не
    раскрывают сами цитаты/ID: они приходят из ответа модели и, как и
    остальной ответ, не должны попадать в лог."""

    for selection in plan.requirements:
        if selection.requirement_quote not in vacancy_requirements:
            raise RequirementQuoteError(
                "план ссылается на requirement_quote, которого нет дословно "
                "в vacancy.requirements"
            )

    known_ids = {fact.id for fact in profile.facts}
    referenced: set[str] = set()
    for selection in plan.requirements:
        referenced.update(selection.evidence_fact_ids)
    referenced.update(plan.resume_fact_ids)
    referenced.update(plan.cover_letter_fact_ids)

    if not referenced.issubset(known_ids):
        raise EvidenceValidationError(
            "план ссылается на evidence_fact_id, которых нет в текущей версии Master Profile"
        )
