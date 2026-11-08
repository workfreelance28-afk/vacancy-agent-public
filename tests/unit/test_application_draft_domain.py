from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from vacancy_agent.domain.application_draft import (
    ApplicationDraft,
    ApplicationDraftContent,
    EvidenceFact,
    EvidenceParagraph,
    MasterProfile,
    RequirementMatch,
    ResumeSuggestion,
)
from vacancy_agent.domain.stored_application_draft import StoredApplicationDraft


def _profile(**overrides: object) -> MasterProfile:
    defaults: dict[str, object] = {
        "version": "test-v1",
        "facts": [
            EvidenceFact(id="skill.python", category="skill", statement="Python.", source="t"),
            EvidenceFact(id="skill.sql", category="skill", statement="SQL.", source="t"),
        ],
    }
    defaults.update(overrides)
    return MasterProfile(**defaults)


def _requirement(**overrides: object) -> RequirementMatch:
    defaults: dict[str, object] = {
        "requirement": "Python",
        "coverage": "full",
        "explanation": "Есть опыт",
        "evidence_fact_ids": ["skill.python"],
    }
    defaults.update(overrides)
    return RequirementMatch(**defaults)


def _content(**overrides: object) -> ApplicationDraftContent:
    defaults: dict[str, object] = {
        "match_summary": "Хорошее соответствие.",
        "requirements": [_requirement()],
        "resume_suggestions": [],
        "cover_letter_paragraphs": [
            EvidenceParagraph(text="Абзац письма.", evidence_fact_ids=["skill.python"])
        ],
        "gaps": [],
        "questions_for_user": [],
        "warnings": [],
    }
    defaults.update(overrides)
    return ApplicationDraftContent(**defaults)


# --- MasterProfile / EvidenceFact ---


def test_master_profile_accepts_unique_nonempty_facts() -> None:
    profile = _profile()
    assert len(profile.facts) == 2


def test_master_profile_facts_cannot_be_mutated_after_validation() -> None:
    """Найдено ревью TASK-011: `frozen=True` запрещает только переприсвоение
    атрибута, не мутацию объекта, на который он ссылается — на `list` это
    оставляло обходной путь (`profile.facts.append(...)`) вокруг уже
    пройденных инвариантов (`test_master_profile_rejects_duplicate_fact_ids`
    и т.п.). Списочные поля объявлены как `tuple`, у которого нет `.append`."""

    profile = _profile()

    assert isinstance(profile.facts, tuple)
    assert not hasattr(profile.facts, "append")


def test_requirement_match_evidence_fact_ids_cannot_be_mutated() -> None:
    requirement = _requirement()

    assert isinstance(requirement.evidence_fact_ids, tuple)
    assert not hasattr(requirement.evidence_fact_ids, "append")


def test_content_list_fields_cannot_be_mutated() -> None:
    content = _content()

    assert isinstance(content.requirements, tuple)
    assert isinstance(content.resume_suggestions, tuple)
    assert isinstance(content.cover_letter_paragraphs, tuple)
    assert isinstance(content.gaps, tuple)
    assert isinstance(content.questions_for_user, tuple)
    assert isinstance(content.warnings, tuple)


def test_master_profile_rejects_duplicate_fact_ids() -> None:
    fact = EvidenceFact(id="dup", category="skill", statement="A.", source="t")
    with pytest.raises(ValidationError):
        MasterProfile(version="v1", facts=[fact, fact])


def test_master_profile_rejects_empty_facts_list() -> None:
    with pytest.raises(ValidationError):
        MasterProfile(version="v1", facts=[])


def test_evidence_fact_rejects_whitespace_only_statement() -> None:
    with pytest.raises(ValidationError):
        EvidenceFact(id="x", category="skill", statement="   ", source="t")


def test_evidence_fact_strips_surrounding_whitespace() -> None:
    fact = EvidenceFact(id="  x  ", category="skill", statement="  Python.  ", source="t")
    assert fact.id == "x"
    assert fact.statement == "Python."


def test_evidence_fact_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        EvidenceFact(id="x", category="skill", statement="A.", source="t", extra="nope")


# --- RequirementMatch coverage/evidence rule ---


def test_requirement_full_without_evidence_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RequirementMatch(
            requirement="Python", coverage="full", explanation="x", evidence_fact_ids=[]
        )


def test_requirement_partial_without_evidence_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RequirementMatch(
            requirement="Python", coverage="partial", explanation="x", evidence_fact_ids=[]
        )


