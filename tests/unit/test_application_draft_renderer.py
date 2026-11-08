from vacancy_agent.domain.application_draft import EvidenceFact, MasterProfile
from vacancy_agent.domain.application_draft_plan import (
    ApplicationDraftPlan,
    RequirementEvidenceSelection,
)
from vacancy_agent.domain.application_draft_renderer import render_application_draft_content

_PYTHON_STATEMENT = "Работает с Python более пяти лет в продуктовых командах."
_SQL_STATEMENT = "Пишет SQL-запросы и проектирует схемы PostgreSQL."
_CROSS_FUNCTIONAL_QUOTE = "«кросс-функциональный»"


def _profile() -> MasterProfile:
    return MasterProfile(
        version="test-v1",
        facts=[
            EvidenceFact(
                id="skill.python", category="skill", statement=_PYTHON_STATEMENT, source="t"
            ),
            EvidenceFact(id="skill.sql", category="skill", statement=_SQL_STATEMENT, source="t"),
            EvidenceFact(
                id="employment.designer",
                category="employment",
                statement="Работал дизайнером с 2018 по 2020 год.",
                source="t",
            ),
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


# --- coverage: evidence -> partial (never full), no evidence -> gap ---


def test_requirement_with_evidence_gets_partial_coverage_never_full() -> None:
    content = render_application_draft_content(_plan(), _profile())

    assert content.requirements[0].coverage == "partial"


def test_requirement_without_evidence_gets_gap_coverage() -> None:
    plan = _plan(requirements=[_selection(evidence_fact_ids=[])])

    content = render_application_draft_content(plan, _profile())

    assert content.requirements[0].coverage == "gap"
    assert content.requirements[0].evidence_fact_ids == ()


# --- explanation: verbatim statements only, fixed prefix ---


def test_explanation_contains_verbatim_statement_of_selected_fact() -> None:
    content = render_application_draft_content(_plan(), _profile())

    assert _PYTHON_STATEMENT in content.requirements[0].explanation


def test_explanation_contains_all_selected_fact_statements() -> None:
    plan = _plan(requirements=[_selection(evidence_fact_ids=["skill.python", "skill.sql"])])

    content = render_application_draft_content(plan, _profile())

    assert _PYTHON_STATEMENT in content.requirements[0].explanation
    assert _SQL_STATEMENT in content.requirements[0].explanation


def test_explanation_fits_within_domain_bounds_for_three_max_length_facts() -> None:
    """`docs/TASK-012.md`: границы `ApplicationDraftContent` должны явно
    покрывать худший случай — три факта максимальной длины (2000 символов
    каждый) плюс фиксированный префикс, без молчаливой обрезки."""

    long_statement_a = "A" * 2000
    long_statement_b = "B" * 2000
    long_statement_c = "C" * 2000
    profile = MasterProfile(
        version="test-v1",
        facts=[
            EvidenceFact(id="f.a", category="skill", statement=long_statement_a, source="t"),
            EvidenceFact(id="f.b", category="skill", statement=long_statement_b, source="t"),
            EvidenceFact(id="f.c", category="skill", statement=long_statement_c, source="t"),
        ],
    )
    plan = _plan(
        requirements=[_selection(evidence_fact_ids=["f.a", "f.b", "f.c"])],
        cover_letter_fact_ids=["f.a"],
    )

    content = render_application_draft_content(plan, profile)

    explanation = content.requirements[0].explanation
    assert long_statement_a in explanation
    assert long_statement_b in explanation
    assert long_statement_c in explanation


def test_gap_requirement_explanation_is_fixed_text_not_empty() -> None:
    plan = _plan(requirements=[_selection(evidence_fact_ids=[])])

    content = render_application_draft_content(plan, _profile())

    assert len(content.requirements[0].explanation) > 0


# --- requirement text is the exact quote from the plan ---


def test_requirement_text_is_exact_plan_quote() -> None:
    plan = _plan(requirements=[_selection(requirement_quote="строго заданная цитата")])

    content = render_application_draft_content(plan, _profile())

    assert content.requirements[0].requirement == "строго заданная цитата"


# --- weakly-relevant evidence stays a verbatim fact, never a stronger claim ---


def test_cross_functional_wording_in_quote_never_appears_synthesized_in_output() -> None:
    """`docs/TASK-012.md`: неверно выбранный слабо релевантный факт остаётся
    дословным фактом — renderer никогда не подставляет слово из требования
    вместо statement, потому что explanation строится только из statement,
    не из requirement_quote."""

    plan = _plan(
        requirements=[
            _selection(
                requirement_quote=f"нужен {_CROSS_FUNCTIONAL_QUOTE} опыт",
                evidence_fact_ids=["employment.designer"],
            )
        ],
        cover_letter_fact_ids=["employment.designer"],
    )
    profile = MasterProfile(
        version="test-v1",
        facts=[
            EvidenceFact(
                id="employment.designer",
                category="employment",
                statement="Работал дизайнером с 2018 по 2020 год.",
                source="t",
            )
        ],
    )

    content = render_application_draft_content(plan, profile)

    assert _CROSS_FUNCTIONAL_QUOTE not in content.requirements[0].explanation
    assert "Работал дизайнером с 2018 по 2020 год." in content.requirements[0].explanation


# --- resume_suggestions: one per fact ID, verbatim, allow-listed section ---


def test_resume_suggestions_one_per_fact_id_with_verbatim_text() -> None:
    plan = _plan(resume_fact_ids=["skill.python", "skill.sql"])

    content = render_application_draft_content(plan, _profile())

    assert len(content.resume_suggestions) == 2
    assert content.resume_suggestions[0].suggested_text == _PYTHON_STATEMENT
    assert content.resume_suggestions[0].evidence_fact_ids == ("skill.python",)
    assert content.resume_suggestions[1].suggested_text == _SQL_STATEMENT


def test_resume_suggestion_target_section_from_allow_listed_category_mapping() -> None:
    plan = _plan(resume_fact_ids=["employment.designer"])

    content = render_application_draft_content(plan, _profile())

    assert content.resume_suggestions[0].target_section == "Опыт работы"


def test_resume_suggestions_preserve_plan_order() -> None:
    plan = _plan(resume_fact_ids=["skill.sql", "skill.python"])

    content = render_application_draft_content(plan, _profile())

    assert [s.suggested_text for s in content.resume_suggestions] == [
        _SQL_STATEMENT,
        _PYTHON_STATEMENT,
    ]


def test_no_resume_suggestions_when_plan_has_none() -> None:
    content = render_application_draft_content(_plan(resume_fact_ids=[]), _profile())

    assert content.resume_suggestions == ()


# --- cover_letter_paragraphs: one per fact ID, evidence-only, verbatim ---


def test_cover_letter_paragraphs_one_per_fact_id_with_verbatim_statement() -> None:
    plan = _plan(cover_letter_fact_ids=["skill.python", "skill.sql"])

    content = render_application_draft_content(plan, _profile())

    assert len(content.cover_letter_paragraphs) == 2
    assert _PYTHON_STATEMENT in content.cover_letter_paragraphs[0].text
    assert content.cover_letter_paragraphs[0].evidence_fact_ids == ("skill.python",)
    assert _SQL_STATEMENT in content.cover_letter_paragraphs[1].text


def test_cover_letter_paragraphs_preserve_plan_order() -> None:
    plan = _plan(cover_letter_fact_ids=["skill.sql", "skill.python"])

    content = render_application_draft_content(plan, _profile())

    assert _SQL_STATEMENT in content.cover_letter_paragraphs[0].text
    assert _PYTHON_STATEMENT in content.cover_letter_paragraphs[1].text


# --- gaps / questions_for_user: fixed templates from evidence-less quotes ---


def test_gaps_built_only_from_evidence_less_requirement_quotes() -> None:
    plan = _plan(
        requirements=[
            _selection(requirement_quote="опыт Python", evidence_fact_ids=["skill.python"]),
            _selection(requirement_quote="опыт Rust", evidence_fact_ids=[]),
        ]
    )

    content = render_application_draft_content(plan, _profile())

    assert len(content.gaps) == 1
    assert "опыт Rust" in content.gaps[0]
    assert "опыт Python" not in content.gaps[0]


def test_questions_for_user_built_only_from_evidence_less_requirement_quotes() -> None:
    plan = _plan(
        requirements=[
            _selection(requirement_quote="опыт Python", evidence_fact_ids=["skill.python"]),
            _selection(requirement_quote="опыт Rust", evidence_fact_ids=[]),
        ]
    )

    content = render_application_draft_content(plan, _profile())

    assert len(content.questions_for_user) == 1
    assert "опыт Rust" in content.questions_for_user[0]


def test_no_gaps_or_questions_when_every_requirement_has_evidence() -> None:
    content = render_application_draft_content(_plan(), _profile())

    assert content.gaps == ()
    assert content.questions_for_user == ()


# --- warnings: always present, fixed disclaimer ---


def test_warnings_always_contains_manual_review_disclaimer() -> None:
    content = render_application_draft_content(_plan(), _profile())

    assert len(content.warnings) == 1
    assert "ручн" in content.warnings[0].lower()


# --- match_summary: fixed template with counts ---


def test_match_summary_counts_requirements_with_and_without_evidence() -> None:
    plan = _plan(
        requirements=[
            _selection(requirement_quote="опыт Python", evidence_fact_ids=["skill.python"]),
            _selection(requirement_quote="опыт SQL", evidence_fact_ids=["skill.sql"]),
            _selection(requirement_quote="опыт Rust", evidence_fact_ids=[]),
        ]
    )

    content = render_application_draft_content(plan, _profile())

    assert "3" in content.match_summary
    assert "2" in content.match_summary
    assert "1" in content.match_summary


# --- determinism: same plan renders identically on repeated calls ---


def test_rendering_the_same_plan_twice_is_identical() -> None:
    plan = _plan(
        requirements=[
            _selection(requirement_quote="опыт Python", evidence_fact_ids=["skill.python"]),
            _selection(requirement_quote="опыт SQL", evidence_fact_ids=["skill.sql"]),
        ],
        resume_fact_ids=["skill.python", "skill.sql"],
        cover_letter_fact_ids=["skill.sql", "skill.python"],
    )

    first = render_application_draft_content(plan, _profile())
    second = render_application_draft_content(plan, _profile())

    assert first == second
