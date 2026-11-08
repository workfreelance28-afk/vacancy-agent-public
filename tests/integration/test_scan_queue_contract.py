"""Contract-тесты `PostgresScanJobQueue`/`application.scan_worker` на
реальной тестовой PostgreSQL (TASK-S2.2, критерии готовности №1-4, №7:
«live PostgreSQL suite», «включая два конкурентных worker»).

Тот же fail-closed guard, что `tests/integration/
test_postgres_repository_contract.py` (TASK-S2.1 review-fix [P1]):
`TEST_DATABASE_URL` пропущен без него, `_require_safe_test_dsn` требует ЕЩЁ
`TEST_DATABASE_URL_CONFIRM_DESTRUCTIVE=1` и `test` в имени БД перед любым
`DELETE FROM scan_jobs`.

Реальный SuperJob/Habr/Qwen/Telegram здесь не вызывается ни разу
(`docs/TASK-S2.2.md`, «Проверка и передача»): worker-сценарии (retry/
backoff/success/failed) проверяются через `application.scan_worker.
run_one_job` с fake async runner'ом — реальная `run_scan_once`-обвязка
(`worker.py`) не тестируется юнит/интеграционными тестами, как и
`scheduler.py`/`api/main.py`."""

import os
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from vacancy_agent.application.scan_worker import run_one_job
from vacancy_agent.domain.scan_job_errors import PermanentScanJobError, TransientScanJobError

if TYPE_CHECKING:
    from vacancy_agent.persistence.postgres_scan_queue import PostgresScanJobQueue

from vacancy_agent.config.settings import DatabaseSettings

psycopg = pytest.importorskip("psycopg")

_TEST_DSN = os.environ.get("TEST_DATABASE_URL", "")
_CONFIRM_ENV_VAR = "TEST_DATABASE_URL_CONFIRM_DESTRUCTIVE"
_CONFIRMED = os.environ.get(_CONFIRM_ENV_VAR, "") == "1"

pytestmark = pytest.mark.skipif(
    not _TEST_DSN,
    reason=(
        "TEST_DATABASE_URL не задан — scan queue contract-тесты пропущены "
        "локально (TASK-S2.2, критерии готовности №1-4/№7 остаются "
        "открытыми до зафиксированного прогона на реальной тестовой "
        "PostgreSQL)"
    ),
)


def _require_safe_test_dsn(dsn: str) -> None:
    """Тот же fail-closed guard, что `test_postgres_repository_contract.
    _require_safe_test_dsn` (см. его docstring) — продублирован намеренно,
    не импортирован из другого test-модуля (каждый файл, выполняющий
    `DELETE FROM`, держит собственную независимую защиту)."""

    if not _CONFIRMED:
        pytest.fail(
            f"TEST_DATABASE_URL задан, но {_CONFIRM_ENV_VAR}=1 — нет. Эти тесты "
            "выполняют DELETE FROM scan_jobs перед каждым тестом — установите "
            f"{_CONFIRM_ENV_VAR}=1, только если TEST_DATABASE_URL указывает на "
            "одноразовую тестовую PostgreSQL, данные которой не жалко потерять."
        )
    db_name = dsn.rsplit("/", 1)[-1].split("?", 1)[0]
    if "test" not in db_name.lower():
        pytest.fail(
            f"TEST_DATABASE_URL указывает на БД {db_name!r}, в имени которой нет "
            "'test' — fail-closed guard отказывается выполнять DELETE FROM в "
            "базе, не похожей на одноразовую тестовую."
        )


def _new_queue() -> "PostgresScanJobQueue":
    from vacancy_agent.persistence.postgres_scan_queue import PostgresScanJobQueue

    settings = DatabaseSettings(url=_TEST_DSN)
    return PostgresScanJobQueue.from_settings(settings)


@pytest.fixture
def queue() -> Iterator["PostgresScanJobQueue"]:
    _require_safe_test_dsn(_TEST_DSN)
    repository = _new_queue()
    with repository._connection.cursor() as cursor:
        cursor.execute("DELETE FROM scan_jobs")
    repository._connection.commit()
    try:
        yield repository
    finally:
        repository.close()


# --- enqueue idempotency (критерий №1) ---


def test_enqueue_returns_the_same_job_for_a_repeated_key(
    queue: "PostgresScanJobQueue",
) -> None:
    first, first_reused = queue.enqueue("run-2026-09-20")
    second, second_reused = queue.enqueue("run-2026-09-20")

    assert first.id == second.id
    assert first_reused is False
    assert second_reused is True


def test_enqueue_different_keys_creates_different_jobs(queue: "PostgresScanJobQueue") -> None:
    first, _ = queue.enqueue("key-a")
    second, _ = queue.enqueue("key-b")

    assert first.id != second.id


# --- concurrent claim (критерий №2, "два конкурентных worker") ---


