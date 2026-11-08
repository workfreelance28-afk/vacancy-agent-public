"""Durable scan queue worker orchestration (TASK-S2.2): claim одной job
через `domain.ports.ScanJobQueue`, выполнение через переданный
`ScanJobRunner`, запись success/retry/failure.

Чистая логика поверх портов — не знает о `RuntimeComponents`/реальном
`run_scan_once`/PostgreSQL DSN/SuperJob/Habr/Qwen/Telegram, тестируется на
fake handler (`docs/TASK-S2.2.md`, «Проверка и передача»: worker
integration проверять через fake handler, не реальные внешние сервисы).
Реальная сборка (`RuntimeComponents` + `run_scan_once`, классификация
исключений в `TransientScanJobError`/`PermanentScanJobError`) — в
`worker.py`, по тому же принципу разделения, что `daily_scan.py` (чистая
логика) vs `application/wiring.py` (сборка реальных зависимостей).

TASK-S2.5: `run_one_job` пишет структурированные JSON-события (claim/
retry/success/final failure) с `job_id` во всех них, и опционально
записывает в переданный `MetricsRegistry` (`observability/metrics.py`) —
`metrics=None` (по умолчанию, используется в существующих TASK-S2.2
тестах) полностью отключает запись метрик, ничего не меняя в остальном
поведении. Только `job.id`/`attempt`/`error_code`/duration идут в поля
события — не сама вакансия и не содержимое `result`."""

import logging
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Protocol

from vacancy_agent.domain.ports import ScanJobQueue
from vacancy_agent.domain.scan_job import ScanJob
from vacancy_agent.domain.scan_job_errors import PermanentScanJobError, TransientScanJobError
from vacancy_agent.observability.metrics import MetricsRegistry, error_category

logger = logging.getLogger("vacancy_agent.scan_worker")

_JOBS_TOTAL_METRIC = "scan_jobs_total"
_ERRORS_TOTAL_METRIC = "scan_job_errors_total"
_ATTEMPT_DURATION_METRIC = "scan_job_attempt_duration_seconds"

_BASE_DELAY_SECONDS = 30
_MAX_DELAY_SECONDS = 15 * 60


class ScanJobRunner(Protocol):
    """Выполняет один прогон scan для уже claim'нутой job и возвращает
    JSON-сериализуемый итог (сохраняется как `scan_jobs.result_json`).
    Поднимает `TransientScanJobError`/`PermanentScanJobError`, чтобы
    сообщить `run_one_job`, как классифицировать сбой — любое ДРУГОЕ
    исключение (программная ошибка в самом runner'е) не перехватывается
    здесь и распространяется дальше, оставляя job в `running` до истечения
    lease (TASK-S2.2, критерий №4: «истёкший lease восстанавливается») —
    это тот же безопасный fail-closed путь, что и для краша процесса
    worker'а."""

    async def __call__(self) -> Mapping[str, object]: ...


def compute_backoff_seconds(attempt: int) -> int:
    """Exponential backoff с верхней границей (TASK-S2.2, критерий №3).
    `attempt` — счётчик ПОСЛЕ инкремента в `claim_one` (1 для первой
    попытки после enqueue) — задержка перед следующей попыткой растёт от
    `_BASE_DELAY_SECONDS`, не превышая `_MAX_DELAY_SECONDS`."""

    # `1 << exponent`, не `2 ** exponent`: typeshed типизирует `int ** int`
    # как `Any` (экспонента может быть отрицательной, тогда результат —
    # `float`) — побитовый сдвиг однозначно `int` и не требует `cast`.
    exponent = max(attempt - 1, 0)
    return min(_BASE_DELAY_SECONDS * (1 << exponent), _MAX_DELAY_SECONDS)


async def run_one_job(
    queue: ScanJobQueue,
    runner: ScanJobRunner,
    *,
    lease_seconds: int,
    now: datetime | None = None,
    metrics: MetricsRegistry | None = None,
) -> ScanJob | None:
    """Claim'ит одну доступную job (`queue.claim_one`) и выполняет её через
    `runner`. `None`, если доступных job нет — штатный исход, не ошибка;
    вызывающий код (`worker.py`) в этом случае просто ждёт и пробует снова.

    `metrics` — опциональный `MetricsRegistry` (TASK-S2.5); `None`
    (по умолчанию) полностью отключает запись метрик, не меняя остальное
    поведение — существующие TASK-S2.2 тесты вызывают эту функцию без
    этого аргумента."""

    claim_time = now if now is not None else datetime.now(UTC)
    job = queue.claim_one(now=claim_time, lease_seconds=lease_seconds)
    if job is None:
        return None

    job_id = str(job.id)
    logger.info(
        "scan_job.claimed",
        extra={"job_id": job_id, "attempt": job.attempt, "max_attempts": job.max_attempts},
    )

    attempt_started = time.monotonic()
    try:
        result = await runner()
    except PermanentScanJobError as exc:
        duration_seconds = time.monotonic() - attempt_started
        logger.warning(
            "scan_job.failed",
            extra={
                "job_id": job_id,
                "attempt": job.attempt,
                "max_attempts": job.max_attempts,
                "error_code": exc.error_code,
                "duration_seconds": round(duration_seconds, 3),
                "reason": "permanent",
            },
        )
        _record_outcome(metrics, "failed", duration_seconds, exc.error_code)
        queue.mark_failed(job.id, attempt=job.attempt, error_code=exc.error_code)
        return job
    except TransientScanJobError as exc:
        duration_seconds = time.monotonic() - attempt_started
        if job.attempt >= job.max_attempts:
            logger.warning(
                "scan_job.failed",
                extra={
                    "job_id": job_id,
                    "attempt": job.attempt,
                    "max_attempts": job.max_attempts,
                    "error_code": exc.error_code,
                    "duration_seconds": round(duration_seconds, 3),
                    "reason": "attempts_exhausted",
                },
            )
            _record_outcome(metrics, "failed", duration_seconds, exc.error_code)
            queue.mark_failed(job.id, attempt=job.attempt, error_code=exc.error_code)
        else:
            delay = compute_backoff_seconds(job.attempt)
            logger.warning(
                "scan_job.retry_scheduled",
                extra={
                    "job_id": job_id,
                    "attempt": job.attempt,
                    "max_attempts": job.max_attempts,
                    "error_code": exc.error_code,
                    "duration_seconds": round(duration_seconds, 3),
                    "retry_in_seconds": delay,
                },
            )
            _record_outcome(metrics, "retry_scheduled", duration_seconds, exc.error_code)
            queue.mark_for_retry(
                job.id,
                attempt=job.attempt,
                available_at=claim_time + timedelta(seconds=delay),
                error_code=exc.error_code,
            )
        return job
    else:
        duration_seconds = time.monotonic() - attempt_started
        logger.info(
            "scan_job.succeeded",
            extra={
                "job_id": job_id,
                "attempt": job.attempt,
                "max_attempts": job.max_attempts,
                "duration_seconds": round(duration_seconds, 3),
            },
        )
        _record_outcome(metrics, "succeeded", duration_seconds, None)
        queue.mark_succeeded(job.id, attempt=job.attempt, result=result)
        return job


def _record_outcome(
    metrics: MetricsRegistry | None,
    outcome: str,
    duration_seconds: float,
    error_code: str | None,
) -> None:
    if metrics is None:
        return
    metrics.increment(_JOBS_TOTAL_METRIC, {"outcome": outcome})
    metrics.observe_latency(_ATTEMPT_DURATION_METRIC, duration_seconds)
    if error_code is not None:
        metrics.increment(_ERRORS_TOTAL_METRIC, {"category": error_category(error_code)})
