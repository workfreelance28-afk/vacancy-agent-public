"""Regression gate (TASK-S2.4, «Quality gate»). Pure function, no I/O —
`compare.py` gathers the inputs from dataset/baseline/candidate and calls
this once; unit tests exercise it directly with synthetic values for both
a pass and an intentional-regression fail, without needing a real 50-case
run."""

from dataclasses import dataclass

# TASK-S2.4: "Quality gate проваливается при... падении macro-F1 более чем
# на 0.02 или accept-recall более чем на 0.05 от approved baseline."
MACRO_F1_MAX_DROP = 0.02
ACCEPT_RECALL_MAX_DROP = 0.05

# A drop exactly equal to the threshold must pass ("более чем", strictly
# more than) — plain float subtraction can overshoot the threshold by a
# representation error (e.g. `0.80 - 0.78 == 0.020000000000000018`),
# which would fail a case that is not actually a regression. This epsilon
# absorbs float noise without meaningfully loosening the real threshold.
_FLOAT_TOLERANCE = 1e-9


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reasons: tuple[str, ...]


def evaluate_gate(
    *,
    dataset_case_ids: frozenset[str],
    baseline_case_ids: frozenset[str],
    candidate_case_ids: frozenset[str],
    baseline_dataset_fingerprint_matches: bool,
    candidate_dataset_fingerprint_matches: bool,
    baseline_invalid_cases: int,
    candidate_invalid_cases: int,
    baseline_macro_f1: float,
    candidate_macro_f1: float,
    baseline_accept_recall: float,
    candidate_accept_recall: float,
) -> GateResult:
    reasons: list[str] = []

    _require_matching_dataset(
        reasons,
        "baseline",
        baseline_dataset_fingerprint_matches,
        baseline_case_ids,
        dataset_case_ids,
    )
    _require_matching_dataset(
        reasons,
        "candidate",
        candidate_dataset_fingerprint_matches,
        candidate_case_ids,
        dataset_case_ids,
    )

    if baseline_invalid_cases > 0:
        reasons.append(f"baseline run has {baseline_invalid_cases} invalid/unparseable output(s)")
    if candidate_invalid_cases > 0:
        reasons.append(f"candidate run has {candidate_invalid_cases} invalid/unparseable output(s)")

    macro_f1_drop = baseline_macro_f1 - candidate_macro_f1
    if macro_f1_drop > MACRO_F1_MAX_DROP + _FLOAT_TOLERANCE:
        reasons.append(
            f"macro-F1 dropped by {macro_f1_drop:.4f} (max allowed {MACRO_F1_MAX_DROP})"
        )

    accept_recall_drop = baseline_accept_recall - candidate_accept_recall
    if accept_recall_drop > ACCEPT_RECALL_MAX_DROP + _FLOAT_TOLERANCE:
        reasons.append(
            f"accept-recall dropped by {accept_recall_drop:.4f} "
            f"(max allowed {ACCEPT_RECALL_MAX_DROP})"
        )

    return GateResult(passed=not reasons, reasons=tuple(reasons))


def _require_matching_dataset(
    reasons: list[str],
    label: str,
    fingerprint_matches: bool,
    run_case_ids: frozenset[str],
    dataset_case_ids: frozenset[str],
) -> None:
    if not fingerprint_matches:
        reasons.append(f"{label} run's dataset_fingerprint does not match the current dataset")
    if run_case_ids != dataset_case_ids:
        missing = dataset_case_ids - run_case_ids
        extra = run_case_ids - dataset_case_ids
        reasons.append(
            f"{label} run does not cover exactly the dataset's case_ids "
            f"(missing={len(missing)}, extra={len(extra)})"
        )