def test_concurrent_claim_gives_the_job_to_exactly_one_worker(
    queue: "PostgresScanJobQueue",
) -> None:
    job, _ = queue.enqueue("concurrent-claim")
    now = datetime.now(UTC)
    barrier = Barrier(2)

    def claim_with_own_connection() -> UUID | None:
        worker_queue = _new_queue()
        try:
            barrier.wait(timeout=5)
            claimed = worker_queue.claim_one(now=now, lease_seconds=60)
            return None if claimed is None else claimed.id
        finally:
            worker_queue.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim_with_own_connection(), range(2)))

    claimed_ids = [result for result in results if result is not None]
    assert claimed_ids == [job.id]
    assert results.count(None) == 1

    stored = queue.get(job.id)
    assert stored is not None
    assert stored.attempt == 1


# --- lease timeout (критерий №4) ---


def test_expired_lease_is_reclaimed_but_active_lease_is_not(
    queue: "PostgresScanJobQueue",
) -> None:
    job, _ = queue.enqueue("lease-timeout")
    t0 = datetime.now(UTC)

    first_claim = queue.claim_one(now=t0, lease_seconds=60)
    assert first_claim is not None
    assert first_claim.attempt == 1

    # Ещё не истёк — второй claim не должен ничего вернуть (тот же worker
    # или другой — claim_one не делает различий по вызывающей стороне).
    not_yet = queue.claim_one(now=t0 + timedelta(seconds=30), lease_seconds=60)
    assert not_yet is None

    # Истёк — job снова доступна, attempt увеличивается ещё раз.
    reclaimed = queue.claim_one(now=t0 + timedelta(seconds=61), lease_seconds=60)
    assert reclaimed is not None
    assert reclaimed.id == job.id
    assert reclaimed.attempt == 2


def test_expired_lease_on_last_attempt_finalizes_to_failed_not_reclaimed(
    queue: "PostgresScanJobQueue",
) -> None:
    """Review-fix TASK-S2.2 [P1] «Истёкшая последняя попытка запускается
    снова»: worker падает (не вызывает `mark_*`) на каждой из
    `max_attempts` (по умолчанию 3) попыток подряд — четвёртый `claim_one`
    не должен выдать job на ещё одно исполнение, а должен сразу
    финализировать её как `failed`."""

    job, _ = queue.enqueue("lease-expired-last-attempt")
    t0 = datetime.now(UTC)

    first = queue.claim_one(now=t0, lease_seconds=60)
    assert first is not None and first.attempt == 1

    second = queue.claim_one(now=t0 + timedelta(seconds=61), lease_seconds=60)
    assert second is not None and second.attempt == 2

    third = queue.claim_one(now=t0 + timedelta(seconds=122), lease_seconds=60)
    assert third is not None and third.attempt == 3
    assert third.max_attempts == 3

    # Третья попытка тоже "падает" (worker крашнулся, mark_* не вызван) —
    # lease истекает, но попыток больше нет.
    fourth = queue.claim_one(now=t0 + timedelta(seconds=183), lease_seconds=60)
    assert fourth is None

    stored = queue.get(job.id)
    assert stored is not None
    assert stored.status.value == "failed"
    assert stored.attempt == 3
    assert stored.error_code == "lease_expired_after_max_attempts"
    assert stored.lease_expires_at is None

    # И не подхватывается никаким последующим claim_one.
    fifth = queue.claim_one(now=t0 + timedelta(days=1), lease_seconds=60)
    assert fifth is None


# --- stale ("зомби") worker completion (критерий №2/№4) ---


def test_stale_worker_completion_does_not_overwrite_newer_attempt(
    queue: "PostgresScanJobQueue",
) -> None:
    """Review-fix TASK-S2.2 [P1] «Старый worker может перезаписать результат
    новой попытки»: worker A держит attempt=1, его lease истекает и job
    реанимируется для worker B (attempt=2) ДО того, как A успевает
    вызвать `mark_succeeded`. Запоздалый вызов A с его устаревшим
    `attempt=1` не должен повлиять на состояние актуальной попытки B."""

    job, _ = queue.enqueue("zombie-worker")
    t0 = datetime.now(UTC)

    worker_a_job = queue.claim_one(now=t0, lease_seconds=60)
    assert worker_a_job is not None and worker_a_job.attempt == 1

    worker_b_job = queue.claim_one(now=t0 + timedelta(seconds=61), lease_seconds=60)
    assert worker_b_job is not None and worker_b_job.attempt == 2

    # Worker A "не знает", что его лишили job, и запоздало отчитывается о
    # своём (устаревшем) attempt=1.
    queue.mark_succeeded(job.id, attempt=worker_a_job.attempt, result={"from": "stale-worker-a"})

    after_stale_call = queue.get(job.id)
    assert after_stale_call is not None
    assert after_stale_call.status.value == "running"
    assert after_stale_call.attempt == 2
    assert after_stale_call.result is None

    # Worker B, чья попытка актуальна, завершает job нормально.
    queue.mark_succeeded(job.id, attempt=worker_b_job.attempt, result={"from": "worker-b"})

    final = queue.get(job.id)
    assert final is not None
    assert final.status.value == "succeeded"
    assert final.result == {"from": "worker-b"}


