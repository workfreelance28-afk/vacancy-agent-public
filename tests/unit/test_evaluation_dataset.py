from datetime import UTC, datetime
from pathlib import Path

import pytest

from vacancy_agent.evaluation.dataset import (
    DuplicateCaseIdError,
    dataset_fingerprint,
    load_dataset,
)

_NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)


def _case_line(case_id: str, hh_id: str, decision: str = "reject") -> str:
    import json

    return json.dumps(
        {
            "case_id": case_id,
            "dataset_version": 1,
            "gold_label": {
                "annotated_at": "2026-09-16",
                "annotated_by": "owner",
                "decision": decision,
                "rationale": "Достаточно длинное обоснование для теста.",
                "reason_code": "test_reason",
            },
            "provenance": {"source_record": "human_annotation", "source_task": "TASK-S2.4"},
            "vacancy": {
                "hh_id": hh_id,
                "title": "AI Integration Engineer",
                "url": f"https://example.test/{hh_id}",
                "published_at": _NOW.isoformat(),
                "fetched_at": _NOW.isoformat(),
            },
        },
        ensure_ascii=False,
    )


def test_load_dataset_parses_valid_records(tmp_path: Path) -> None:
    path = tmp_path / "golden.jsonl"
    path.write_text(_case_line("vr-v1-001", "1") + "\n" + _case_line("vr-v1-002", "2") + "\n")

    records = load_dataset(path)

    assert [r.case_id for r in records] == ["vr-v1-001", "vr-v1-002"]
    assert records[0].gold_label.decision == "reject"


def test_load_dataset_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "golden.jsonl"
    path.write_text(_case_line("vr-v1-001", "1") + "\n\n\n")

    records = load_dataset(path)

    assert len(records) == 1


def test_load_dataset_rejects_duplicate_case_ids(tmp_path: Path) -> None:
    path = tmp_path / "golden.jsonl"
    path.write_text(_case_line("vr-v1-001", "1") + "\n" + _case_line("vr-v1-001", "2") + "\n")

    with pytest.raises(DuplicateCaseIdError):
        load_dataset(path)


def test_dataset_fingerprint_changes_with_content(tmp_path: Path) -> None:
    path_a = tmp_path / "a.jsonl"
    path_b = tmp_path / "b.jsonl"
    path_a.write_text(_case_line("vr-v1-001", "1") + "\n")
    path_b.write_text(_case_line("vr-v1-001", "1") + "\n" + _case_line("vr-v1-002", "2") + "\n")

    assert dataset_fingerprint(path_a) != dataset_fingerprint(path_b)


def test_dataset_fingerprint_is_stable_for_identical_content(tmp_path: Path) -> None:
    path_a = tmp_path / "a.jsonl"
    path_b = tmp_path / "b.jsonl"
    content = _case_line("vr-v1-001", "1") + "\n"
    path_a.write_text(content)
    path_b.write_text(content)

    assert dataset_fingerprint(path_a) == dataset_fingerprint(path_b)
