import json
import re
from collections import Counter
from pathlib import Path

import pytest

from vacancy_agent.domain.vacancy import Vacancy

_DATASET_PATH = (
    Path(__file__).resolve().parents[2]
    / "data/evaluation/vacancy_relevance_golden_v1.jsonl"
)
pytestmark = pytest.mark.skipif(
    not _DATASET_PATH.is_file(), reason="Private owner-labelled dataset is not distributed"
)

_ALLOWED_DECISIONS = {"accept", "reject", "borderline"}
_FORBIDDEN_TOP_LEVEL_KEYS = {
    "assessment",
    "model",
    "qwen_score",
    "qwen_relevant",
    "recommendation",
    "score",
}
_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    re.compile(r"(?<!\d)(?:\+7|8)[\s()\-]*\d{3}[\s()\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)"),
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def _records() -> list[dict[str, object]]:
    return [json.loads(line) for line in _DATASET_PATH.read_text().splitlines() if line]


def test_golden_dataset_v1_has_valid_unique_human_labelled_cases() -> None:
    records = _records()

    assert 50 <= len(records) <= 100
    assert len({record["case_id"] for record in records}) == len(records)
    assert len({record["vacancy"]["hh_id"] for record in records}) == len(records)

    for record in records:
        assert record["dataset_version"] == 1
        assert re.fullmatch(r"vr-v1-\d{3}", record["case_id"])
        assert not (_FORBIDDEN_TOP_LEVEL_KEYS & record.keys())
        Vacancy.model_validate(record["vacancy"])

        label = record["gold_label"]
        assert label["decision"] in _ALLOWED_DECISIONS
        assert label["annotated_by"] == "owner"
        assert label["reason_code"]
        assert len(label["rationale"]) >= 20
        assert record["provenance"]["source_task"] in {"TASK-006", "TASK-015", "TASK-S2.4"}


def test_golden_dataset_v1_keeps_initial_label_coverage() -> None:
    decisions = Counter(record["gold_label"]["decision"] for record in _records())

    assert decisions["accept"] >= 2
    assert decisions["reject"] >= 4
    assert decisions["borderline"] >= 2


def test_golden_dataset_v1_contains_no_sensitive_looking_values() -> None:
    serialized = _DATASET_PATH.read_text(encoding="utf-8")

    assert not any(pattern.search(serialized) for pattern in _SENSITIVE_PATTERNS)
