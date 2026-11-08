"""Golden dataset loading and fingerprinting (TASK-S2.4).

The dataset is owner-labelled, versioned JSONL
(`data/evaluation/vacancy_relevance_golden_v1.jsonl`,
`tests/unit/test_golden_dataset.py` enforces its contract/security
properties). This module only reads it — it never writes gold labels;
new cases are added by hand after an explicit owner decision
(`docs/TASK-S2.4.md`, «Claude не размечает их сам»).
"""

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from vacancy_agent.domain.vacancy import Vacancy

GoldDecision = Literal["accept", "borderline", "reject"]


class GoldLabel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    annotated_at: str
    annotated_by: str
    decision: GoldDecision
    rationale: str
    reason_code: str


class GoldenCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    dataset_version: int
    gold_label: GoldLabel
    provenance: dict[str, str]
    vacancy: Vacancy


class DuplicateCaseIdError(ValueError):
    """A dataset file has two records sharing the same `case_id`."""


def load_dataset(path: Path) -> list[GoldenCase]:
    """Parses and validates every record — the same contract
    `test_golden_dataset.py` checks, so a hand-edited or truncated file
    fails loudly here too, not just in CI."""

    records = [
        GoldenCase.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    _require_unique_case_ids(records)
    return records


def _require_unique_case_ids(records: list[GoldenCase]) -> None:
    seen: set[str] = set()
    for record in records:
        if record.case_id in seen:
            raise DuplicateCaseIdError(f"Duplicate case_id in dataset: {record.case_id!r}")
        seen.add(record.case_id)


def dataset_fingerprint(path: Path) -> str:
    """SHA-256 of the raw file bytes — identifies exactly which dataset
    content a run/compare was produced against (TASK-S2.4: gate must
    reject a run against an incomplete or unrelated dataset)."""

    return hashlib.sha256(path.read_bytes()).hexdigest()
