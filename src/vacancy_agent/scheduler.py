"""Точка входа для реального запуска.

Планирует ежедневный сценарий через APScheduler (см. `docs/DECISIONS.md`,
ADR-006/ADR-012: направление зафиксировано без отдельного оркестратора,
`AsyncIOScheduler` — потому что `aiogram` уже требует asyncio-цикл) и
слушает нажатия кнопок Telegram через long polling — оба работают в
одном event loop.

`run()`/`main()` не тестируются юнит-тестами (в отличие от
`daily_scan.py`/`wiring.py`): это операционная склейка реальных
сетевых/файловых ресурсов, а не бизнес-логика — проверяется
вручную/при развёртывании. `build_daily_trigger` вынесен отдельно
именно затем, чтобы его конкретное поведение (часовой пояс) можно было
проверить юнит-тестом без запуска планировщика целиком.
"""

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from vacancy_agent.application.wiring import RuntimeComponents, run_scan_once
from vacancy_agent.config import load_settings
from vacancy_agent.config.settings import ScheduleSettings
from vacancy_agent.integrations.telegram_bot import create_dispatcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("vacancy_agent.scheduler")


async def _run_scan_job(components: RuntimeComponents) -> None:
    summary = await run_scan_once(components)
    # TASK-015, Codex review-fix [P2]: partial failure (`failed_sources`
    # непуст) раньше логировался тем же `logger.info`, что и полный успех —
    # планировщик не отличал бы частично неуспешный ежедневный прогон от
    # обычного в логах. `main.py: _run_scan_command` уже делает это
    # различие через exit code; здесь у job нет exit code (APScheduler
    # просто планирует следующий запуск), поэтому уровень лога — то, что
    # реально сигнализирует проблему оператору/мониторингу.
    if summary.failed_sources:
        logger.error("Ежедневный прогон завершён частично: %s", summary.model_dump())
    else:
        logger.info("Ежедневный прогон завершён: %s", summary.model_dump())


def build_daily_trigger(schedule: ScheduleSettings) -> CronTrigger:
    """`timezone` здесь обязателен: без него `CronTrigger` резолвит часовой
    пояс хоста в момент создания (не в момент запуска и не из
    `AsyncIOScheduler(timezone=...)`, где бы он ни был сконструирован) — на
    сервере с системным TZ, отличным от `SCHEDULE_TIMEZONE`, ежедневный
    запуск тихо сдвинулся бы на разницу поясов (найдено ревью TASK-005, см.
    `docs/DECISIONS.md`, ADR-012, «Уточнения по итогам ревью»)."""

    return CronTrigger(hour=schedule.daily_run_hour, minute=0, timezone=schedule.timezone)


async def run() -> None:
    settings = load_settings()
    components = RuntimeComponents(settings)

    scheduler = AsyncIOScheduler(timezone=settings.schedule.timezone)
    scheduler.add_job(
        _run_scan_job,
        build_daily_trigger(settings.schedule),
        args=[components],
        id="daily_scan",
        misfire_grace_time=3600,
    )
    scheduler.start()
    logger.info(
        "Планировщик запущен: ежедневный прогон в %02d:00 (%s)",
        settings.schedule.daily_run_hour,
        settings.schedule.timezone,
    )

    dispatcher = create_dispatcher(components.notifier)
    try:
        await dispatcher.start_polling(components.bot)
    finally:
        scheduler.shutdown(wait=False)
        components.close()
        await components.bot.session.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
