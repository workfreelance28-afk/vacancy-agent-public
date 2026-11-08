"""Worker CLI (TASK-S2.2) — точка входа консольного скрипта
`vacancy-agent-worker`.

Отдельный процесс от `scheduler.py`: тот по-прежнему сам планирует и
выполняет ежедневный scan (не тронут этой задачей — «Текущие прямой scan и
scheduler не удалять и не менять по поведению», `docs/TASK-S2.2.md`) — этот
worker обслуживает НОВУЮ durable-очередь `POST /api/v1/jobs/scan`, не
заменяет существующий планировщик. Оба процесса могут запускать
`run_scan_once` независимо и одновременно — идемпотентность по `hh_id`
(`VacancyRepository.save`, TASK-004) защищает от дублей вакансий, как и
раньше.

Как и `scheduler.py`/`api/main.py`, не тестируется юнит-тестами —
операционная склейка реальных PostgreSQL/SuperJob/Habr/Qwen/Telegram
ресурсов. Чистая orchestration-логика (claim/retry/backoff) — в
`application/scan_worker.py`, тестируемая на fake handler.

Классификация исключений `run_scan_once` в transient/permanent — намеренно
минимальная эвристика, не исчерпывающая таксономия: `OSError`/
`TimeoutError`/`ConnectionError`/`psycopg.Error` (сеть и PostgreSQL —
основной реалистичный источник транзиентных сбоев для уже отфильтрованных
`VacancySourceError`/`QwenClientError`/`TelegramNotifierError`, которые
`daily_scan.py` перехватывает сам и не даёт им всплыть сюда) считаются
transient; всё остальное — `PermanentScanJobError` (TASK-S2.2: «программные/
validation ошибки не повторять»). Уточнение этого списка по мере
наблюдения реальных сбоев — вне scope этой задачи."""

import asyncio
import logging
from collections.abc import Mapping

import psycopg

from vacancy_agent.application.scan_worker import ScanJobRunner, run_one_job
from vacancy_agent.application.wiring import RuntimeComponents, run_scan_once
from vacancy_agent.config import load_settings
from vacancy_agent.domain.ports import ScanJobQueue
from vacancy_agent.domain.scan_job_errors import PermanentScanJobError, TransientScanJobError
from vacancy_agent.observability.json_logging import configure_json_logging
from vacancy_agent.observability.metrics import MetricsRegistry
from vacancy_agent.persistence.factory import create_scan_job_queue

logger = logging.getLogger("vacancy_agent.worker")

_LEASE_SECONDS = 15 * 60
_POLL_INTERVAL_SECONDS = 5.0

_TRANSIENT_EXCEPTION_TYPES: tuple[type[BaseException], ...] = (
    OSError,
    TimeoutError,
    ConnectionError,
    psycopg.Error,
)


def _make_runner(components: RuntimeComponents) -> ScanJobRunner:
    async def runner() -> Mapping[str, object]:
        try:
            summary = await run_scan_once(components)
        except _TRANSIENT_EXCEPTION_TYPES as exc:
            raise TransientScanJobError(type(exc).__name__) from exc
        except Exception as exc:
            raise PermanentScanJobError(type(exc).__name__) from exc
        if summary.failed_sources:
            # `_run_source_scans` (`application/wiring.py`) уже перехватывает
            # `VacancySourceError` на уровне одного источника и продолжает
            # обработку остальных — она НИКОГДА не пробрасывается наружу
            # `run_scan_once`, а лишь попадает в `failed_sources`. Без этой
            # проверки реальный транзиентный сбой источника (обычная сетевая
            # нестабильность SuperJob/Habr) тихо засчитывался бы worker'ом
            # как success — job не повторялась бы (review-fix TASK-S2.2
            # round 1 [P1], «Реальные временные сбои обходят retry очереди»).
            # Имена источников (`failed_sources`) — не raw exception text,
            # безопасны для `error_code`.
            raise TransientScanJobError(
                "vacancy_source_error:" + ",".join(summary.failed_sources)
            )
        if summary.qwen_failed or summary.send_failed:
            # Та же дыра на уровень ниже (review-fix TASK-S2.2 round 2
            # [P1], «Qwen/Telegram failures still become success»):
            # `run_daily_scan` (`application/daily_scan.py`) сам
            # перехватывает `QwenClientError`/`TelegramNotifierError` на
            # уровне ОДНОЙ вакансии — увеличивает `qwen_failed`/
            # `send_failed` и продолжает со следующей, не пробрасывая
            # ошибку наружу. Эти вакансии НЕ сохранены в repository (см.
            # `daily_scan.py`), поэтому safe для повторной обработки — но
            # без этой проверки транзиентный сбой Qwen/Telegram (LM Studio
            # перегружен, Telegram API недоступен) тоже тихо засчитывался
            # бы success, и job не повторилась бы в разумный срок. Только
            # числа — не raw exception, безопасны для `error_code`.
            raise TransientScanJobError(
                f"vacancy_processing_failed:qwen_failed={summary.qwen_failed},"
                f"send_failed={summary.send_failed}"
            )
        return summary.model_dump(mode="json")

    return runner


async def _worker_loop(
    components: RuntimeComponents, queue: ScanJobQueue, metrics: MetricsRegistry
) -> None:
    runner = _make_runner(components)
    while True:
        job = await run_one_job(
            queue, runner, lease_seconds=_LEASE_SECONDS, metrics=metrics
        )
        if job is None:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)


async def run() -> None:
    configure_json_logging()
    metrics = MetricsRegistry()
    settings = load_settings()
    components = RuntimeComponents(settings)
    try:
        queue = create_scan_job_queue(settings.database)
    except Exception:
        # `RuntimeComponents(settings)` уже открыл живые ресурсы (SQLite/
        # PostgreSQL-соединение, source-клиенты, Qwen-клиент, Telegram
        # `Bot`) — если создание очереди упадёт (например, PostgreSQL
        # недоступен на старте), они не должны остаться открытыми
        # (review-fix TASK-S2.2 [P2]).
        components.close()
        raise
    logger.info("worker.started")
    try:
        await _worker_loop(components, queue, metrics)
    finally:
        # `queue.close()` до `components.close()` в одном `finally` без
        # защиты означал бы, что ошибка первого (например, соединение уже
        # разорвано) пропускает второй — `components` (SQLite/PostgreSQL-
        # соединение, source-клиенты, Qwen-клиент, Telegram `Bot`) остался
        # бы открытым (review-fix TASK-S2.2 round 2 [P2], «Queue close can
        # prevent component cleanup»). `components.close()` сама по себе
        # не бросает (`RuntimeComponents.close()` использует `_close_all`,
        # которая логирует, а не поднимает ошибки закрытия отдельных
        # ресурсов, `application/wiring.py`) — здесь достаточно защитить
        # именно `queue.close()`.
        try:
            queue.close()
        except Exception:
            logger.exception("Не удалось закрыть очередь сканирования")
        components.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
