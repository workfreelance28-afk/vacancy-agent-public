"""Durable scan job (TASK-S2.2): состояние одной фоновой job'ы для
существующего `application.wiring.run_scan_once`, хранимой в PostgreSQL
(`persistence/postgres_scan_queue.py`).

Отдельный небольшой модуль, а не поле внутри `application/scan_worker.py`,
по той же причине, что `stored_vacancy.py`/`stored_application_draft.py`:
и `application/scan_worker.py` (orchestration, тестируется на fake handler),
и HTTP-слой (`api/schemas.py`), и persistence-адаптер должны ссылаться на
один и тот же контракт состояния, не зависящий друг от друга."""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator


class ScanJobStatus(StrEnum):
    """`queued -> running -> succeeded`; временный сбой — `retry_wait ->
    running`; исчерпание попыток или permanent-ошибка — `failed`
    (TASK-S2.2)."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"


class ScanJob(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    idempotency_key: str
    status: ScanJobStatus
    attempt: int
    max_attempts: int
    available_at: datetime
    lease_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    result: dict[str, object] | None
    error_code: str | None

    @field_validator("available_at", "created_at", "updated_at")
    @classmethod
    def _require_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("datetime должен быть timezone-aware (содержать tzinfo)")
        return value

    @field_validator("lease_expires_at")
    @classmethod
    def _require_timezone_aware_if_set(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("datetime должен быть timezone-aware (содержать tzinfo)")
        return value
