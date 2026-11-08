"""Offline baseline/candidate comparison and regression gate (TASK-S2.4).

Fully offline — no network, no LLM (safe to run in CI): reads the golden
dataset plus two already-produced `EvalRecord` JSONL artifacts, validates
each covers exactly the dataset's `case_id`s against the dataset it claims
to (`dataset_fingerprint`), computes a `Report` for each, and evaluates
the regression gate (`gate.py`). Writes a JSON artifact and a short
Markdown summary; exits non-zero when the gate fails, so CI can use the
exit code directly.
"""

import argparse
import json
import sys
from pathlib import Path

from vacancy_agent.evaluation.dataset import GoldenCase, dataset_fingerprint, load_dataset
from vacancy_agent.evaluation.gate import GateResult, evaluate_gate
from vacancy_agent.evaluation.metrics import Report, compute_report
from vacancy_agent.evaluation.records import EvalRecord


def load_records(path: Path) -> list[EvalRecord]:
    return [
        EvalRecord.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _records_by_case_id(records: list[EvalRecord]) -> dict[str, EvalRecord]:
    by_id: dict[str, EvalRecord] = {}
    for record in records:
        if record.case_id in by_id:
            raise ValueError(f"Duplicate case_id in run artifact: {record.case_id!r}")
        by_id[record.case_id] = record
    return by_id


def _run_dataset_fingerprint_matches(records: list[EvalRecord], expected: str) -> bool:
    """`True` only if every record in the run claims the current dataset's
    fingerprint — a run mixing fingerprints (which `manual_run.py` never
    produces itself, but an arbitrary file passed to `compare` might) is
    treated as not matching at all, not partially trusted."""

    return bool(records) and all(record.dataset_fingerprint == expected for record in records)


def compare(
    *, dataset_path: Path, baseline_path: Path, candidate_path: Path
) -> tuple[dict[str, object], str, GateResult]:
    cases = load_dataset(dataset_path)
    current_fingerprint = dataset_fingerprint(dataset_path)
    dataset_case_ids = frozenset(case.case_id for case in cases)

    baseline_records = load_records(baseline_path)
    candidate_records = load_records(candidate_path)
    baseline_case_ids = frozenset(r.case_id for r in baseline_records)
    candidate_case_ids = frozenset(r.case_id for r in candidate_records)

    baseline_matches = _run_dataset_fingerprint_matches(baseline_records, current_fingerprint)
    candidate_matches = _run_dataset_fingerprint_matches(candidate_records, current_fingerprint)

    baseline_report = compute_report(cases, _records_by_case_id(baseline_records))
    candidate_report = compute_report(cases, _records_by_case_id(candidate_records))

    gate_result = evaluate_gate(
        dataset_case_ids=dataset_case_ids,
        baseline_case_ids=baseline_case_ids,
        candidate_case_ids=candidate_case_ids,
        baseline_dataset_fingerprint_matches=baseline_matches,
        candidate_dataset_fingerprint_matches=candidate_matches,
        baseline_invalid_cases=baseline_report.invalid_cases,
        candidate_invalid_cases=candidate_report.invalid_cases,
        baseline_macro_f1=baseline_report.macro_f1,
        candidate_macro_f1=candidate_report.macro_f1,
        baseline_accept_recall=baseline_report.accept_recall,
        candidate_accept_recall=candidate_report.accept_recall,
    )

    document = {
        "dataset_fingerprint": current_fingerprint,
        "dataset_case_count": len(cases),
        "baseline": {
            "path": str(baseline_path),
            "dataset_fingerprint_matches": baseline_matches,
            "report": json.loads(baseline_report.model_dump_json()),
        },
        "candidate": {
            "path": str(candidate_path),
            "dataset_fingerprint_matches": candidate_matches,
            "report": json.loads(candidate_report.model_dump_json()),
        },
        "gate": {"passed": gate_result.passed, "reasons": list(gate_result.reasons)},
    }
    markdown = _render_markdown(cases, baseline_report, candidate_report, gate_result)
    return document, markdown, gate_result


def _render_markdown(
    cases: list[GoldenCase], baseline: Report, candidate: Report, gate_result: GateResult
) -> str:
    lines = [
        "# Evaluation report",
        "",
        f"Dataset: {len(cases)} cases.",
        "",
        "| Metric | Baseline | Candidate |",
        "|---|---:|---:|",
        f"| Coverage | {baseline.coverage:.2%} | {candidate.coverage:.2%} |",
        f"| Invalid rate | {baseline.invalid_rate:.2%} | {candidate.invalid_rate:.2%} |",
        f"| Accuracy | {baseline.accuracy:.4f} | {candidate.accuracy:.4f} |",
        f"| Macro-F1 | {baseline.macro_f1:.4f} | {candidate.macro_f1:.4f} |",
        f"| Accept recall | {baseline.accept_recall:.4f} | {candidate.accept_recall:.4f} |",
        f"| Latency p50 (s) | {_fmt(baseline.latency_p50_seconds)} | "
        f"{_fmt(candidate.latency_p50_seconds)} |",
        f"| Latency p95 (s) | {_fmt(baseline.latency_p95_seconds)} | "
        f"{_fmt(candidate.latency_p95_seconds)} |",
        f"| Tokens (known) | {baseline.total_tokens_known} | {candidate.total_tokens_known} |",
        f"| Cost (USD) | {_fmt(baseline.total_cost_usd)} | {_fmt(candidate.total_cost_usd)} |",
        "",
        "## Candidate per-class",
        "",
        "| Class | Support | Precision | Recall | F1 |",
        "|---|---:|---:|---:|---:|",
    ]
    for cls, metrics in candidate.per_class.items():
        lines.append(
            f"| {cls} | {metrics.support} | {metrics.precision:.4f} | "
            f"{metrics.recall:.4f} | {metrics.f1:.4f} |"
        )
    lines += [
        "",
        f"## Gate: {'PASS' if gate_result.passed else 'FAIL'}",
        "",
    ]
    if gate_result.reasons:
        lines += [f"- {reason}" for reason in gate_result.reasons]
    else:
        lines.append("No regressions found against the approved baseline.")
    return "\n".join(lines) + "\n"


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline baseline/candidate eval comparison.")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("data/evaluation/vacancy_relevance_golden_v1.jsonl"),
    )
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    document, markdown, gate_result = compare(
        dataset_path=args.dataset, baseline_path=args.baseline, candidate_path=args.candidate
    )
    args.output_json.write_text(
        json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    args.output_md.write_text(markdown, encoding="utf-8")

    print(markdown)
    sys.exit(0 if gate_result.passed else 1)


if __name__ == "__main__":
    main()