def test_requirement_gap_with_evidence_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RequirementMatch(
            requirement="Rust",
            coverage="gap",
            explanation="x",
            evidence_fact_ids=["skill.python"],
        )


def test_requirement_gap_without_evidence_is_accepted() -> None:
    requirement = RequirementMatch(
        requirement="Rust", coverage="gap", explanation="не подтверждено", evidence_fact_ids=[]
    )
    assert requirement.coverage == "gap"


def test_requirement_rejects_duplicate_evidence_ids() -> None:
    with pytest.raises(ValidationError):
        RequirementMatch(
            requirement="Python",
            coverage="full",
            explanation="x",
            evidence_fact_ids=["skill.python", "skill.python"],
        )


def test_resume_suggestion_requires_at_least_one_evidence_id() -> None:
    with pytest.raises(ValidationError):
        ResumeSuggestion(target_section="Опыт", suggested_text="x", evidence_fact_ids=[])


def test_evidence_paragraph_requires_at_least_one_evidence_id() -> None:
    with pytest.raises(ValidationError):
        EvidenceParagraph(text="x", evidence_fact_ids=[])


# --- ApplicationDraftContent bounds ---


def test_content_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        ApplicationDraftContent(
            match_summary="x",
            requirements=[_requirement()],
            resume_suggestions=[],
            cover_letter_paragraphs=[
                EvidenceParagraph(text="p", evidence_fact_ids=["skill.python"])
            ],
            gaps=[],
            questions_for_user=[],
            warnings=[],
            unexpected="nope",
        )


def test_content_rejects_empty_requirements_list() -> None:
    with pytest.raises(ValidationError):
        _content(requirements=[])


def test_content_rejects_more_than_fifty_requirements() -> None:
    with pytest.raises(ValidationError):
        _content(requirements=[_requirement() for _ in range(51)])


def test_content_rejects_whitespace_only_gap_string() -> None:
    with pytest.raises(ValidationError):
        _content(gaps=["   "])


def test_content_accepts_valid_content() -> None:
    content = _content()
    assert content.match_summary == "Хорошее соответствие."


def test_content_rejects_match_summary_longer_than_domain_maximum() -> None:
    """ADR-040: `maxLength` больше не отправляется Qwen в JSON Schema
    (grammar-совместимость LM Studio) — верхняя граница длины строки
    теперь проверяется ТОЛЬКО здесь, после ответа модели, а не в схеме,
    которую видит модель. `match_summary` ограничен `max_length=4000`
    (`domain/application_draft.py`) — эта граница не ослаблена."""

    with pytest.raises(ValidationError):
        _content(match_summary="x" * 4001)


# --- envelope: ApplicationDraft ---


def test_application_draft_rejects_invalid_vacancy_id_pattern() -> None:
    with pytest.raises(ValidationError):
        ApplicationDraft(
            schema_version="1",
            vacancy_id=" leading-space-and space",
            profile_version="v1",
            content=_content(),
        )


def test_application_draft_rejects_vacancy_id_over_255_chars() -> None:
    with pytest.raises(ValidationError):
        ApplicationDraft(
            schema_version="1",
            vacancy_id="a" * 256,
            profile_version="v1",
            content=_content(),
        )


def test_application_draft_accepts_255_char_vacancy_id() -> None:
    draft = ApplicationDraft(
        schema_version="1",
        vacancy_id="a" * 255,
        profile_version="v1",
        content=_content(),
    )
    assert len(draft.vacancy_id) == 255


def test_application_draft_round_trips_through_json() -> None:
    draft = ApplicationDraft(
        schema_version="1", vacancy_id="superjob:1", profile_version="v1", content=_content()
    )
    restored = ApplicationDraft.model_validate_json(draft.model_dump_json())
    assert restored == draft


def test_stored_application_draft_rejects_naive_datetime() -> None:
    draft = ApplicationDraft(
        schema_version="1", vacancy_id="superjob:1", profile_version="v1", content=_content()
    )
    with pytest.raises(ValidationError):
        StoredApplicationDraft(
            draft=draft, model="m", prompt_version="1", created_at=datetime(2026, 8, 27, 12, 0, 0)
        )


def test_stored_application_draft_accepts_timezone_aware_datetime() -> None:
    draft = ApplicationDraft(
        schema_version="1", vacancy_id="superjob:1", profile_version="v1", content=_content()
    )
    stored = StoredApplicationDraft(
        draft=draft,
        model="m",
        prompt_version="1",
        created_at=datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC),
    )
    assert stored.created_at.tzinfo is not None
