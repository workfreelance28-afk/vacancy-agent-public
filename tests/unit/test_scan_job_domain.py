"""Unit-тесты `domain/scan_job.py` (TASK-S2.2) — тот же инвариант
timezone-aware datetime, что `StoredApplicationDraft`/`Vacancy`."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from vacancy_agent.domain.scan_job import ScanJob, ScanJobStatus


def _fields(**overrides: object) -> dict[str, object]:
    aware_now = datetime.now(UTC)
    fields: dict[str, object] = {
        "id": uuid4(),
        "idempotency_key": "key-1",
        "status": ScanJobStatus.QUEUED,
        "attempt": 0,
        "max_attempts": 3,
        "available_at": aware_now,
        "lease_expires_at": None,
        "created_at": aware_now,
        "updated_at": aware_now,
        "result": None,
        "error_code": None,
    }
    fields.update(overrides)
    return fields


def test_scan_job_accepts_timezone_aware_datetimes() -> None:
    job = ScanJob(**_fields())
    assert job.status is ScanJobStatus.QUEUED


_NAIVE_DATETIME = datetime(2026, 9, 20, 12, 0, 0)  # намеренно naive — тест проверяет отказ


@pytest.mark.parametrize("field", ["available_at", "created_at", "updated_at"])
def test_scan_job_rejects_naive_datetime(field: str) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ScanJob(**_fields(**{field: _NAIVE_DATETIME}))


def test_scan_job_rejects_naive_lease_expires_at_when_set() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ScanJob(**_fields(lease_expires_at=_NAIVE_DATETIME))


def test_scan_job_allows_lease_expires_at_none() -> None:
    job = ScanJob(**_fields(lease_expires_at=None))
    assert job.lease_expires_at is None
