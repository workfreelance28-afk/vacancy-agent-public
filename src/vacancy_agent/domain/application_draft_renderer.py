"""Детерминированный renderer (TASK-012, ADR-052/053) — единственное место,
которое превращает уже проверенный `ApplicationDraftPlan`
(`domain/application_draft_plan.py`) в существующий публичный
`ApplicationDraftContent` (`domain/application_draft.py`).

Чистая функция: без LLM, сети, времени, БД и скрытой глобальной
конфигурации (`docs/TASK-012.md`, «Детерминированный renderer»). Вызывается
только ПОСЛЕ `validate_plan` (`application/application_drafts.py`) — плану
здесь доверяют: каждый `evidence_fact_id` уже подтверждён существующим в
переданном `profile`, каждый `requirement_quote` уже подтверждён дословной
подстрокой `vacancy.requirements`.

Каждое фактическое утверждение в результате — либо фиксированный шаблон
ниже, либо ДОСЛОВНЫЙ `EvidenceFact.statement`; свободного текста о
кандидате модель больше не пишет вообще, поэтому этому renderer нечего
"додумывать" — он только собирает уже проверенные строки в форму
`ApplicationDraftContent`. Порядок элементов результата всегда совпадает с
порядком соответствующих полей `plan` — детерминизм гарантирован порядком
итерации, без сортировки и без скрытого состояния."""

from collections.abc import Mapping

from vacancy_agent.domain.application_draft import (
    ApplicationDraftContent,
    Coverage,
    EvidenceFact,
    EvidenceParagraph,
    MasterProfile,
    RequirementMatch,
    ResumeSuggestion,
)
from vacancy_agent.domain.application_draft_plan import (
    ApplicationDraftPlan,
    RequirementEvidenceSelection,
)

_EXPLANATION_PREFIX = "Подтверждено следующими фактами профиля:\n"
_NO_EVIDENCE_EXPLANATION = "Требование не подтверждено ни одним фактом профиля."
_GAP_PREFIX = "Требование не подтверждено фактами профиля: "
_QUESTION_PREFIX = "Уточните у пользователя, применимо ли это требование: "
_COVER_LETTER_PREFIX = "Факт из профиля кандидата: "
_WARNING_TEXT = (
    "Это evidence-only черновик: каждое утверждение — фиксированный служебный "
    "шаблон либо дословная цитата из Master Profile, без участия модели в "
    "формулировках. Релевантность подобранных фактов и итоговый текст не "
    "подтверждены как готовые к использованию — требуется ручная проверка перед "
    "отправкой."
)
_MATCH_SUMMARY_TEMPLATE = (
    "Проверено требований вакансии: {total}. Подтверждено фактами профиля: "
    "{with_evidence}. Не подтверждено: {without_evidence}. Это evidence-only "
    "черновик — релевантность подобранных фактов требует ручной проверки."
)

# Allow-list `category` (`domain/master_profile.py`) → раздел резюме
# (`docs/TASK-012.md`, «Детерминированный renderer»: "target_section
# определяется Python по allow-listed отображению category → section") —
# закрытый список, не catch-all: неизвестная категория должна упасть явно
# (`_target_section_for` ниже), а не тихо получить произвольный раздел.
_CATEGORY_TO_RESUME_SECTION: Mapping[str, str] = {
    "role": "Целевая позиция",
    "preference": "Условия работы",
    "education": "Образование",
    "language": "Языки",
    "experience": "Опыт работы",
    "employment": "Опыт работы",
    "skill": "Навыки",
    "project": "Проекты",
    "achievement": "Достижения",
}


def render_application_draft_content(
    plan: ApplicationDraftPlan, profile: MasterProfile
) -> ApplicationDraftContent:
    facts_by_id = {fact.id: fact for fact in profile.facts}

    requirements = tuple(
        _render_requirement(selection, facts_by_id) for selection in plan.requirements
    )
    resume_suggestions = tuple(
        _render_resume_suggestion(facts_by_id[fact_id]) for fact_id in plan.resume_fact_ids
    )
    cover_letter_paragraphs = tuple(
        _render_cover_letter_paragraph(facts_by_id[fact_id])
        for fact_id in plan.cover_letter_fact_ids
    )
    gaps = tuple(
        f"{_GAP_PREFIX}{selection.requirement_quote}"
        for selection in plan.requirements
        if not selection.evidence_fact_ids
    )
    questions_for_user = tuple(
        f"{_QUESTION_PREFIX}{selection.requirement_quote}"
        for selection in plan.requirements
        if not selection.evidence_fact_ids
    )

    with_evidence = sum(1 for selection in plan.requirements if selection.evidence_fact_ids)
    total = len(plan.requirements)
    match_summary = _MATCH_SUMMARY_TEMPLATE.format(
        total=total, with_evidence=with_evidence, without_evidence=total - with_evidence
    )

    return ApplicationDraftContent(
        match_summary=match_summary,
        requirements=requirements,
        resume_suggestions=resume_suggestions,
        cover_letter_paragraphs=cover_letter_paragraphs,
        gaps=gaps,
        questions_for_user=questions_for_user,
        warnings=(_WARNING_TEXT,),
    )


def _render_requirement(
    selection: RequirementEvidenceSelection, facts_by_id: Mapping[str, EvidenceFact]
) -> RequirementMatch:
    coverage: Coverage
    if selection.evidence_fact_ids:
        facts = [facts_by_id[fact_id] for fact_id in selection.evidence_fact_ids]
        explanation = _EXPLANATION_PREFIX + "\n".join(f"- {fact.statement}" for fact in facts)
        coverage = "partial"
    else:
        explanation = _NO_EVIDENCE_EXPLANATION
        coverage = "gap"

    return RequirementMatch(
        requirement=selection.requirement_quote,
        coverage=coverage,
        explanation=explanation,
        evidence_fact_ids=selection.evidence_fact_ids,
    )


def _render_resume_suggestion(fact: EvidenceFact) -> ResumeSuggestion:
    return ResumeSuggestion(
        target_section=_target_section_for(fact.category),
        suggested_text=fact.statement,
        evidence_fact_ids=(fact.id,),
    )


def _render_cover_letter_paragraph(fact: EvidenceFact) -> EvidenceParagraph:
    return EvidenceParagraph(
        text=_COVER_LETTER_PREFIX + fact.statement,
        evidence_fact_ids=(fact.id,),
    )


def _target_section_for(category: str) -> str:
    try:
        return _CATEGORY_TO_RESUME_SECTION[category]
    except KeyError as exc:
        raise AssertionError(
            f"категория факта {category!r} не входит в allow-list target_section"
        ) from exc
