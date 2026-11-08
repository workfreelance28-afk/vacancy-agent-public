"""TASK-S2.5 incident test (criterion №5): a scan job fails transiently on
its first attempt, gets retried, and fails for good on its second (final)
attempt — without a real PostgreSQL, LM Studio, or any vacancy content.
Proves the incident is fully diagnosable from JSON log events and the
worker's `MetricsRegistry` snapshot alone: a consistent `job_id` across
claim/retry/failure, attempt numbers, per-attempt duration, and a short,
safe `error_code` — and that nothing sensitive (vacancy text, secrets)
ever appears in that output.

Uses a small in-memory fake `ScanJobQueue` that mimics the real state
machine's claim/attempt/lease bookkeeping closely enough to simulate two
real attempts on the same job — unlike `test_scan_worker.py`'s simpler
`_FakeQueue` (fixed `job_to_claim`, no state transitions), this one is
needed here specifically to let `attempt` legitimately advance between
the two `run_one_job` calls, the way `PostgresScanJobQueue.claim_one`
does (TASK-S2.2)."""

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from vacancy_agent.application.scan_worker import run_one_job
from vacancy_agent.domain.scan_job import ScanJob, ScanJobStatus
from vacancy_agent.domain.scan_job_errors import TransientScanJobError
from vacancy_agent.observability.json_logging import JsonFormatter
from vacancy_agent.observability.metrics import MetricsRegistry

_NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)

# A decoy resembling exactly the kind of data TASK-S2.5 forbids in
# logs/metrics — never fed into any log call in this test, only used to
# assert its absence from everything actually emitted.
_FORBIDDEN_STRINGS = (
    "AI-интегратор в сфере строительных смет",  # vacancy title
    "sk-live-should-never-appear",  # secret-shaped string
    "Bearer super-secret-token",
)


class _StatefulFakeQueue:
    """Enough of `domain.ports.ScanJobQueue` to let one job go through two
    real `claim_one` calls with `attempt` genuinely incrementing, and
    `mark_for_retry`/`mark_failed` genuinely changing its state — a
    faithful-enough state machine to make this a real incident, not a
    scripted sequence of fixed returns."""

    def __init__(self, job: ScanJob) -> None:
        self._job = job

    def claim_one(self, *, now: datetime, lease_seconds: int) -> ScanJob | None:
        if self._job.status not in (ScanJobStatus.QUEUED, ScanJobStatus.RETRY_WAIT):
            return None
        self._job = self._job.model_copy(
            update={
                "status": ScanJobStatus.RUNNING,
                "attempt": self._job.attempt + 1,
                "lease_expires_at": now + timedelta(seconds=lease_seconds),
            }
        )
        return self._job

    def mark_succeeded(self, job_id: UUID, *, attempt: int, result: Mapping[str, object]) -> None:
        self._job = self._job.model_copy(update={"status": ScanJobStatus.SUCCEEDED})

    def mark_for_retry(
        self, job_id: UUID, *, attempt: int, available_at: datetime, error_code: str
    ) -> None:
        self._job = self._job.model_copy(
            update={
                "status": ScanJobStatus.RETRY_WAIT,
                "available_at": available_at,
                "error_code": error_code,
            }
        )

    def mark_failed(self, job_id: UUID, *, attempt: int, error_code: str) -> None:
        self._job = self._job.model_copy(
            update={"status": ScanJobStatus.FAILED, "error_code": error_code}
        )

    def get(self, job_id: UUID) -> ScanJob | None:
        return self._job if self._job.id == job_id else None

    def enqueue(self, idempotency_key: str) -> tuple[ScanJob, bool]:
        raise NotImplementedError

    def close(self) -> None:
        pass


