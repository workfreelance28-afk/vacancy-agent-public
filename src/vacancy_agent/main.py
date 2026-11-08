"""Точка входа CLI (`vacancy-agent`).

Без аргументов — только загружает и валидирует конфигурацию (TASK-001),
не обращается к HH/SuperJob, Telegram, LM Studio или Claude API.

`scan` (TASK-008, `docs/DECISIONS.md` ADR-025) — одноразовый прогон уже
существующего pipeline: `SuperJob/HH → жёсткие фильтры → Qwen → SQLite →
Telegram`. CLI здесь — тонкая точка входа: она не собирает зависимости и
не содержит бизнес-логику сама — обе живут в `application/wiring.py`
(`RuntimeComponents`, `run_scan_once`) и `application/daily_scan.py`
(`run_daily_scan`), которые уже использует и `scheduler.py`. `scan`
завершается сама после одного прогона — не запускает APScheduler и не
слушает Telegram long polling (это делает только
`vacancy-agent-scheduler`, поведение которого не менялось).
"""

import asyncio
import logging
import sys

from pydantic import ValidationError

from vacancy_agent.application.wiring import RuntimeComponents, ScanRunSummary, run_scan_once
from vacancy_agent.config import load_settings
from vacancy_agent.config.settings import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("vacancy_agent")


def run(argv: list[str] | None = None) -> int:
    """Точка входа консольного скрипта `vacancy-agent`.

    `argv` — только для тестов (передать аргументы напрямую, без
    monkeypatch `sys.argv`); реальный запуск читает `sys.argv[1:]`."""

    args = sys.argv[1:] if argv is None else argv

    if args and args[0] == "scan":
        if len(args) > 1:
            logger.error("Неизвестные аргументы команды 'scan': %s", " ".join(args[1:]))
            return 1
        return _run_scan_command()

    if args:
        logger.error("Неизвестная команда: %s (доступно: scan)", args[0])
        return 1

    settings = _try_load_settings()
    if settings is None:
        return 1

    logger.info(
        "Каркас готов: hh.base_url=%s database.url=%s schedule.timezone=%s",
        settings.hh.base_url,
        settings.database.url,
        settings.schedule.timezone,
    )
    return 0


def _run_scan_command() -> int:
    settings = _try_load_settings()
    if settings is None:
        return 1

    missing = _missing_scan_settings(settings)
    if missing:
        for field in missing:
            logger.error("Для 'scan' обязательна настройка: %s", field)
        return 1

    try:
        summary = asyncio.run(_scan_once_and_close(settings))
    except Exception:
        logger.exception("Прогон не завершился успешно")
        return 1

    # `ScanRunSummary` — только счётчики и имена источников (см.
    # `application/wiring.py`), без текста вакансий, HTML или секретов —
    # безопасно логировать целиком.
    logger.info("Прогон завершён: %s", summary.model_dump())
    if summary.failed_sources:
        # TASK-015: хотя бы один источник (`superjob_habr`) не завершился —
        # частичный успех остаётся видимым для CLI/мониторинга, не тихим
        # `0`, даже если другой источник обработан полностью.
        logger.error(
            "Источники завершились с ошибкой: %s", ", ".join(summary.failed_sources)
        )
        return 1
    return 0


def _try_load_settings() -> Settings | None:
    try:
        return load_settings()
    except ValidationError as exc:
        # Логируем только путь и тип ошибки поля, без exc.errors()["input"] и без
        # str(exc) целиком — оба могут содержать значение поля, включая секреты
        # (см. docs/ARCHITECTURE.md: «Логи не содержат API-ключи... секреты»).
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"])
            logger.error("Конфигурация невалидна: %s (%s)", field, error["type"])
        return None
    except Exception:
        logger.exception("Не удалось загрузить конфигурацию")
        return None


def _missing_scan_settings(settings: Settings) -> list[str]:
    """Fail-fast проверка только тех настроек, без которых реальный `scan`
    заведомо не может ничего сделать полезного — не полная валидация
    (`load_settings()`/`_try_load_settings()` её уже сделали) и не касается
    plain `vacancy-agent` (TASK-008, ADR-025, п.5)."""

    missing: list[str] = []
    if not settings.telegram.bot_token.get_secret_value():
        missing.append("TELEGRAM_BOT_TOKEN")
    if not settings.telegram.allowed_chat_ids:
        missing.append("TELEGRAM_ALLOWED_CHAT_IDS")
    # TASK-015: `superjob_habr` требует ОБА условия ниже одновременно — тот
    # же приём условной проверки, расширенный третьим значением
    # `vacancy_source`, не отдельная ветка.
    if (
        settings.vacancy_source in ("superjob", "superjob_habr")
        and not settings.superjob.api_key.get_secret_value()
    ):
        missing.append("SUPERJOB_API_KEY")
    # TASK-013, Codex review-fix [P2]: без этой проверки здесь пустой
    # `HABR_SEARCH_QUERY` обнаруживался только внутри `HabrCareerSource.
    # search_vacancies`, вызванного из `run_daily_scan` — то есть уже ПОСЛЕ
    # того, как `RuntimeComponents.__init__` успевал создать SQLite,
    # HTTP-клиенты, `LMStudioQwenClient` и Telegram `Bot`. Критерий требует
    # понятную ошибку до создания HTTP-клиента, не после.
    if (
        settings.vacancy_source in ("habr", "superjob_habr")
        and not settings.habr.search_query.strip()
    ):
        missing.append("HABR_SEARCH_QUERY")
    if not settings.qwen.model:
        missing.append("QWEN_MODEL")
    return missing


async def _scan_once_and_close(settings: Settings) -> ScanRunSummary:
    components = RuntimeComponents(settings)
    try:
        return await run_scan_once(components)
    finally:
        # Nested finally: `bot.session.close()` must run even if
        # `components.close()` itself raises (review finding, ADR-025) —
        # `close()` is now best-effort internally (`_close_all`), but this
        # keeps the guarantee explicit rather than relying on that.
        try:
            components.close()
        finally:
            await components.bot.session.close()


if __name__ == "__main__":
    sys.exit(run())
