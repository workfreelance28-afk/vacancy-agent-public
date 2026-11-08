"""Unit-тесты чистой worker-orchestration логики (TASK-S2.2,
`application/scan_worker.py`) на fake `ScanJobQueue`/`ScanJobRunner` — без
реального PostgreSQL и без вызова какого-либо реального SuperJob/Habr/Qwen/
Telegram (см. `tests/integration/test_scan_queue_contract.py` для контракта
на реальной тестовой PostgreSQL)."""

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from vacancy_agent.application.scan_worker import compute_backoff_seconds, run_one_job
from vacancy_agent.domain.scan_job import ScanJob, ScanJobStatus
from vacancy_agent.domain.scan_job_errors import PermanentScanJobError, TransientScanJobError
from vacancy_agent.observability.metrics import MetricsRegistry

_NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


def _job(**overrides: object) -> ScanJob:
    fields: dict[str, object] = {
        "id": uuid4(),
        "idempotency_key": "key-1",
        "status": ScanJobStatus.RUNNING,
        "attempt": 1,
        "max_attempts": 3,
        "available_at": _NOW,
        "lease_expires_at": _NOW + timedelta(minutes=15),
        "created_at": _NOW,
        "updated_at": _NOW,
        "result": None,
        "error_code": None,
    }
    fields.update(overrides)
    return ScanJob(**fields)


class _FakeQueue:
    """Фейк `domain.ports.ScanJobQueue` — держит один job в памяти,
    записывает аргументы каждого `mark_*`-вызова для проверки в тестах."""

    def __init__(self, job_to_claim: ScanJob | None) -> None:
        self._job_to_claim = job_to_claim
        self.claim_calls: list[tuple[datetime, int]] = []
        self.succeeded: list[tuple[UUID, int, Mapping[str, object]]] = []
        self.retried: list[tuple[UUID, int, datetime, str]] = []
        self.failed: list[tuple[UUID, int, str]] = []

    def claim_one(self, *, now: datetime, lease_seconds: int) -> ScanJob | None:
        self.claim_calls.append((now, lease_seconds))
        return self._job_to_claim

    def mark_succeeded(self, job_id: UUID, *, attempt: int, result: Mapping[str, object]) -> None:
        self.succeeded.append((job_id, attempt, result))

    def mark_for_retry(
        self, job_id: UUID, *, attempt: int, available_at: datetime, error_code: str
    ) -> None:
        self.retried.append((job_id, attempt, available_at, error_code))

    def mark_failed(self, job_id: UUID, *, attempt: int, error_code: str) -> None:
        self.failed.append((job_id, attempt, error_code))

    def get(self, job_id: UUID) -> ScanJob | None:
        raise NotImplementedError

    def enqueue(self, idempotency_key: str) -> tuple[ScanJob, bool]:
        raise NotImplementedError

    def close(self) -> None:
        pass


async def test_run_one_job_returns_none_when_queue_has_nothing_to_claim() -> None:
    queue = _FakeQueue(job_to_claim=None)

    async def runner() -> Mapping[str, object]:
        pytest.fail("runner не должен вызываться, если claim_one вернул None")

    result = await run_one_job(queue, runner, lease_seconds=60, now=_NOW)

    assert result is None
    assert queue.claim_calls == [(_NOW, 60)]


async def test_run_one_job_marks_succeeded_with_runner_result() -> None:
    job = _job()
    queue = _FakeQueue(job_to_claim=job)

    async def runner() -> Mapping[str, object]:
        return {"fetched": 5, "notified": 1}

    result = await run_one_job(queue, runner, lease_seconds=60, now=_NOW)

    assert result is job
    assert queue.succeeded == [(job.id, job.attempt, {"fetched": 5, "notified": 1})]
    assert queue.retried == []
    assert queue.failed == []


