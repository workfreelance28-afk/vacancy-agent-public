from pathlib import Path

import pytest
from mcp.server import MCPServer

import vacancy_agent.mcp_main as mcp_main_module
from vacancy_agent.config.settings import DatabaseSettings
from vacancy_agent.mcp_main import (
    _generator_factory,
    _repository_factory,
    _resolve_model_name,
    main,
)
from vacancy_agent.persistence.sqlite_repository import SqliteVacancyRepository


def test_repository_factory_builds_working_migrated_repository(tmp_path: Path) -> None:
    db_path = tmp_path / "vacancies.db"
    database = DatabaseSettings(url=f"sqlite:///{db_path}")

    factory = _repository_factory(database)

    repository = factory()
    try:
        assert isinstance(repository, SqliteVacancyRepository)
        assert repository.get_vacancy("unknown") is None
    finally:
        repository.close()
    assert db_path.exists()


def test_main_runs_stdio_transport_exactly_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Entrypoint не должен случайно поднимать сетевой transport
    (`sse`/`streamable-http`) — TASK-S2.6 явно исключает remote MCP и
    публичный endpoint. Реальный `mcp.run()` блокирует процесс на чтении
    stdin, поэтому здесь он подменяется."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'vacancies.db'}")

    calls: list[str] = []

    def fake_run(self: MCPServer, transport: str = "stdio", **kwargs: object) -> None:
        calls.append(transport)

    monkeypatch.setattr(MCPServer, "run", fake_run)

    main()

    assert calls == ["stdio"]


def test_main_wires_generator_factory_and_model_resolver_into_create_mcp_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Найдено ревью Codex: `main()` раньше вызывал `create_mcp_server`
    только с `repository_factory` — реальный MCP-сервер никогда не мог
    сгенерировать черновик ни при каком `QWEN_MODEL`, хотя
    `create_mcp_server` уже умел принимать `generator_factory`/
    `model_resolver` (это было доказано только fake-based unit-тестами
    `test_mcp_server.py`, не реальной точкой входа)."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'vacancies.db'}")

    calls: list[dict[str, object]] = []

    def fake_create_mcp_server(
        repository_factory: object,
        generator_factory: object = None,
        model_resolver: object = None,
    ) -> MCPServer:
        calls.append({"generator_factory": generator_factory, "model_resolver": model_resolver})
        return MCPServer("fake")

    def fake_run(self: MCPServer, transport: str = "stdio", **kwargs: object) -> None:
        pass

    monkeypatch.setattr(mcp_main_module, "create_mcp_server", fake_create_mcp_server)
    monkeypatch.setattr(MCPServer, "run", fake_run)

    main()

    assert len(calls) == 1
    assert calls[0]["generator_factory"] is _generator_factory
    assert calls[0]["model_resolver"] is _resolve_model_name
