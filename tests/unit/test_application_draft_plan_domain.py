import pytest
from pydantic import ValidationError

from vacancy_agent.domain.application_draft import EvidenceFact, MasterProfile
from vacancy_agent.domain.application_draft_errors import (
    EvidenceValidationError,
    RequirementQuoteError,
)
from vacancy_agent.domain.application_draft_plan import (
    ApplicationDraftPlan,
    RequirementEvidenceSelection,
    validate_plan,
)

_VACANCY_REQUIREMENTS = "Обязателен опыт Python и SQL. Приветствуется Docker."


def _profile(*fact_ids: str) -> MasterProfile:
    ids = fact_ids or ("skill.python", "skill.sql")
    return MasterProfile(
        version="test-v1",
        facts=[
            EvidenceFact(id=fid, category="skill", statement="x", source="t") for fid in ids
        ],
    )


def _selection(**overrides: object) -> RequirementEvidenceSelection:
    defaults: dict[str, object] = {
        "requirement_quote": "опыт Python",
        "evidence_fact_ids": ["skill.python"],
    }
    defaults.update(overrides)
    return RequirementEvidenceSelection(**defaults)


def _plan(**overrides: object) -> ApplicationDraftPlan:
    defaults: dict[str, object] = {
        "requirements": [_selection()],
        "resume_fact_ids": [],
        "cover_letter_fact_ids": ["skill.python"],
    }
    defaults.update(overrides)
    return ApplicationDraftPlan(**defaults)


# --- RequirementEvidenceSelection ---


def test_selection_accepts_valid_values() -> None:
    selection = _selection()
    assert selection.requirement_quote == "опыт Python"
    assert selection.evidence_fact_ids == ("skill.python",)


def test_selection_accepts_empty_evidence_ids() -> None:
    selection = _selection(evidence_fact_ids=[])
    assert selection.evidence_fact_ids == ()


def test_selection_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        RequirementEvidenceSelection(
            requirement_quote="x", evidence_fact_ids=[], unexpected="nope"
        )


def test_selection_rejects_empty_quote() -> None:
    with pytest.raises(ValidationError):
        _selection(requirement_quote="")


def test_selection_rejects_whitespace_only_quote() -> None:
    with pytest.raises(ValidationError):
        _selection(requirement_quote="   ")


def test_selection_rejects_more_than_three_evidence_ids() -> None:
    with pytest.raises(ValidationError):
        _selection(evidence_fact_ids=["a", "b", "c", "d"])


def test_selection_accepts_exactly_three_evidence_ids() -> None:
    selection = _selection(evidence_fact_ids=["a", "b", "c"])
    assert len(selection.evidence_fact_ids) == 3


def test_selection_rejects_duplicate_evidence_ids() -> None:
    with pytest.raises(ValidationError):
        _selection(evidence_fact_ids=["skill.python", "skill.python"])


def test_selection_evidence_fact_ids_cannot_be_mutated() -> None:
    selection = _selection()
    assert isinstance(selection.evidence_fact_ids, tuple)
    assert not hasattr(selection.evidence_fact_ids, "append")


# --- ApplicationDraftPlan ---


def test_plan_accepts_valid_values() -> None:
    plan = _plan()
    assert len(plan.requirements) == 1
    assert plan.cover_letter_fact_ids == ("skill.python",)


def test_plan_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        ApplicationDraftPlan(
            requirements=[_selection()],
            resume_fact_ids=[],
            cover_letter_fact_ids=["skill.python"],
            unexpected="nope",
        )


def test_plan_rejects_empty_requirements_list() -> None:
    with pytest.raises(ValidationError):
        _plan(requirements=[])


def test_plan_rejects_more_than_fifty_requirements() -> None:
    with pytest.raises(ValidationError):
        _plan(requirements=[_selection(requirement_quote=f"q{i}") for i in range(51)])


def test_plan_accepts_zero_resume_fact_ids() -> None:
    plan = _plan(resume_fact_ids=[])
    assert plan.resume_fact_ids == ()


def test_plan_rejects_more_than_twenty_resume_fact_ids() -> None:
    with pytest.raises(ValidationError):
        _plan(resume_fact_ids=[f"id.{i}" for i in range(21)])


def test_plan_rejects_duplicate_resume_fact_ids() -> None:
    with pytest.raises(ValidationError):
        _plan(resume_fact_ids=["skill.python", "skill.python"])


def test_plan_rejects_empty_cover_letter_fact_ids() -> None:
    """Спецификация TASK-012: `cover_letter_fact_ids` — 1..10, не 0..10."""

    with pytest.raises(ValidationError):
        _plan(cover_letter_fact_ids=[])


def test_plan_rejects_more_than_ten_cover_letter_fact_ids() -> None:
    with pytest.raises(ValidationError):
        _plan(cover_letter_fact_ids=[f"id.{i}" for i in range(11)])