def _job() -> ScanJob:
    return ScanJob(
        id=uuid4(),
        idempotency_key="daily-scan-2026-09-20",
        status=ScanJobStatus.QUEUED,
        attempt=0,
        max_attempts=2,  # small on purpose: attempt 1 retries, attempt 2 is final
        available_at=_NOW,
        lease_expires_at=None,
        created_at=_NOW,
        updated_at=_NOW,
        result=None,
        error_code=None,
    )


async def test_transient_failure_then_retry_then_final_failure_is_fully_diagnosable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    job = _job()
    queue = _StatefulFakeQueue(job)
    metrics = MetricsRegistry()

    async def always_fails_with_network_error() -> Mapping[str, object]:
        raise TransientScanJobError("network_error")

    with caplog.at_level(logging.INFO, logger="vacancy_agent.scan_worker"):
        first_outcome = await run_one_job(
            queue, always_fails_with_network_error, lease_seconds=900, now=_NOW, metrics=metrics
        )
        second_outcome = await run_one_job(
            queue,
            always_fails_with_network_error,
            lease_seconds=900,
            now=_NOW + timedelta(seconds=45),
            metrics=metrics,
        )

    assert first_outcome is not None
    assert second_outcome is not None
    job_id = str(job.id)

    events = {record.getMessage(): record for record in caplog.records}
    assert set(events) == {
        "scan_job.claimed",
        "scan_job.retry_scheduled",
        "scan_job.failed",
    }, "claimed/retry_scheduled/failed should each appear exactly once across both attempts"
    claim_events = [r for r in caplog.records if r.getMessage() == "scan_job.claimed"]
    assert len(claim_events) == 2, "one claim per attempt"

    # 1. Every single event, from both attempts, carries the SAME job_id —
    #    this is what makes the incident traceable at all.
    for record in caplog.records:
        assert record.job_id == job_id  # type: ignore[attr-defined]

    # 2. Attempt numbers are visible and increase across the incident.
    attempts_seen = sorted(r.attempt for r in claim_events)  # type: ignore[attr-defined]
    assert attempts_seen == [1, 2]

    # 3. Retry carries a safe error_code and a concrete backoff.
    retry_record = events["scan_job.retry_scheduled"]
    assert retry_record.error_code == "network_error"  # type: ignore[attr-defined]
    assert retry_record.retry_in_seconds > 0  # type: ignore[attr-defined]
    assert isinstance(retry_record.duration_seconds, float)  # type: ignore[attr-defined]

    # 4. Final failure carries the same safe error_code, marks attempts as
    #    exhausted, and has its own duration.
    failed_record = events["scan_job.failed"]
    assert failed_record.error_code == "network_error"  # type: ignore[attr-defined]
    assert failed_record.reason == "attempts_exhausted"  # type: ignore[attr-defined]
    assert failed_record.attempt == 2  # type: ignore[attr-defined]
    assert isinstance(failed_record.duration_seconds, float)  # type: ignore[attr-defined]

    # 5. The job's own final state agrees with the logs.
    final_job = queue.get(job.id)
    assert final_job is not None
    assert final_job.status is ScanJobStatus.FAILED
    assert final_job.error_code == "network_error"

    # 6. Metrics alone (no logs at all) already tell the same story.
    snapshot = metrics.snapshot()
    assert snapshot["counters"]["scan_jobs_total"] == {
        "outcome=retry_scheduled": 1,
        "outcome=failed": 1,
    }
    assert snapshot["counters"]["scan_job_errors_total"] == {"category=network_error": 2}
    assert snapshot["latencies"]["scan_job_attempt_duration_seconds"]["count"] == 2

    # 7. Nothing sensitive anywhere in what was actually emitted — neither
    #    in the formatted JSON log lines nor in the metrics snapshot.
    formatter = JsonFormatter()
    formatted_logs = "\n".join(formatter.format(record) for record in caplog.records)
    metrics_text = json.dumps(snapshot, ensure_ascii=False)
    for forbidden in _FORBIDDEN_STRINGS:
        assert forbidden not in formatted_logs
        assert forbidden not in metrics_text
