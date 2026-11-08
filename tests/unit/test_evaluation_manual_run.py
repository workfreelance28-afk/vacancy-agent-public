import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from vacancy_agent.evaluation import manual_run
from vacancy_agent.evaluation.compare import _records_by_case_id
from vacancy_agent.evaluation.dataset import dataset_fingerprint, load_dataset
from vacancy_agent.evaluation.prompt_fingerprint import prompt_fingerprint
from vacancy_agent.evaluation.records import EvalRecord

_NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)

_VALID_ASSESSMENT = {
    "score": 82,
    "relevant": True,
    "strengths": [],
    "gaps": [],
    "risks": [],
    "recommendation": "review",
}


def _dataset_line(case_id: str, hh_id: str) -> str:
    return json.dumps(
        {
            "case_id": case_id,
            "dataset_version": 1,
            "gold_label": {
                "annotated_at": "2026-09-16",
                "annotated_by": "owner",
                "decision": "borderline",
                "rationale": "Достаточно длинное обоснование для теста.",
                "reason_code": "test_reason",
            },
            "provenance": {"source_record": "human_annotation", "source_task": "TASK-S2.4"},
            "vacancy": {
                "hh_id": hh_id,
                "title": f"Vacancy {hh_id}",
                "url": f"https://example.test/{hh_id}",
                "published_at": _NOW.isoformat(),
                "fetched_at": _NOW.isoformat(),
            },
        },
        ensure_ascii=False,
    )


def _write_dataset(path: Path, case_ids: list[str]) -> None:
    path.write_text(
        "\n".join(_dataset_line(case_id, case_id) for case_id in case_ids) + "\n",
        encoding="utf-8",
    )


def _chat_response(content_obj: object) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(content_obj, ensure_ascii=False)}}]},
    )


def _run_kwargs(dataset_path: Path, output_path: Path, transport: httpx.BaseTransport) -> dict:
    return {
        "dataset_path": dataset_path,
        "output_path": output_path,
        "model": "test-model",
        "base_url": "http://localhost:1234/v1",
        "api_key": "lm-studio",
        "request_timeout_seconds": 1.0,
        "max_retries": 0,
        "retry_backoff_seconds": 0.001,
        "transport": transport,
    }


def _read_records(path: Path) -> list[EvalRecord]:
    return [
        EvalRecord.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_run_writes_one_record_per_case(tmp_path: Path) -> None:
    dataset_path = tmp_path / "golden.jsonl"
    _write_dataset(dataset_path, ["c1", "c2"])
    output_path = tmp_path / "run.jsonl"

    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(_VALID_ASSESSMENT)

    manual_run.run(**_run_kwargs(dataset_path, output_path, httpx.MockTransport(handler)))

    records = _read_records(output_path)
    assert {r.case_id for r in records} == {"c1", "c2"}
    assert all(r.predicted_class == "borderline" for r in records)
    assert all(r.dataset_fingerprint == dataset_fingerprint(dataset_path) for r in records)
    assert all(r.prompt_fingerprint == prompt_fingerprint() for r in records)


def test_run_resumes_without_recalling_the_model_for_done_cases(tmp_path: Path) -> None:
    dataset_path = tmp_path / "golden.jsonl"
    _write_dataset(dataset_path, ["c1", "c2"])
    output_path = tmp_path / "run.jsonl"
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["messages"][1]["content"])
        return _chat_response(_VALID_ASSESSMENT)

    manual_run.run(**_run_kwargs(dataset_path, output_path, httpx.MockTransport(handler)))
    assert len(calls) == 2

    manual_run.run(**_run_kwargs(dataset_path, output_path, httpx.MockTransport(handler)))

    assert len(calls) == 2  # no new calls — both cases already done
    records = _read_records(output_path)
    assert len(records) == 2  # not duplicated