async def test_run_one_job_retries_transient_failure_when_attempts_remain() -> None:
    job = _job(attempt=1, max_attempts=3)
    queue = _FakeQueue(job_to_claim=job)

    async def runner() -> Mapping[str, object]:
        raise TransientScanJobError("network_error")

    await run_one_job(queue, runner, lease_seconds=60, now=_NOW)

    assert queue.failed == []
    assert len(queue.retried) == 1
    retried_job_id, retried_attempt, available_at, error_code = queue.retried[0]
    assert retried_job_id == job.id
    assert retried_attempt == job.attempt
    assert error_code == "network_error"
    assert available_at == _NOW + timedelta(seconds=compute_backoff_seconds(job.attempt))


async def test_run_one_job_fails_transient_error_once_attempts_exhausted() -> None:
    job = _job(attempt=3, max_attempts=3)
    queue = _FakeQueue(job_to_claim=job)

    async def runner() -> Mapping[str, object]:
        raise TransientScanJobError("network_error")

    await run_one_job(queue, runner, lease_seconds=60, now=_NOW)

    assert queue.retried == []
    assert queue.failed == [(job.id, job.attempt, "network_error")]


async def test_run_one_job_fails_immediately_on_permanent_error_with_attempts_remaining() -> None:
    """Программные/validation ошибки не повторяются (TASK-S2.2), даже если
    попытки ещё остались — в отличие от transient."""

    job = _job(attempt=1, max_attempts=3)
    queue = _FakeQueue(job_to_claim=job)

    async def runner() -> Mapping[str, object]:
        raise PermanentScanJobError("validation_error")

    await run_one_job(queue, runner, lease_seconds=60, now=_NOW)

    assert queue.retried == []
    assert queue.failed == [(job.id, job.attempt, "validation_error")]


async def test_run_one_job_propagates_unexpected_runner_errors() -> None:
    """Ошибка, не классифицированная runner'ом ни как transient, ни как
    permanent, не перехватывается здесь — job остаётся `running` до
    истечения lease (тот же fail-closed путь, что и краш процесса worker'а,
    TASK-S2.2 критерий №4)."""

    job = _job()
    queue = _FakeQueue(job_to_claim=job)

    async def runner() -> Mapping[str, object]:
        raise RuntimeError("bug")

    with pytest.raises(RuntimeError, match="bug"):
        await run_one_job(queue, runner, lease_seconds=60, now=_NOW)

    assert queue.succeeded == []
    assert queue.retried == []
    assert queue.failed == []


@pytest.mark.parametrize(
    ("attempt", "expected_seconds"),
    [
        (1, 30),
        (2, 60),
        (3, 120),
        (4, 240),
        (10, 900),  # верхняя граница (15 минут), не 30 * 2**9
    ],
)
def test_compute_backoff_seconds_is_exponential_with_a_cap(
    attempt: int, expected_seconds: int
) -> None:
    assert compute_backoff_seconds(attempt) == expected_seconds


# --- TASK-S2.5: structured logging + metrics ---


async def test_run_one_job_logs_claimed_event_with_job_id(caplog: pytest.LogCaptureFixture) -> None:
    job = _job()
    queue = _FakeQueue(job_to_claim=job)

    async def runner() -> Mapping[str, object]:
        return {}

    with caplog.at_level(logging.INFO):
        await run_one_job(queue, runner, lease_seconds=60, now=_NOW)

    claimed = _find_record(caplog, "scan_job.claimed")
    assert claimed.job_id == str(job.id)  # type: ignore[attr-defined]
    assert claimed.attempt == job.attempt  # type: ignore[attr-defined]
    assert claimed.max_attempts == job.max_attempts  # type: ignore[attr-defined]


async def test_run_one_job_logs_succeeded_event_with_job_id_and_duration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    job = _job()
    queue = _FakeQueue(job_to_claim=job)

    async def runner() -> Mapping[str, object]:
        return {"fetched": 1}

    with caplog.at_level(logging.INFO):
        await run_one_job(queue, runner, lease_seconds=60, now=_NOW)

    succeeded = _find_record(caplog, "scan_job.succeeded")
    assert succeeded.job_id == str(job.id)  # type: ignore[attr-defined]
    assert isinstance(succeeded.duration_seconds, float)  # type: ignore[attr-defined]


