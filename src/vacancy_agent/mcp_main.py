"""Точка входа локального MCP server (TASK-S2.6).

`mcp_server.py` определяет только testable adapter (`create_mcp_server`),
ничего не знающий про реальную БД — этот файл единственный решает, откуда
брать `repository_factory` в проде: SQLite/PostgreSQL по `DATABASE_URL`,
как и `api/main.py`.

ВАЖНО (stdio transport): MCP-клиент читает JSON-RPC протокол из
`sys.stdout` этого процесса. `configure_json_logging()` (TASK-S2.5) сюда
НЕЛЬЗЯ подключать — она пишет JSON-логи в тот же `sys.stdout`, и любая
случайная строка лога сломала бы protocol stream клиенту. Логирование
здесь сознательно не настраивается; если понадобится — только в
`sys.stderr`.
"""

from vacancy_agent.application.application_drafts import (
    ApplicationDraftRepositoryFactory,
)
from vacancy_agent.config.settings import DatabaseSettings, QwenSettings
from vacancy_agent.domain.application_draft_errors import ApplicationDraftNetworkError
from vacancy_agent.domain.ports import VacancyRepositoryBackend
from vacancy_agent.integrations.application_draft_client import LMStudioApplicationDraftGenerator
from vacancy_agent.mcp_server import create_mcp_server
from vacancy_agent.persistence.factory import create_vacancy_repository


def _repository_factory(
    database: DatabaseSettings,
) -> ApplicationDraftRepositoryFactory:
    """Как и `api/main.py`'s `_repository_factory`: каждый вызов открывает
    и мигрирует своё соединение; backend (SQLite/PostgreSQL) выбирается по
    схеме `database.url` в `persistence.factory.create_vacancy_repository`."""

    def factory() -> VacancyRepositoryBackend:
        return create_vacancy_repository(database)

    return factory


def _require_configured_qwen_settings() -> QwenSettings:
    """Как и `api/main.py`'s одноимённая функция: пустой `QWEN_MODEL` не
    мешает процессу запуститься и отвечать на schema/preflight-only вызовы
    — обнаруживается только здесь, лениво, в момент реального `accept`."""

    qwen = QwenSettings()
    if not qwen.model:
        raise ApplicationDraftNetworkError("QWEN_MODEL не задан — генерация черновика недоступна")
    return qwen


def _resolve_model_name() -> str:
    """Дешёвая проверка сконфигурированного имени модели (TASK-011,
    ADR-049) — не создаёт `LMStudioApplicationDraftGenerator` и не
    открывает сеть, как и парная `api/main.py`'s `_resolve_model_name`."""

    return _require_configured_qwen_settings().model


def _generator_factory() -> LMStudioApplicationDraftGenerator:
    """Ленивая фабрика generator'а: `QwenSettings()` читается и клиент
    создаётся только когда эту функцию реально вызывает
    `prepare_application_draft` — то есть уже после accept и повторной
    backend-валидации (см. `mcp_server.py`, `application/
    application_drafts.py`)."""

    return LMStudioApplicationDraftGenerator(_require_configured_qwen_settings())


def main() -> None:
    database = DatabaseSettings()
    mcp = create_mcp_server(
        _repository_factory(database),
        generator_factory=_generator_factory,
        model_resolver=_resolve_model_name,
    )
    mcp.run()


if __name__ == "__main__":
    main()