def test_run_retries_a_previously_errored_case(tmp_path: Path) -> None:
    dataset_path = tmp_path / "golden.jsonl"
    _write_dataset(dataset_path, ["c1"])
    output_path = tmp_path / "run.jsonl"

    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json{{"}}]})

    manual_run.run(**_run_kwargs(dataset_path, output_path, httpx.MockTransport(failing_handler)))
    first_records = _read_records(output_path)
    assert len(first_records) == 1
    assert first_records[0].error is not None
    assert first_records[0].predicted_class is None

    def succeeding_handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(_VALID_ASSESSMENT)

    manual_run.run(
        **_run_kwargs(dataset_path, output_path, httpx.MockTransport(succeeding_handler))
    )

    records = _read_records(output_path)
    # Exactly one row for "c1" — the retry's success replaces the earlier
    # error, it doesn't sit alongside it (regression test, TASK-S2.4
    # review-fix: a duplicate case_id here made
    # `compare._records_by_case_id` raise).
    assert len(records) == 1
    assert records[0].case_id == "c1"
    assert records[0].predicted_class == "borderline"
    assert records[0].error is None


def test_run_continues_past_a_failing_case_to_the_next_one(tmp_path: Path) -> None:
    dataset_path = tmp_path / "golden.jsonl"
    _write_dataset(dataset_path, ["bad", "good"])
    output_path = tmp_path / "run.jsonl"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "bad" in body["messages"][1]["content"]:
            return httpx.Response(200, json={"choices": [{"message": {"content": "not json{{"}}]})
        return _chat_response(_VALID_ASSESSMENT)

    manual_run.run(**_run_kwargs(dataset_path, output_path, httpx.MockTransport(handler)))

    records = {r.case_id: r for r in _read_records(output_path)}
    assert records["bad"].error is not None
    assert records["bad"].predicted_class is None
    assert records["good"].predicted_class == "borderline"


def test_run_refuses_to_resume_against_a_different_dataset(tmp_path: Path) -> None:
    dataset_path = tmp_path / "golden.jsonl"
    _write_dataset(dataset_path, ["c1"])
    output_path = tmp_path / "run.jsonl"

    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(_VALID_ASSESSMENT)

    manual_run.run(**_run_kwargs(dataset_path, output_path, httpx.MockTransport(handler)))

    _write_dataset(dataset_path, ["c1", "c2"])  # dataset content changed -> new fingerprint

    with pytest.raises(manual_run.IncompatibleResumeError):
        manual_run.run(**_run_kwargs(dataset_path, output_path, httpx.MockTransport(handler)))


def test_run_refuses_to_resume_against_a_different_model(tmp_path: Path) -> None:
    dataset_path = tmp_path / "golden.jsonl"
    _write_dataset(dataset_path, ["c1"])
    output_path = tmp_path / "run.jsonl"

    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(_VALID_ASSESSMENT)

    manual_run.run(**_run_kwargs(dataset_path, output_path, httpx.MockTransport(handler)))

    kwargs = _run_kwargs(dataset_path, output_path, httpx.MockTransport(handler))
    kwargs["model"] = "a-different-model"
    with pytest.raises(manual_run.IncompatibleResumeError):
        manual_run.run(**kwargs)


def test_run_respects_limit(tmp_path: Path) -> None:
    dataset_path = tmp_path / "golden.jsonl"
    _write_dataset(dataset_path, ["c1", "c2", "c3"])
    output_path = tmp_path / "run.jsonl"

    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(_VALID_ASSESSMENT)

    kwargs = _run_kwargs(dataset_path, output_path, httpx.MockTransport(handler))
    kwargs["limit"] = 2
    manual_run.run(**kwargs)

    records = _read_records(output_path)
    assert len(records) == 2


def test_run_creates_missing_output_parent_directory(tmp_path: Path) -> None:
    """Regression test, TASK-S2.4 review-fix: the documented command uses
    an `--output` under a directory (`data/evaluation/runs/...`) that
    doesn't exist yet — `run()` used to raise `FileNotFoundError` instead
    of creating it."""

    dataset_path = tmp_path / "golden.jsonl"
    _write_dataset(dataset_path, ["c1"])
    output_path = tmp_path / "runs" / "nested" / "run.jsonl"
    assert not output_path.parent.exists()

    def handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(_VALID_ASSESSMENT)

    manual_run.run(**_run_kwargs(dataset_path, output_path, httpx.MockTransport(handler)))

    assert output_path.exists()
    assert len(_read_records(output_path)) == 1


def test_run_output_after_error_and_retry_is_usable_by_compare(tmp_path: Path) -> None:
    """Regression test, TASK-S2.4 review-fix: a case that errors and is
    later retried successfully used to leave two rows for the same
    `case_id` in the artifact — `compare._records_by_case_id` raises
    `ValueError` on a duplicate, so that artifact could never be
    compared. Exercises the real function `compare.py` uses, not just
    `manual_run`'s own view of the file."""

    dataset_path = tmp_path / "golden.jsonl"
    _write_dataset(dataset_path, ["c1", "c2"])
    output_path = tmp_path / "run.jsonl"

    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json{{"}}]})

    manual_run.run(**_run_kwargs(dataset_path, output_path, httpx.MockTransport(failing_handler)))

    def succeeding_handler(request: httpx.Request) -> httpx.Response:
        return _chat_response(_VALID_ASSESSMENT)

    manual_run.run(
        **_run_kwargs(dataset_path, output_path, httpx.MockTransport(succeeding_handler))
    )

    records = _read_records(output_path)
    by_case_id = _records_by_case_id(records)  # must not raise

    assert set(by_case_id) == {"c1", "c2"}
    cases = load_dataset(dataset_path)
    assert {case.case_id for case in cases} == set(by_case_id)
