import json
from datetime import UTC, datetime
from pathlib import Path

from vacancy_agent.evaluation.compare import compare

_NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
_CASE_IDS = ["c1", "c2", "c3", "c4"]
_GOLD = {"c1": "accept", "c2": "accept", "c3": "borderline", "c4": "reject"}


def _dataset_line(case_id: str) -> str:
    return json.dumps(
        {
            "case_id": case_id,
            "dataset_version": 1,
            "gold_label": {
                "annotated_at": "2026-09-16",
                "annotated_by": "owner",
                "decision": _GOLD[case_id],
                "rationale": "Достаточно длинное обоснование для теста.",
                "reason_code": "test_reason",
            },
            "provenance": {"source_record": "human_annotation", "source_task": "TASK-S2.4"},
            "vacancy": {
                "hh_id": case_id,
                "title": f"Vacancy {case_id}",
                "url": f"https://example.test/{case_id}",
                "published_at": _NOW.isoformat(),
                "fetched_at": _NOW.isoformat(),
            },
        },
        ensure_ascii=False,
    )


def _write_dataset(path: Path, case_ids: list[str] = _CASE_IDS) -> None:
    path.write_text("\n".join(_dataset_line(c) for c in case_ids) + "\n", encoding="utf-8")


def _assessment(recommendation: str) -> dict[str, object]:
    return {
        "schema_version": "1",
        "score": 80,
        "relevant": recommendation != "skip",
        "strengths": [],
        "gaps": [],
        "risks": [],
        "recommendation": recommendation,
    }


_RECOMMENDATION_FOR_CLASS = {"accept": "strong_match", "borderline": "review", "reject": "skip"}


def _record_line(
    case_id: str,
    predicted_class: str | None,
    *,
    dataset_fp: str,
    error: str | None = None,
) -> str:
    return json.dumps(
        {
            "case_id": case_id,
            "dataset_fingerprint": dataset_fp,
            "prompt_fingerprint": "pp",
            "schema_version": "1",
            "model": "test-model",
            "base_url": "http://localhost:1234/v1",
            "temperature": 0.0,
            "elapsed_seconds": 1.0,
            "usage": {
                "prompt_tokens": "unknown",
                "completion_tokens": "unknown",
                "total_tokens": "unknown",
            },
            "cost": {"amount_usd": 0.0, "note": "test"},
            "predicted_class": predicted_class,
            "assessment": (
                None
                if predicted_class is None
                else _assessment(_RECOMMENDATION_FOR_CLASS[predicted_class])
            ),
            "error": error,
            "created_at": _NOW.isoformat(),
        },
        ensure_ascii=False,
    )


def _write_run(
    path: Path,
    predictions: dict[str, str | None],
    *,
    dataset_fp: str,
    case_ids: list[str] = _CASE_IDS,
) -> None:
    path.write_text(
        "\n".join(
            _record_line(case_id, predictions[case_id], dataset_fp=dataset_fp)
            for case_id in case_ids
        )
        + "\n",
        encoding="utf-8",
    )


def test_compare_passes_when_candidate_matches_baseline_exactly(tmp_path: Path) -> None:
    from vacancy_agent.evaluation.dataset import dataset_fingerprint

    dataset_path = tmp_path / "golden.jsonl"
    _write_dataset(dataset_path)
    fp = dataset_fingerprint(dataset_path)

    perfect = {"c1": "accept", "c2": "accept", "c3": "borderline", "c4": "reject"}
    baseline_path = tmp_path / "baseline.jsonl"
    candidate_path = tmp_path / "candidate.jsonl"
    _write_run(baseline_path, perfect, dataset_fp=fp)
    _write_run(candidate_path, perfect, dataset_fp=fp)

    document, markdown, gate_result = compare(
        dataset_path=dataset_path, baseline_path=baseline_path, candidate_path=candidate_path
    )

    assert gate_result.passed is True
    assert document["gate"]["passed"] is True
    assert "PASS" in markdown


