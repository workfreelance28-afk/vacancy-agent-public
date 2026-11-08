"""Durable PostgreSQL-очередь фонового scan (TASK-S2.2). Реализует
`domain.ports.ScanJobQueue` структурно — та же схема, что
`persistence/postgres_repository.py` (TASK-S2.1): один экземпляр — одно
`psycopg`-соединение на одну операцию, миграции применяются на `__init__`
через общий `persistence/postgres_migrator.py`.

Только PostgreSQL: `SELECT ... FOR UPDATE SKIP LOCKED` в `claim_one` —
стандартная PostgreSQL-идиома атомарного claim без блокировки других
воркеров на строках, уже взятых кем-то другим (в отличие от обычного
`FOR UPDATE`, который заставил бы второй worker ждать первого, а не
пропустить занятую строку и поискать следующую) — у SQLite нет эквивалента,
поэтому `persistence/factory.create_scan_job_queue` не пытается предложить
SQLite-реализацию (`docs/TASK-S2.2.md`, «Архитектурная граница»)."""

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self
from uuid import UUID, uuid4

import psycopg

from vacancy_agent.config.settings import DatabaseSettings
from vacancy_agent.domain.scan_job import ScanJob, ScanJobStatus
from vacancy_agent.persistence.postgres_migrator import apply_migrations

logger = logging.getLogger("vacancy_agent.persistence.postgres_scan_queue")

_MIGRATIONS_DIR = Path(__file__).parent / "postgres_migrations"
_POSTGRES_URL_PREFIX = "postgresql://"

_CLAIMABLE_STATUSES = (ScanJobStatus.QUEUED.value, ScanJobStatus.RETRY_WAIT.value)
_MAX_ATTEMPTS_DEFAULT = 3