def test_stale_worker_retry_after_permanent_failure_does_not_reopen_job(
    queue: "PostgresScanJobQueue",
) -> None:
    """Тот же compare-and-swap, что предыдущий тест, но для `mark_for_retry`
    поверх уже терминального состояния: worker A (attempt=1) висит дольше
    lease, worker B (attempt=2) успевает завершить job `failed`
    (permanent). Запоздалый `mark_for_retry` от A с `attempt=1` не должен
    вернуть уже провалившуюся job обратно в очередь."""

    job, _ = queue.enqueue("zombie-worker-retry")
    t0 = datetime.now(UTC)

    worker_a_job = queue.claim_one(now=t0, lease_seconds=60)
    assert worker_a_job is not None and worker_a_job.attempt == 1

    worker_b_job = queue.claim_one(now=t0 + timedelta(seconds=61), lease_seconds=60)
    assert worker_b_job is not None and worker_b_job.attempt == 2

    queue.mark_failed(job.id, attempt=worker_b_job.attempt, error_code="validation_error")

    # Worker A запаздывает с (устаревшим) retry для своего attempt=1.
    queue.mark_for_retry(
        job.id,
        attempt=worker_a_job.attempt,
        available_at=t0 + timedelta(seconds=200),
        error_code="stale_network_error",
    )

    stored = queue.get(job.id)
    assert stored is not None
    assert stored.status.value == "failed"
    assert stored.error_code == "validation_error"


# --- worker orchestration через fake handler (критерий №3) ---


async def test_run_one_job_success_persists_result_in_real_postgres(
    queue: "PostgresScanJobQueue",
) -> None:
    job, _ = queue.enqueue("worker-success")

    async def fake_runner() -> Mapping[str, object]:
        return {"fetched": 4, "notified": 2}

    await run_one_job(queue, fake_runner, lease_seconds=60)

    stored = queue.get(job.id)
    assert stored is not None
    assert stored.status.value == "succeeded"
    assert stored.result == {"fetched": 4, "notified": 2}
    assert stored.error_code is None
    assert stored.lease_expires_at is None


async def test_run_one_job_transient_failure_schedules_retry_in_real_postgres(
    queue: "PostgresScanJobQueue",
) -> None:
    job, _ = queue.enqueue("worker-transient-retry")
    before = datetime.now(UTC)

    async def failing_runner() -> Mapping[str, object]:
        raise TransientScanJobError("network_error")

    await run_one_job(queue, failing_runner, lease_seconds=60)

    stored = queue.get(job.id)
    assert stored is not None
    assert stored.status.value == "retry_wait"
    assert stored.error_code == "network_error"
    assert stored.attempt == 1
    assert stored.available_at > before
    assert stored.lease_expires_at is None


async def test_run_one_job_transient_failure_becomes_failed_after_max_attempts(
    queue: "PostgresScanJobQueue",
) -> None:
    queue.enqueue("worker-transient-exhausted")

    async def failing_runner() -> Mapping[str, object]:
        raise TransientScanJobError("network_error")

    # max_attempts по умолчанию — 3: claim/fail три раза подряд, каждый раз
    # передавая `now` за пределами уже назначенного backoff, чтобы не
    # дожидаться реального времени.
    now = datetime.now(UTC)
    job_id: UUID | None = None
    for _ in range(3):
        claimed = await run_one_job(queue, failing_runner, lease_seconds=60, now=now)
        assert claimed is not None
        job_id = claimed.id
        now = now + timedelta(hours=1)

    assert job_id is not None
    stored = queue.get(job_id)
    assert stored is not None
    assert stored.status.value == "failed"
    assert stored.attempt == 3
    assert stored.error_code == "network_error"


async def test_run_one_job_permanent_failure_does_not_retry_in_real_postgres(
    queue: "PostgresScanJobQueue",
) -> None:
    job, _ = queue.enqueue("worker-permanent")

    async def failing_runner() -> Mapping[str, object]:
        raise PermanentScanJobError("validation_error")

    await run_one_job(queue, failing_runner, lease_seconds=60)

    stored = queue.get(job.id)
    assert stored is not None
    assert stored.status.value == "failed"
    assert stored.attempt == 1
    assert stored.error_code == "validation_error"


async def test_migrations_are_safe_to_reapply(queue: "PostgresScanJobQueue") -> None:
    """Повторное создание очереди для той же БД не падает на уже
    применённых миграциях (тот же критерий готовности №4 TASK-S2.1,
    подтверждённый для новой `scan_jobs`-миграции TASK-S2.2)."""

    second = _new_queue()
    try:
        job, reused = second.enqueue("reopen-check")
        assert reused is False
        assert second.get(job.id) is not None
    finally:
        second.close()
