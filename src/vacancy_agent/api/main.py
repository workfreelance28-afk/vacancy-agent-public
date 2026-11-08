"""Точка входа консольного скрипта `vacancy-agent-api` (TASK-009/ADR-028/029,
TASK-011/ADR-034/035).

Не поднимает APScheduler, Telegram long polling и не создаёт SuperJob/HH-
или Telegram-клиентов (в отличие от `scheduler.py`/`main._run_scan_command`)
— только Uvicorn поверх `create_app`, читающего/пишущего SQLite через
`application/vacancy_queries.py`/`application/application_drafts.py`. Для
запуска достаточно корректного `DATABASE_URL`; operational secrets
`scan`/`scheduler`/`QWEN_MODEL` здесь не проверяются (`docs/TASK-009.md`,
«Конфигурация и запуск»; `docs/TASK-011.md`: «Поле `QWEN_MODEL` должно быть
непустым только при реальном POST»).

Намеренно НЕ вызывает `config.load_settings()`: та собирает всю `Settings`
целиком (HH/SuperJob/фильтры/Telegram/Qwen/расписание), а health/list/detail
их не используют вовсе. Если бы `main()` шёл через `load_settings()`,
невалидное значение в любой из этих посторонних секций (например, битый
JSON в `TELEGRAM_ALLOWED_CHAT_IDS`) не давало бы API запуститься — той
секции, которую read-only часть API вообще не читает (найдено ревью
TASK-009, ADR-030). Вместо этого `DatabaseSettings`/`ApiSettings` собираются
по отдельности — каждая сама по себе `BaseSettings` и читает `.env`/
переменные окружения независимо (см. `config/settings.py`,
`_SectionSettings`). `QwenSettings` по той же причине читается не здесь, а
лениво внутри `_generator_factory` — только когда POST реально вызывает её
(`docs/TASK-011.md`, «Порты и границы»)."""

import logging
from collections.abc import Callable

import uvicorn

from vacancy_agent.api.app import create_app
from vacancy_agent.application.vacancy_queries import RepositoryFactory
from vacancy_agent.config.settings import ApiSettings, DatabaseSettings, QwenSettings
from vacancy_agent.domain.application_draft_errors import ApplicationDraftNetworkError
from vacancy_agent.domain.ports import ScanJobQueue, VacancyRepositoryBackend
from vacancy_agent.integrations.application_draft_client import LMStudioApplicationDraftGenerator
from vacancy_agent.observability.json_logging import configure_json_logging
from vacancy_agent.persistence.factory import create_scan_job_queue, create_vacancy_repository

logger = logging.getLogger("vacancy_agent.api")


def _repository_factory(database: DatabaseSettings) -> RepositoryFactory:
    """Фабрика: каждый вызов открывает и мигрирует своё соединение — ни
    одно соединение не переживает вызывающую операцию (см. `api/app.py`,
    `application/vacancy_queries.py`). Backend (SQLite/PostgreSQL) выбирается
    по схеме `database.url` в `persistence.factory.create_vacancy_repository`
    (TASK-S2.1) — `api/main.py` не решает это сам."""

    def factory() -> VacancyRepositoryBackend:
        return create_vacancy_repository(database)

    return factory


def _scan_job_queue_factory(database: DatabaseSettings) -> Callable[[], ScanJobQueue]:
    """Всегда собирается, независимо от backend'а (TASK-S2.2) — сам вызов
    `create_scan_job_queue` внутри `factory()` отклоняет не-PostgreSQL
    `DATABASE_URL` явным `ValueError`; `api/app.py` ловит это на каждый
    запрос и превращает в безопасный `503`, не мешая остальным routes."""

    def factory() -> ScanJobQueue:
        return create_scan_job_queue(database)

    return factory


def _require_configured_qwen_settings() -> QwenSettings:
    """Общая проверка для `_generator_factory`/`_resolve_model_name`: пустой
    `QWEN_MODEL` не мешает процессу запуститься или отвечать на
    health/list/detail/GET — он обнаруживается только здесь, лениво, в
    момент фактического POST (`docs/TASK-011.md`)."""

    qwen = QwenSettings()
    if not qwen.model:
        raise ApplicationDraftNetworkError("QWEN_MODEL не задан — генерация черновика недоступна")
    return qwen


def _resolve_model_name() -> str:
    """Дешёвая проверка сконфигурированного имени модели (TASK-011,
    ADR-049) — читает только `QwenSettings()`, не создаёт
    `LMStudioApplicationDraftGenerator` и не открывает сеть.
    `prepare_application_draft` вызывает её ТОЛЬКО когда решает, применим
    ли reuse (profile/prompt уже совпали) — если модель тоже совпадает,
    `_generator_factory` ниже вообще не вызывается."""

    return _require_configured_qwen_settings().model


def _generator_factory() -> LMStudioApplicationDraftGenerator:
    """Ленивая фабрика generator'а черновиков (TASK-011): `QwenSettings()`
    читается и `LMStudioApplicationDraftGenerator` создаётся только когда
    эту функцию реально вызывает `prepare_application_draft` — то есть уже
    после того, как request/vacancy preconditions и reuse-проверка (включая
    модель, `_resolve_model_name`) прошли (см. `application/
    application_drafts.py`)."""

    return LMStudioApplicationDraftGenerator(_require_configured_qwen_settings())


def main() -> None:
    configure_json_logging()
    database = DatabaseSettings()
    api = ApiSettings()
    app = create_app(
        _repository_factory(database),
        generator_factory=_generator_factory,
        model_resolver=_resolve_model_name,
        scan_job_queue_factory=_scan_job_queue_factory(database),
    )
    logger.info("api.started", extra={"host": api.host, "port": api.port})
    uvicorn.run(app, host=api.host, port=api.port)


if __name__ == "__main__":
    main()