class PostgresScanJobQueue:
    def __init__(self, dsn: str) -> None:
        self._connection = psycopg.connect(dsn, row_factory=psycopg.rows.dict_row)
        apply_migrations(self._connection, _MIGRATIONS_DIR)

    @classmethod
    def from_settings(cls, settings: DatabaseSettings) -> Self:
        """Ожидает уже провалидированный `postgresql://...` URL — реальный
        вызывающий код (`persistence/factory.create_scan_job_queue`)
        отклоняет любую другую схему раньше. Сообщение об ошибке здесь не
        включает сам `settings.url` (может содержать credentials, TASK-S2.1
        инвариант) — только схему до `://`."""

        if not settings.url.startswith(_POSTGRES_URL_PREFIX):
            scheme = settings.url.split("://", 1)[0] if "://" in settings.url else settings.url
            raise ValueError(
                f"Ожидался URL со схемой {_POSTGRES_URL_PREFIX!r}, получено: {scheme!r}"
            )
        return cls(settings.url)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def enqueue(self, idempotency_key: str) -> tuple[ScanJob, bool]:
        """`INSERT ... ON CONFLICT (idempotency_key) DO NOTHING`, затем
        `SELECT` по ключу — тот же приём, что и остальные UPSERT-подобные
        операции в этом слое (`postgres_repository.py: save`): второй
        `SELECT` нужен именно для ветки конфликта, где `INSERT` ничего не
        вернул бы, а вызывающему коду (`POST /api/v1/jobs/scan`) нужна
        существующая job, а не факт конфликта."""

        job_id = uuid4()
        now = _utcnow()
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO scan_jobs (
                        id, idempotency_key, status, attempt, max_attempts,
                        available_at, created_at, updated_at
                    ) VALUES (%s, %s, %s, 0, %s, %s, %s, %s)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    """,
                    (
                        job_id,
                        idempotency_key,
                        ScanJobStatus.QUEUED.value,
                        _MAX_ATTEMPTS_DEFAULT,
                        now,
                        now,
                        now,
                    ),
                )
                created = cursor.rowcount == 1

                cursor.execute(
                    "SELECT * FROM scan_jobs WHERE idempotency_key = %s", (idempotency_key,)
                )
                row = cursor.fetchone()
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

        assert row is not None
        return _row_to_job(row), not created

    def claim_one(self, *, now: datetime, lease_seconds: int) -> ScanJob | None:
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, attempt, max_attempts FROM scan_jobs
                    WHERE (status = ANY(%s) AND available_at <= %s)
                       OR (status = %s AND lease_expires_at <= %s)
                    ORDER BY available_at ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """,
                    (list(_CLAIMABLE_STATUSES), now, ScanJobStatus.RUNNING.value, now),
                )
                candidate = cursor.fetchone()
                if candidate is None:
                    row = None
                elif candidate["attempt"] >= candidate["max_attempts"]:
                    # Lease истёк на job, которая уже была на своей последней
                    # разрешённой попытке (worker упал, не успев вызвать
                    # mark_*, TASK-S2.2 review-fix [P1]) — ЕЩЁ одна попытка
                    # нарушила бы max_attempts. Job сразу terminal `failed`,
                    # не выдаётся на исполнение этим вызовом.
                    cursor.execute(
                        """
                        UPDATE scan_jobs
                        SET status = %s, lease_expires_at = NULL,
                            error_code = %s, updated_at = %s
                        WHERE id = %s
                        """,
                        (
                            ScanJobStatus.FAILED.value,
                            "lease_expired_after_max_attempts",
                            now,
                            candidate["id"],
                        ),
                    )
                    row = None
                else:
                    cursor.execute(
                        """
                        UPDATE scan_jobs
                        SET status = %s, attempt = attempt + 1, lease_expires_at = %s,
                            error_code = NULL, updated_at = %s
                        WHERE id = %s
                        RETURNING *
                        """,
                        (
                            ScanJobStatus.RUNNING.value,
                            _add_seconds(now, lease_seconds),
                            now,
                            candidate["id"],
                        ),
                    )
                    row = cursor.fetchone()
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

        return None if row is None else _row_to_job(row)

    def mark_succeeded(self, job_id: UUID, *, attempt: int, result: Mapping[str, object]) -> None:
        self._update_terminal_or_retry(
            job_id,
            attempt=attempt,
            status=ScanJobStatus.SUCCEEDED,
            available_at=None,
            result_json=json.dumps(dict(result), ensure_ascii=False),
            error_code=None,
        )

    def mark_for_retry(
        self, job_id: UUID, *, attempt: int, available_at: datetime, error_code: str
    ) -> None:
        self._update_terminal_or_retry(
            job_id,
            attempt=attempt,
            status=ScanJobStatus.RETRY_WAIT,
            available_at=available_at,
            result_json=None,
            error_code=error_code,
        )

    def mark_failed(self, job_id: UUID, *, attempt: int, error_code: str) -> None:
        self._update_terminal_or_retry(
            job_id,
            attempt=attempt,
            status=ScanJobStatus.FAILED,
            available_at=None,
            result_json=None,
            error_code=error_code,
        )

    def _update_terminal_or_retry(
        self,
        job_id: UUID,
        *,
        attempt: int,
        status: ScanJobStatus,
        available_at: datetime | None,
        result_json: str | None,
        error_code: str | None,
    ) -> None:
        """Общая реализация трёх исходов claim'а (success/retry/failed,
        TASK-S2.2) — все три меняют один и тот же набор колонок, отличаясь
        только значениями; отдельная функция вместо трёх копий SQL исключает
        риск, что один из исходов забудет сбросить `lease_expires_at`
        (иначе `claim_one` мог бы посчитать успешно завершённую или
        окончательно упавшую job снова доступной по истечении старого
        lease).

        `WHERE ... AND attempt = %s AND status = 'running'` — compare-and-swap
        защита от «зомби»-worker'а (review-fix TASK-S2.2 [P1]): вызывающий
        код (`application/scan_worker.run_one_job`) передаёт `attempt`,
        полученный от `claim_one` для СВОЕЙ попытки. Если к моменту этого
        вызова `claim_one` уже реанимировал job по истёкшему lease
        (увеличив `attempt`) или уже финализировал её сам (истёкший lease
        на последней попытке, см. `claim_one`), это условие не совпадёт —
        `UPDATE` не тронет ни одной строки, и устаревший результат не
        перезапишет актуальное состояние job."""

        now = _utcnow()
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE scan_jobs
                    SET status = %s,
                        available_at = COALESCE(%s, available_at),
                        lease_expires_at = NULL,
                        result_json = %s,
                        error_code = %s,
                        updated_at = %s
                    WHERE id = %s AND attempt = %s AND status = %s
                    """,
                    (
                        status.value,
                        available_at,
                        result_json,
                        error_code,
                        now,
                        job_id,
                        attempt,
                        ScanJobStatus.RUNNING.value,
                    ),
                )
                if cursor.rowcount == 0:
                    logger.warning(
                        "job=%s attempt=%s: mark_%s проигнорирован — job уже "
                        "не в состоянии running с этим attempt (устаревшая "
                        "попытка, TASK-S2.2 review-fix [P1])",
                        job_id,
                        attempt,
                        status.value,
                    )
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def get(self, job_id: UUID) -> ScanJob | None:
        with self._connection.cursor() as cursor:
            cursor.execute("SELECT * FROM scan_jobs WHERE id = %s", (job_id,))
            row = cursor.fetchone()
        return None if row is None else _row_to_job(row)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _add_seconds(value: datetime, seconds: int) -> datetime:
    return value + timedelta(seconds=seconds)


def _row_to_job(row: dict[str, Any]) -> ScanJob:
    return ScanJob(
        id=row["id"],
        idempotency_key=row["idempotency_key"],
        status=ScanJobStatus(row["status"]),
        attempt=row["attempt"],
        max_attempts=row["max_attempts"],
        available_at=row["available_at"],
        lease_expires_at=row["lease_expires_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        result=json.loads(row["result_json"]) if row["result_json"] is not None else None,
        error_code=row["error_code"],
    )