def test_compare_fails_on_an_intentional_regression(tmp_path: Path) -> None:
    from vacancy_agent.evaluation.dataset import dataset_fingerprint

    dataset_path = tmp_path / "golden.jsonl"
    _write_dataset(dataset_path)
    fp = dataset_fingerprint(dataset_path)

    baseline_predictions = {"c1": "accept", "c2": "accept", "c3": "borderline", "c4": "reject"}
    # Candidate misclassifies both accept cases as reject — a clear
    # accept-recall and macro-F1 regression, not a borderline float wobble.
    regressed_predictions = {"c1": "reject", "c2": "reject", "c3": "borderline", "c4": "reject"}

    baseline_path = tmp_path / "baseline.jsonl"
    candidate_path = tmp_path / "candidate.jsonl"
    _write_run(baseline_path, baseline_predictions, dataset_fp=fp)
    _write_run(candidate_path, regressed_predictions, dataset_fp=fp)

    document, markdown, gate_result = compare(
        dataset_path=dataset_path, baseline_path=baseline_path, candidate_path=candidate_path
    )

    assert gate_result.passed is False
    assert document["gate"]["passed"] is False
    assert "FAIL" in markdown
    assert any(
        "macro-F1" in reason or "accept-recall" in reason for reason in gate_result.reasons
    )


def test_compare_fails_when_candidate_is_missing_dataset_cases(tmp_path: Path) -> None:
    from vacancy_agent.evaluation.dataset import dataset_fingerprint

    dataset_path = tmp_path / "golden.jsonl"
    _write_dataset(dataset_path)
    fp = dataset_fingerprint(dataset_path)

    full = {"c1": "accept", "c2": "accept", "c3": "borderline", "c4": "reject"}
    baseline_path = tmp_path / "baseline.jsonl"
    candidate_path = tmp_path / "candidate.jsonl"
    _write_run(baseline_path, full, dataset_fp=fp)
    _write_run(candidate_path, full, dataset_fp=fp, case_ids=["c1", "c2", "c3"])  # missing c4

    _document, _markdown, gate_result = compare(
        dataset_path=dataset_path, baseline_path=baseline_path, candidate_path=candidate_path
    )

    assert gate_result.passed is False
    assert any("does not cover exactly" in reason for reason in gate_result.reasons)


def test_compare_fails_on_candidate_invalid_outputs(tmp_path: Path) -> None:
    from vacancy_agent.evaluation.dataset import dataset_fingerprint

    dataset_path = tmp_path / "golden.jsonl"
    _write_dataset(dataset_path)
    fp = dataset_fingerprint(dataset_path)

    full = {"c1": "accept", "c2": "accept", "c3": "borderline", "c4": "reject"}
    with_invalid: dict[str, str | None] = dict(full)
    with_invalid["c4"] = None

    baseline_path = tmp_path / "baseline.jsonl"
    candidate_path = tmp_path / "candidate.jsonl"
    _write_run(baseline_path, full, dataset_fp=fp)
    _write_run(candidate_path, with_invalid, dataset_fp=fp)

    _document, _markdown, gate_result = compare(
        dataset_path=dataset_path, baseline_path=baseline_path, candidate_path=candidate_path
    )

    assert gate_result.passed is False
    assert any("invalid" in reason for reason in gate_result.reasons)


def test_compare_fails_when_run_is_against_a_foreign_dataset(tmp_path: Path) -> None:
    dataset_path = tmp_path / "golden.jsonl"
    _write_dataset(dataset_path)

    full = {"c1": "accept", "c2": "accept", "c3": "borderline", "c4": "reject"}
    baseline_path = tmp_path / "baseline.jsonl"
    candidate_path = tmp_path / "candidate.jsonl"
    _write_run(baseline_path, full, dataset_fp="not-the-real-fingerprint")
    _write_run(candidate_path, full, dataset_fp="not-the-real-fingerprint")

    _document, _markdown, gate_result = compare(
        dataset_path=dataset_path, baseline_path=baseline_path, candidate_path=candidate_path
    )

    assert gate_result.passed is False
    assert any("fingerprint" in reason for reason in gate_result.reasons)


def test_compare_is_deterministic_across_repeated_runs(tmp_path: Path) -> None:
    from vacancy_agent.evaluation.dataset import dataset_fingerprint

    dataset_path = tmp_path / "golden.jsonl"
    _write_dataset(dataset_path)
    fp = dataset_fingerprint(dataset_path)

    predictions = {"c1": "accept", "c2": "accept", "c3": "borderline", "c4": "reject"}
    baseline_path = tmp_path / "baseline.jsonl"
    candidate_path = tmp_path / "candidate.jsonl"
    _write_run(baseline_path, predictions, dataset_fp=fp)
    _write_run(candidate_path, predictions, dataset_fp=fp)

    first_document, first_markdown, _ = compare(
        dataset_path=dataset_path, baseline_path=baseline_path, candidate_path=candidate_path
    )
    second_document, second_markdown, _ = compare(
        dataset_path=dataset_path, baseline_path=baseline_path, candidate_path=candidate_path
    )

    assert json.dumps(first_document, sort_keys=True) == json.dumps(second_document, sort_keys=True)
    assert first_markdown == second_markdown
