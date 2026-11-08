from vacancy_agent.evaluation.gate import (
    ACCEPT_RECALL_MAX_DROP,
    MACRO_F1_MAX_DROP,
    evaluate_gate,
)

_IDS = frozenset({"vr-v1-001", "vr-v1-002"})


def _passing_kwargs(**overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = {
        "dataset_case_ids": _IDS,
        "baseline_case_ids": _IDS,
        "candidate_case_ids": _IDS,
        "baseline_dataset_fingerprint_matches": True,
        "candidate_dataset_fingerprint_matches": True,
        "baseline_invalid_cases": 0,
        "candidate_invalid_cases": 0,
        "baseline_macro_f1": 0.80,
        "candidate_macro_f1": 0.80,
        "baseline_accept_recall": 0.90,
        "candidate_accept_recall": 0.90,
    }
    defaults.update(overrides)
    return defaults


def test_gate_passes_when_candidate_matches_baseline() -> None:
    result = evaluate_gate(**_passing_kwargs())  # type: ignore[arg-type]

    assert result.passed is True
    assert result.reasons == ()


def test_gate_passes_when_candidate_improves() -> None:
    result = evaluate_gate(
        **_passing_kwargs(candidate_macro_f1=0.95, candidate_accept_recall=1.0)  # type: ignore[arg-type]
    )

    assert result.passed is True


def test_gate_tolerates_macro_f1_drop_at_the_threshold() -> None:
    result = evaluate_gate(
        **_passing_kwargs(candidate_macro_f1=0.80 - MACRO_F1_MAX_DROP)  # type: ignore[arg-type]
    )

    assert result.passed is True


def test_gate_fails_when_macro_f1_drops_past_the_threshold() -> None:
    result = evaluate_gate(
        **_passing_kwargs(  # type: ignore[arg-type]
            candidate_macro_f1=0.80 - MACRO_F1_MAX_DROP - 0.001
        )
    )

    assert result.passed is False
    assert any("macro-F1" in reason for reason in result.reasons)


def test_gate_tolerates_accept_recall_drop_at_the_threshold() -> None:
    result = evaluate_gate(
        **_passing_kwargs(  # type: ignore[arg-type]
            candidate_accept_recall=0.90 - ACCEPT_RECALL_MAX_DROP
        )
    )

    assert result.passed is True


def test_gate_fails_when_accept_recall_drops_past_the_threshold() -> None:
    result = evaluate_gate(
        **_passing_kwargs(  # type: ignore[arg-type]
            candidate_accept_recall=0.90 - ACCEPT_RECALL_MAX_DROP - 0.001
        )
    )

    assert result.passed is False
    assert any("accept-recall" in reason for reason in result.reasons)


def test_gate_fails_on_candidate_invalid_outputs() -> None:
    result = evaluate_gate(**_passing_kwargs(candidate_invalid_cases=1))  # type: ignore[arg-type]

    assert result.passed is False
    assert any("invalid" in reason for reason in result.reasons)


def test_gate_fails_on_baseline_invalid_outputs() -> None:
    result = evaluate_gate(**_passing_kwargs(baseline_invalid_cases=2))  # type: ignore[arg-type]

    assert result.passed is False
    assert any("invalid" in reason for reason in result.reasons)


def test_gate_fails_when_candidate_dataset_fingerprint_does_not_match() -> None:
    result = evaluate_gate(
        **_passing_kwargs(candidate_dataset_fingerprint_matches=False)  # type: ignore[arg-type]
    )

    assert result.passed is False
    assert any("fingerprint" in reason for reason in result.reasons)


def test_gate_fails_when_candidate_is_missing_dataset_cases() -> None:
    result = evaluate_gate(
        **_passing_kwargs(candidate_case_ids=frozenset({"vr-v1-001"}))  # type: ignore[arg-type]
    )

    assert result.passed is False
    assert any("does not cover exactly" in reason for reason in result.reasons)


def test_gate_fails_when_baseline_is_missing_dataset_cases() -> None:
    result = evaluate_gate(
        **_passing_kwargs(baseline_case_ids=frozenset({"vr-v1-001"}))  # type: ignore[arg-type]
    )

    assert result.passed is False
    assert any("does not cover exactly" in reason for reason in result.reasons)


def test_gate_reports_multiple_reasons_at_once() -> None:
    result = evaluate_gate(
        **_passing_kwargs(  # type: ignore[arg-type]
            candidate_invalid_cases=1,
            candidate_macro_f1=0.0,
        )
    )

    assert result.passed is False
    assert len(result.reasons) >= 2