async def test_run_one_job_logs_retry_scheduled_event_with_job_id_and_error_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    job = _job(attempt=1, max_attempts=3)
    queue = _FakeQueue(job_to_claim=job)

    async def runner() -> Mapping[str, object]:
        raise TransientScanJobError("network_error")

    with caplog.at_level(logging.INFO):
        await run_one_job(queue, runner, lease_seconds=60, now=_NOW)

    retried = _find_record(caplog, "scan_job.retry_scheduled")
    assert retried.job_id == str(job.id)  # type: ignore[attr-defined]
    assert retried.error_code == "network_error"  # type: ignore[attr-defined]
    assert retried.retry_in_seconds == compute_backoff_seconds(job.attempt)  # type: ignore[attr-defined]


async def test_run_one_job_logs_failed_event_with_job_id_on_permanent_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    job = _job(attempt=1, max_attempts=3)
    queue = _FakeQueue(job_to_claim=job)

    async def runner() -> Mapping[str, object]:
        raise PermanentScanJobError("validation_error")

    with caplog.at_level(logging.INFO):
        await run_one_job(queue, runner, lease_seconds=60, now=_NOW)

    failed = _find_record(caplog, "scan_job.failed")
    assert failed.job_id == str(job.id)  # type: ignore[attr-defined]
    assert failed.error_code == "validation_error"  # type: ignore[attr-defined]
    assert failed.reason == "permanent"  # type: ignore[attr-defined]


async def test_run_one_job_records_metrics_for_success() -> None:
    job = _job()
    queue = _FakeQueue(job_to_claim=job)
    metrics = MetricsRegistry()

    async def runner() -> Mapping[str, object]:
        return {}

    await run_one_job(queue, runner, lease_seconds=60, now=_NOW, metrics=metrics)

    snapshot = metrics.snapshot()
    assert snapshot["counters"]["scan_jobs_total"] == {"outcome=succeeded": 1}
    assert snapshot["latencies"]["scan_job_attempt_duration_seconds"]["count"] == 1


async def test_run_one_job_records_metrics_for_retry_with_bounded_error_category() -> None:
    job = _job(attempt=1, max_attempts=3)
    queue = _FakeQueue(job_to_claim=job)
    metrics = MetricsRegistry()

    async def runner() -> Mapping[str, object]:
        raise TransientScanJobError("vacancy_processing_failed:qwen_failed=7,send_failed=0")

    await run_one_job(queue, runner, lease_seconds=60, now=_NOW, metrics=metrics)

    snapshot = metrics.snapshot()
    assert snapshot["counters"]["scan_jobs_total"] == {"outcome=retry_scheduled": 1}
    # The category is the bounded prefix, not the raw error_code (which
    # embeds live counters — TASK-S2.5, criterion #3: no unbounded labels).
    assert snapshot["counters"]["scan_job_errors_total"] == {
        "category=vacancy_processing_failed": 1
    }


async def test_run_one_job_records_metrics_for_final_failure() -> None:
    job = _job(attempt=3, max_attempts=3)
    queue = _FakeQueue(job_to_claim=job)
    metrics = MetricsRegistry()

    async def runner() -> Mapping[str, object]:
        raise TransientScanJobError("network_error")

    await run_one_job(queue, runner, lease_seconds=60, now=_NOW, metrics=metrics)

    snapshot = metrics.snapshot()
    assert snapshot["counters"]["scan_jobs_total"] == {"outcome=failed": 1}
    assert snapshot["counters"]["scan_job_errors_total"] == {"category=network_error": 1}


async def test_run_one_job_without_metrics_argument_does_not_raise() -> None:
    """`metrics=None` (default) — existing TASK-S2.2 callers/tests that
    never pass it keep working unchanged."""

    job = _job()
    queue = _FakeQueue(job_to_claim=job)

    async def runner() -> Mapping[str, object]:
        raise PermanentScanJobError("boom")

    result = await run_one_job(queue, runner, lease_seconds=60, now=_NOW)

    assert result is job


def _find_record(
    caplog: pytest.LogCaptureFixture, event: str
) -> logging.LogRecord:
    for record in caplog.records:
        if record.getMessage() == event:
            return record
    raise AssertionError(f"no log record with event={event!r} found in {caplog.records!r}")
