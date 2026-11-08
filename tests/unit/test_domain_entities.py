from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from vacancy_agent.domain import (
    FilterResult,
    Gap,
    Strength,
    Vacancy,
    VacancyAssessment,
    VacancyStatus,
    passes_relevance_threshold,
)
from vacancy_agent.domain.status import MVP1_STATUSES


def _vacancy(**overrides: object) -> Vacancy:
    fields: dict[str, object] = {
        "hh_id": "12345",
        "title": "Python Developer",
        "url": "https://hh.ru/vacancy/12345",
        "published_at": datetime(2026, 8, 20, tzinfo=UTC),
        "fetched_at": datetime(2026, 8, 23, tzinfo=UTC),
    }
    fields.update(overrides)
    return Vacancy(**fields)


def test_vacancy_requires_hh_id_and_url() -> None:
    with pytest.raises(ValidationError):
        _vacancy(hh_id="")


def test_vacancy_defaults_to_empty_lists() -> None:
    vacancy = _vacancy()

    assert vacancy.key_skills == []
    assert vacancy.work_format == []


def test_vacancy_rejects_naive_published_at() -> None:
    with pytest.raises(ValidationError):
        _vacancy(published_at=datetime(2026, 8, 20))


def test_vacancy_rejects_naive_fetched_at() -> None:
    with pytest.raises(ValidationError):
        _vacancy(fetched_at=datetime(2026, 8, 23))


def test_filter_result_defaults() -> None:
    result = FilterResult(accepted=True)

    assert result.reasons == []
    assert result.rejected_by == []


def test_vacancy_assessment_score_bounds() -> None:
    with pytest.raises(ValidationError):
        VacancyAssessment(schema_version="1", score=101, relevant=True, recommendation="review")


def test_vacancy_assessment_rejects_unknown_recommendation() -> None:
    with pytest.raises(ValidationError):
        VacancyAssessment(schema_version="1", score=50, relevant=True, recommendation="maybe")


def test_vacancy_assessment_valid() -> None:
    assessment = VacancyAssessment(
        schema_version="1", score=84, relevant=True, recommendation="strong_match"
    )

    assert assessment.score == 84


# --- unbounded free-text length (threat-model review, 2026-08-29) ---
#
# `Strength.skill/evidence`, `Gap.skill`, `risks: list[str]` — свободный
# текст модели без `max_length` в `RESPONSE_JSON_SCHEMA` (JSON schema,
# отправляемая LM Studio, — `maxLength` там ломает grammar-компиляцию,
# ADR-040) — единственная граница длины теперь только здесь, в Pydantic.
# Компрометированная модель (prompt injection из недоверенного текста
# вакансии) без этой границы могла впихнуть неограниченно длинную строку,
# которая дословно сохраняется в SQLite и отдаётся через
# `GET /vacancies/{id}`.


def test_strength_rejects_skill_longer_than_200_chars() -> None:
    with pytest.raises(ValidationError):
        Strength(skill="x" * 201, evidence="Есть опыт.")


def test_strength_accepts_skill_at_exactly_200_chars() -> None:
    strength = Strength(skill="x" * 200, evidence="Есть опыт.")

    assert len(strength.skill) == 200


def test_strength_rejects_evidence_longer_than_1000_chars() -> None:
    with pytest.raises(ValidationError):
        Strength(skill="Python", evidence="x" * 1001)


def test_strength_accepts_evidence_at_exactly_1000_chars() -> None:
    strength = Strength(skill="Python", evidence="x" * 1000)

    assert len(strength.evidence) == 1000


def test_gap_rejects_skill_longer_than_200_chars() -> None:
    with pytest.raises(ValidationError):
        Gap(skill="x" * 201, importance="medium")


def test_vacancy_assessment_rejects_risk_longer_than_1000_chars() -> None:
    with pytest.raises(ValidationError):
        VacancyAssessment(
            schema_version="1",
            score=50,
            relevant=True,
            risks=["x" * 1001],
            recommendation="review",
        )


def test_vacancy_assessment_rejects_empty_string_risk() -> None:
    with pytest.raises(ValidationError):
        VacancyAssessment(
            schema_version="1", score=50, relevant=True, risks=[""], recommendation="review"
        )


def test_vacancy_assessment_accepts_risk_at_exactly_1000_chars() -> None:
    assessment = VacancyAssessment(
        schema_version="1",
        score=50,
        relevant=True,
        risks=["x" * 1000],
        recommendation="review",
    )

    assert len(assessment.risks[0]) == 1000


def test_passes_relevance_threshold_true_when_score_at_or_above_min() -> None:
    assessment = VacancyAssessment(
        schema_version="1", score=70, relevant=True, recommendation="review"
    )

    assert passes_relevance_threshold(assessment, min_score=70) is True


def test_passes_relevance_threshold_false_when_score_below_min() -> None:
    assessment = VacancyAssessment(
        schema_version="1", score=69, relevant=True, recommendation="review"
    )

    assert passes_relevance_threshold(assessment, min_score=70) is False


def test_passes_relevance_threshold_false_when_model_says_not_relevant() -> None:
    """Высокий score не переопределяет `relevant=False` — числовой порог
    дополняет решение модели, а не заменяет его (см. `domain/assessment.py`)."""

    assessment = VacancyAssessment(
        schema_version="1", score=95, relevant=False, recommendation="skip"
    )

    assert passes_relevance_threshold(assessment, min_score=70) is False


def test_mvp1_statuses_subset_of_full_enum() -> None:
    assert MVP1_STATUSES.issubset(set(VacancyStatus))
    assert VacancyStatus.NEW in MVP1_STATUSES
    assert VacancyStatus.OFFER not in MVP1_STATUSES
