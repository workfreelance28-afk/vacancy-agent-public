from datetime import UTC, datetime

import pytest

from vacancy_agent.domain.assessment import VacancyAssessment
from vacancy_agent.domain.vacancy import Vacancy
from vacancy_agent.evaluation.dataset import GoldDecision, GoldenCase, GoldLabel
from vacancy_agent.evaluation.metrics import compute_report
from vacancy_agent.evaluation.records import EvalCost, EvalRecord, EvalUsage

_NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)


def _case(case_id: str, decision: GoldDecision) -> GoldenCase:
    return GoldenCase(
        case_id=case_id,
        dataset_version=1,
        gold_label=GoldLabel(
            annotated_at="2026-09-16",
            annotated_by="owner",
            decision=decision,
            rationale="Достаточно длинное обоснование для теста корректности.",
            reason_code="test_reason",
        ),
        provenance={"source_record": "human_annotation", "source_task": "TASK-S2.4"},
        vacancy=Vacancy(
            hh_id=case_id,
            title="AI Integration Engineer",
            url=f"https://example.test/{case_id}",
            published_at=_NOW,
            fetched_at=_NOW,
        ),
    )


def _assessment(recommendation: str, score: int = 80) -> VacancyAssessment:
    return VacancyAssessment(
        schema_version="1",
        score=score,
        relevant=recommendation != "skip",
        recommendation=recommendation,  # type: ignore[arg-type]
    )


def _record(
    case_id: str,
    predicted_class: GoldDecision | None,
    *,
    recommendation: str = "review",
    elapsed_seconds: float = 1.0,
    total_tokens: int | str = "unknown",
    cost: float | str = 0.0,
    error: str | None = None,
) -> EvalRecord:
    return EvalRecord(
        case_id=case_id,
        dataset_fingerprint="fp",
        prompt_fingerprint="pp",
        schema_version="1",
        model="test-model",
        base_url="http://localhost:1234/v1",
        temperature=0.0,
        elapsed_seconds=elapsed_seconds,
        usage=EvalUsage(total_tokens=total_tokens),  # type: ignore[arg-type]
        cost=EvalCost(amount_usd=cost, note="test"),  # type: ignore[arg-type]
        predicted_class=predicted_class,
        assessment=None if predicted_class is None else _assessment(recommendation),
        error=error,
        created_at=_NOW,
    )


def test_compute_report_confusion_matrix_and_macro_f1() -> None:
    cases = [
        _case("c1", "accept"),
        _case("c2", "accept"),
        _case("c3", "borderline"),
        _case("c4", "reject"),
    ]
    records = {
        "c1": _record("c1", "accept"),
        "c2": _record("c2", "borderline"),
        "c3": _record("c3", "borderline"),
        "c4": _record("c4", "reject"),
    }

    report = compute_report(cases, records)

    assert report.total_cases == 4
    assert report.covered_cases == 4
    assert report.invalid_cases == 0
    assert report.coverage == pytest.approx(1.0)
    assert report.accuracy == pytest.approx(0.75)
    assert report.confusion_matrix["accept"] == {"accept": 1, "borderline": 1, "reject": 0}
    assert report.confusion_matrix["borderline"] == {"accept": 0, "borderline": 1, "reject": 0}
    assert report.confusion_matrix["reject"] == {"accept": 0, "borderline": 0, "reject": 1}
    assert report.per_class["accept"].precision == pytest.approx(1.0)
    assert report.per_class["accept"].recall == pytest.approx(0.5)
    assert report.per_class["accept"].f1 == pytest.approx(2 / 3)
    assert report.per_class["borderline"].precision == pytest.approx(0.5)
    assert report.per_class["borderline"].recall == pytest.approx(1.0)
    assert report.per_class["reject"].f1 == pytest.approx(1.0)
    assert report.macro_f1 == pytest.approx((2 / 3 + 2 / 3 + 1.0) / 3)
    assert report.accept_recall == pytest.approx(0.5)


def test_compute_report_counts_unparsed_output_as_invalid_not_covered() -> None:
    cases = [_case("c1", "accept"), _case("c2", "reject")]
    records = {
        "c1": _record("c1", "accept"),
        "c2": _record("c2", None, error="QwenResponseError"),
    }

    report = compute_report(cases, records)

    assert report.covered_cases == 1
    assert report.invalid_cases == 1
    assert report.coverage == pytest.approx(0.5)
    assert report.invalid_rate == pytest.approx(0.5)
    assert report.accuracy == pytest.approx(1.0)  # only over the 1 covered case


def test_compute_report_counts_missing_record_as_invalid() -> None:
    cases = [_case("c1", "accept"), _case("c2", "reject")]
    records = {"c1": _record("c1", "accept")}

    report = compute_report(cases, records)

    assert report.covered_cases == 1
    assert report.invalid_cases == 1


def test_compute_report_latency_percentiles() -> None:
    cases = [_case(f"c{i}", "reject") for i in range(1, 5)]
    records = {
        f"c{i}": _record(f"c{i}", "reject", elapsed_seconds=float(i))
        for i in range(1, 5)
    }

    report = compute_report(cases, records)

    assert report.latency_p50_seconds in {2.0, 3.0}
    assert report.latency_p95_seconds == 4.0


def test_compute_report_token_and_cost_aggregation_marks_unknown_honestly() -> None:
    cases = [_case("c1", "reject"), _case("c2", "reject")]
    records = {
        "c1": _record("c1", "reject", total_tokens=100, cost=0.0),
        "c2": _record("c2", "reject", total_tokens="unknown", cost=0.0),
    }

    report = compute_report(cases, records)

    assert report.total_tokens_known == 100
    assert report.total_tokens_unknown_cases == 1
    assert report.total_cost_usd == pytest.approx(0.0)


def test_compute_report_cost_is_unknown_if_any_case_is_unknown() -> None:
    cases = [_case("c1", "reject"), _case("c2", "reject")]
    records = {
        "c1": _record("c1", "reject", cost=0.0),
        "c2": _record("c2", "reject", cost="unknown"),
    }

    report = compute_report(cases, records)

    assert report.total_cost_usd is None


def test_compute_report_handles_empty_dataset() -> None:
    report = compute_report([], {})

    assert report.total_cases == 0
    assert report.coverage == 0.0
    assert report.accuracy == 0.0
    assert report.latency_p50_seconds is None
