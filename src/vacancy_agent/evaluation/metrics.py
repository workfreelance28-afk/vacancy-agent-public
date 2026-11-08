"""Offline metric calculation (TASK-S2.4) — no network, no LLM. Pure
functions over a golden dataset and one already-produced `EvalRecord`
run; `compare.py` calls this for both baseline and candidate."""

from collections import Counter

from pydantic import BaseModel, ConfigDict

from vacancy_agent.evaluation.dataset import GoldDecision, GoldenCase
from vacancy_agent.evaluation.records import EvalRecord

_CLASSES: tuple[GoldDecision, ...] = ("accept", "borderline", "reject")


class ClassMetrics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    support: int
    precision: float
    recall: float
    f1: float


class Report(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    total_cases: int
    covered_cases: int
    invalid_cases: int
    coverage: float
    invalid_rate: float
    accuracy: float
    macro_f1: float
    confusion_matrix: dict[str, dict[str, int]]
    per_class: dict[str, ClassMetrics]
    latency_p50_seconds: float | None
    latency_p95_seconds: float | None
    total_tokens_known: int
    total_tokens_unknown_cases: int
    total_cost_usd: float | None

    @property
    def accept_recall(self) -> float:
        return self.per_class["accept"].recall


def compute_report(gold_cases: list[GoldenCase], records: dict[str, EvalRecord]) -> Report:
    """`records` is keyed by `case_id`. A dataset case with no matching
    record (an incomplete run — `gate.py` separately treats this as a hard
    failure, but the report should still be computable for debugging why)
    counts the same as an invalid/unparseable output: not covered."""

    total = len(gold_cases)
    covered_pairs: list[tuple[GoldDecision, GoldDecision]] = []
    invalid = 0
    latencies: list[float] = []
    known_tokens_sum = 0
    unknown_token_cases = 0
    known_cost_sum = 0.0
    any_unknown_cost = False

    for case in gold_cases:
        record = records.get(case.case_id)
        if record is None:
            invalid += 1
            continue
        latencies.append(record.elapsed_seconds)
        if record.usage.total_tokens == "unknown":
            unknown_token_cases += 1
        else:
            known_tokens_sum += record.usage.total_tokens
        if record.cost.amount_usd == "unknown":
            any_unknown_cost = True
        else:
            known_cost_sum += record.cost.amount_usd

        if record.predicted_class is None:
            invalid += 1
            continue
        covered_pairs.append((case.gold_label.decision, record.predicted_class))

    covered = len(covered_pairs)
    confusion = _confusion_matrix(covered_pairs)
    per_class = _per_class_metrics(covered_pairs, confusion)
    accuracy = (
        sum(1 for gold, pred in covered_pairs if gold == pred) / covered if covered else 0.0
    )
    macro_f1 = sum(m.f1 for m in per_class.values()) / len(_CLASSES)

    return Report(
        total_cases=total,
        covered_cases=covered,
        invalid_cases=invalid,
        coverage=covered / total if total else 0.0,
        invalid_rate=invalid / total if total else 0.0,
        accuracy=accuracy,
        macro_f1=macro_f1,
        confusion_matrix=confusion,
        per_class=per_class,
        latency_p50_seconds=_percentile(latencies, 0.50),
        latency_p95_seconds=_percentile(latencies, 0.95),
        total_tokens_known=known_tokens_sum,
        total_tokens_unknown_cases=unknown_token_cases,
        total_cost_usd=None if any_unknown_cost else known_cost_sum,
    )


def _confusion_matrix(pairs: list[tuple[GoldDecision, GoldDecision]]) -> dict[str, dict[str, int]]:
    matrix: dict[str, dict[str, int]] = {gold: dict.fromkeys(_CLASSES, 0) for gold in _CLASSES}
    for gold, predicted in pairs:
        matrix[gold][predicted] += 1
    return matrix


def _per_class_metrics(
    pairs: list[tuple[GoldDecision, GoldDecision]], confusion: dict[str, dict[str, int]]
) -> dict[str, ClassMetrics]:
    gold_counts = Counter(gold for gold, _ in pairs)
    predicted_counts = Counter(predicted for _, predicted in pairs)

    result: dict[str, ClassMetrics] = {}
    for cls in _CLASSES:
        true_positive = confusion[cls][cls]
        support = gold_counts.get(cls, 0)
        predicted_total = predicted_counts.get(cls, 0)
        precision = true_positive / predicted_total if predicted_total else 0.0
        recall = true_positive / support if support else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall) > 0
            else 0.0
        )
        result[cls] = ClassMetrics(support=support, precision=precision, recall=recall, f1=f1)
    return result


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    # Nearest-rank method — simple and deterministic, no interpolation
    # ambiguity to keep repeated `compare` runs byte-identical.
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]