def test_plan_rejects_duplicate_cover_letter_fact_ids() -> None:
    with pytest.raises(ValidationError):
        _plan(cover_letter_fact_ids=["skill.python", "skill.python"])


def test_plan_rejects_duplicate_requirement_quotes() -> None:
    with pytest.raises(ValidationError):
        _plan(requirements=[_selection(), _selection()])


def test_plan_accepts_distinct_requirement_quotes() -> None:
    plan = _plan(
        requirements=[
            _selection(requirement_quote="опыт Python"),
            _selection(requirement_quote="опыт SQL", evidence_fact_ids=["skill.sql"]),
        ]
    )
    assert len(plan.requirements) == 2


def test_plan_requirements_cannot_be_mutated() -> None:
    plan = _plan()
    assert isinstance(plan.requirements, tuple)
    assert not hasattr(plan.requirements, "append")


# --- validate_plan: exact-substring quote check ---


def test_validate_plan_accepts_quote_that_is_exact_substring() -> None:
    validate_plan(_plan(), _VACANCY_REQUIREMENTS, _profile())  # не должно поднимать


def test_validate_plan_rejects_paraphrased_quote() -> None:
    """`docs/TASK-012.md`: "пересказ или новая фраза отклоняется"."""

    plan = _plan(requirements=[_selection(requirement_quote="нужен опыт разработки на Python")])

    with pytest.raises(RequirementQuoteError):
        validate_plan(plan, _VACANCY_REQUIREMENTS, _profile())


def test_validate_plan_rejects_quote_not_present_at_all() -> None:
    plan = _plan(requirements=[_selection(requirement_quote="опыт Rust")])

    with pytest.raises(RequirementQuoteError):
        validate_plan(plan, _VACANCY_REQUIREMENTS, _profile())


def test_validate_plan_accepts_quote_spanning_full_requirements_text() -> None:
    plan = _plan(requirements=[_selection(requirement_quote=_VACANCY_REQUIREMENTS)])

    validate_plan(plan, _VACANCY_REQUIREMENTS, _profile())  # не должно поднимать


def test_validate_plan_accepts_boundary_quote_from_truncated_prompt_view() -> None:
    """Codex review-fix [P2] «Обрезка создаёт несуществующую цитату»: живая
    находка — `vacancy.requirements` длиннее `_MAX_DESCRIPTION_CHARS`
    символов (например, 4053 при лимите 4000), модель видит в промпте
    только точный prefix (`integrations/application_draft_prompt.py:
    _truncate_requirements`), но выбирает цитату прямо на границе видимого
    окна. Такая цитата — реальная подстрока ПОЛНОГО `vacancy.requirements`,
    поэтому `validate_plan` обязан её принять, а не отклонить `502`, как
    отклонял бы старый `_truncate` с добавленным символом «…»."""

    from vacancy_agent.integrations.application_draft_prompt import _MAX_DESCRIPTION_CHARS

    full_requirements = "Обязателен опыт Python. " + "x" * (_MAX_DESCRIPTION_CHARS + 200)
    shown_to_model = full_requirements[:_MAX_DESCRIPTION_CHARS]
    boundary_quote = shown_to_model[-30:]

    plan = _plan(requirements=[_selection(requirement_quote=boundary_quote)])

    validate_plan(plan, full_requirements, _profile())  # не должно поднимать


# --- validate_plan: evidence ID existence check ---


def test_validate_plan_rejects_unknown_requirement_evidence_id() -> None:
    plan = _plan(requirements=[_selection(evidence_fact_ids=["does.not.exist"])])

    with pytest.raises(EvidenceValidationError):
        validate_plan(plan, _VACANCY_REQUIREMENTS, _profile())


def test_validate_plan_rejects_unknown_resume_fact_id() -> None:
    plan = _plan(resume_fact_ids=["does.not.exist"])

    with pytest.raises(EvidenceValidationError):
        validate_plan(plan, _VACANCY_REQUIREMENTS, _profile())


def test_validate_plan_rejects_unknown_cover_letter_fact_id() -> None:
    plan = _plan(cover_letter_fact_ids=["does.not.exist"])

    with pytest.raises(EvidenceValidationError):
        validate_plan(plan, _VACANCY_REQUIREMENTS, _profile())


def test_validate_plan_checks_quotes_before_evidence_ids() -> None:
    """`docs/TASK-012.md`, «Детерминированные проверки»: цитата — пункт 2,
    evidence ID — пункт 4. Оба нарушения присутствуют одновременно — должна
    сработать проверка цитаты первой."""

    plan = _plan(
        requirements=[
            _selection(
                requirement_quote="не существует в вакансии",
                evidence_fact_ids=["does.not.exist"],
            )
        ]
    )

    with pytest.raises(RequirementQuoteError):
        validate_plan(plan, _VACANCY_REQUIREMENTS, _profile())
