"""Per-case eval run artifact (TASK-S2.4) — written by `manual_run.py`
as JSONL (one `EvalRecord` per line), read by `compare.py`.

`RECOMMENDATION_TO_CLASS` is the single place mapping the model's
`VacancyAssessment.recommendation` onto the same three classes as the
golden dataset's `gold_label.decision` — `manual_run.py`/`metrics.py`
both use it so the mapping can never quietly drift between where a
prediction is produced and where it is scored."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from vacancy_agent.domain.assessment import VacancyAssessment
from vacancy_agent.evaluation.dataset import GoldDecision

RECOMMENDATION_TO_CLASS: dict[str, GoldDecision] = {
    "skip": "reject",
    "review": "borderline",
    "strong_match": "accept",
}

_TokenCount = int | Literal["unknown"]


class EvalUsage(BaseModel):
    """Token usage for one call. `"unknown"` (not `0`) when the server's
    response didn't include a `usage` object — TASK-S2.4 invariant:
    «отсутствующий usage помечается unknown, а не выдумывается»."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_tokens: _TokenCount = "unknown"
    completion_tokens: _TokenCount = "unknown"
    total_tokens: _TokenCount = "unknown"

    @classmethod
    def from_payload(cls, usage: Mapping[str, Any] | None) -> "EvalUsage":
        if usage is None:
            return cls()
        return cls(
            prompt_tokens=_int_or_unknown(usage.get("prompt_tokens")),
            completion_tokens=_int_or_unknown(usage.get("completion_tokens")),
            total_tokens=_int_or_unknown(usage.get("total_tokens")),
        )


def _int_or_unknown(value: object) -> _TokenCount:
    return value if isinstance(value, int) else "unknown"


class EvalCost(BaseModel):
    """Local LM Studio has no direct per-call API cost — `amount_usd=0.0`
    with a note, not a fabricated non-zero estimate (TASK-S2.4: «для
    local LM Studio прямая API-cost равна 0»)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    amount_usd: float | Literal["unknown"]
    note: str

    @classmethod
    def local_lm_studio(cls) -> "EvalCost":
        return cls(amount_usd=0.0, note="local LM Studio — no direct per-call API cost")


class EvalRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    dataset_fingerprint: str
    prompt_fingerprint: str
    schema_version: str
    model: str
    base_url: str
    temperature: float
    elapsed_seconds: float = Field(ge=0)
    usage: EvalUsage
    cost: EvalCost
    predicted_class: GoldDecision | None
    assessment: VacancyAssessment | None
    error: str | None
    created_at: datetime


def predicted_class_from_assessment(assessment: VacancyAssessment) -> GoldDecision:
    return RECOMMENDATION_TO_CLASS[assessment.recommendation]
