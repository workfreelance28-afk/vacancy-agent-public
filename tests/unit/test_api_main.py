from pathlib import Path

import pytest
from fastapi import FastAPI

import vacancy_agent.api.main as api_main_module
from vacancy_agent.api.main import (
    _generator_factory,
    _repository_factory,
    _resolve_model_name,
    main,
)
from vacancy_agent.config.settings import ApiSettings, DatabaseSettings
from vacancy_agent.domain.application_draft_errors import ApplicationDraftNetworkError
from vacancy_agent.integrations.application_draft_client import LMStudioApplicationDraftGenerator
from vacancy_agent.persistence.sqlite_repository import SqliteVacancyRepository


def test_repository_factory_builds_working_repository(tmp_path: Path) -> None:
    db_path = tmp_path / "vacancies.db"
    database = DatabaseSettings(url=f"sqlite:///{db_path}")

    factory = _repository_factory(database)

    with factory() as repository:
        assert isinstance(repository, SqliteVacancyRepository)
        assert repository.count_vacancies() == 0
    assert db_path.exists()


def test_database_and_api_settings_ignore_unrelated_invalid_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Найдено ревью TASK-009 (`api/main.py:37-38`): `main()` раньше вызывал
    `config.load_settings()`, который собирает всю `Settings` целиком —
    невалидное значение в HH/SuperJob/фильтрах/Telegram/Qwen (которые API
    вообще не использует) не давало бы `vacancy-agent-api` запуститься.
    `DatabaseSettings`/`ApiSettings` — независимые `BaseSettings`-секции и
    должны собираться отдельно, не через посторонние секции конфигурации."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "not-valid-json")
    monkeypatch.setenv("HH_PERIOD_DAYS", "not-a-number")
    monkeypatch.setenv("FILTER_SALARY_MIN", "not-a-number")
    monkeypatch.setenv("QWEN_MIN_SCORE", "not-a-number")

    # Само наличие этих значений подтверждает, что полная `Settings()` упала
    # бы: это ровно то поведение, из-за которого API раньше отказывался
    # запускаться. Здесь проверяется прямо противоположное — что узкие
    # секции API это не задевает.
    database = DatabaseSettings()
    api = ApiSettings()

    assert database.url == "sqlite:///./data/vacancy_agent.db"
    assert api.host == "127.0.0.1"
    assert api.port == 8000


def test_main_starts_despite_unrelated_invalid_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Найдено повторным ревью: предыдущий тест проверял только то, что
    `DatabaseSettings`/`ApiSettings` по отдельности конструируются — не то,
    что сама точка входа `main()` действительно их использует, а не
    `load_settings()`. Он остался бы зелёным, даже если бы `main()` попутно
    продолжала звать `load_settings()` где-то ещё. Здесь вызывается
    настоящий `main()` (с подменённым `uvicorn.run`, чтобы не поднимать
    реальный блокирующий сервер) — при регрессии тест упал бы с
    `SettingsError` на невалидном `TELEGRAM_ALLOWED_CHAT_IDS` ещё до вызова
    `uvicorn.run`."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'vacancies.db'}")
    monkeypatch.setenv("API_PORT", "8123")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "not-valid-json")
    monkeypatch.setenv("HH_PERIOD_DAYS", "not-a-number")

    calls: list[dict[str, object]] = []

    def fake_run(app: FastAPI, *, host: str, port: int) -> None:
        calls.append({"app": app, "host": host, "port": port})

    monkeypatch.setattr(api_main_module.uvicorn, "run", fake_run)

    main()

    assert len(calls) == 1
    app = calls[0]["app"]
    assert isinstance(app, FastAPI)
    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["port"] == 8123
    # TASK-011 ADR-049: `main()` должен передавать `model_resolver` в
    # `create_app` наравне с `generator_factory` — без него POST всегда
    # отвечал бы `503`, даже с настроенным `QWEN_MODEL`.
    assert app.state.generator_factory is api_main_module._generator_factory
    assert app.state.model_resolver is api_main_module._resolve_model_name


def test_generator_factory_raises_without_real_network_call_when_model_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TASK-011: `QWEN_MODEL` пустой не должен приводить к попытке реального
    сетевого запроса — `_generator_factory` обязана поднять
    `ApplicationDraftNetworkError` до конструирования HTTP-клиента с
    30-секундным (и тем более 300-секундным draft) timeout. Найдено при
    ручной проверке: запуск без изоляции от реального `.env` разработчика
    (с непустым `QWEN_MODEL`/`QWEN_BASE_URL`) привёл к реальной попытке
    обращения к LM Studio из этого самого пути — эта проверка фиксирует
    именно тот код, который должен был её предотвратить, изолированно от
    реального `.env`."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("QWEN_MODEL", raising=False)

    with pytest.raises(ApplicationDraftNetworkError):
        _generator_factory()


def test_generator_factory_builds_generator_without_network_call_when_model_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Конструирование `LMStudioApplicationDraftGenerator` (создание
    `httpx.Client`) само по себе не открывает соединение — сеть трогает
    только `.generate()`, который здесь не вызывается."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QWEN_MODEL", "configured-model")
    monkeypatch.setenv("QWEN_BASE_URL", "http://localhost:1234/v1")

    generator = _generator_factory()

    try:
        assert isinstance(generator, LMStudioApplicationDraftGenerator)
        assert generator.model == "configured-model"
    finally:
        generator.close()


def test_resolve_model_name_raises_without_creating_generator_when_model_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TASK-011 ADR-049: `_resolve_model_name` — дешёвая проверка,
    отдельная от `_generator_factory` — должна вести себя так же при
    пустом `QWEN_MODEL` (`ApplicationDraftNetworkError`, не сырая ошибка)."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("QWEN_MODEL", raising=False)

    with pytest.raises(ApplicationDraftNetworkError):
        _resolve_model_name()


def test_resolve_model_name_returns_configured_model_without_creating_generator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`_resolve_model_name` должна вернуть ровно то же имя модели, что
    получил бы `LMStudioApplicationDraftGenerator.model`, но не создавая
    его — только `QwenSettings()` (TASK-011, ADR-049: `prepare_
    application_draft` вызывает её раньше `generator_factory`, чтобы решить
    reuse-vs-regenerate без построения generator'а)."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QWEN_MODEL", "configured-model")
    monkeypatch.setenv("QWEN_BASE_URL", "http://localhost:1234/v1")

    assert _resolve_model_name() == "configured-model"
