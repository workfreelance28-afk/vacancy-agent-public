"""Unit-тесты выбора backend-адаптера по схеме `DATABASE_URL`
(`persistence/factory.py`, TASK-S2.1). Не открывают реальное сетевое
PostgreSQL-соединение: `postgresql://`-ветка подменяет
`PostgresVacancyRepository.from_settings` заглушкой — эти тесты проверяют
только диспетчеризацию по схеме, не сам PostgreSQL-адаптер (см.
`tests/integration/test_postgres_repository_contract.py` для контрактных
тестов на реальной тестовой PostgreSQL)."""

from pathlib import Path
from typing import Self

import pytest

from vacancy_agent.config.settings import DatabaseSettings
from vacancy_agent.persistence import factory as factory_module
from vacancy_agent.persistence.factory import create_scan_job_queue, create_vacancy_repository
from vacancy_agent.persistence.sqlite_repository import SqliteVacancyRepository


def test_sqlite_url_selects_sqlite_repository(tmp_path: Path) -> None:
    settings = DatabaseSettings(url=f"sqlite:///{tmp_path}/db.sqlite3")

    with create_vacancy_repository(settings) as repository:
        assert isinstance(repository, SqliteVacancyRepository)


def test_postgres_url_selects_postgres_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    created_with: list[DatabaseSettings] = []

    class _StubPostgresRepository:
        @classmethod
        def from_settings(cls, settings: DatabaseSettings) -> Self:
            created_with.append(settings)
            return cls()

        def close(self) -> None:
            pass

    monkeypatch.setattr(factory_module, "PostgresVacancyRepository", _StubPostgresRepository)
    settings = DatabaseSettings(url="postgresql://user:pass@localhost/vacancy_agent_test")

    repository = create_vacancy_repository(settings)

    assert isinstance(repository, _StubPostgresRepository)
    assert created_with == [settings]


def test_unknown_scheme_is_rejected_explicitly() -> None:
    settings = DatabaseSettings(url="mysql://localhost/db")

    with pytest.raises(ValueError, match="Неизвестная схема"):
        create_vacancy_repository(settings)


def test_unknown_scheme_error_does_not_leak_credentials() -> None:
    settings = DatabaseSettings(url="mysql://user:s3cr3t@localhost/db")

    with pytest.raises(ValueError) as exc_info:
        create_vacancy_repository(settings)

    assert "s3cr3t" not in str(exc_info.value)


def test_scan_job_queue_postgres_url_selects_postgres_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`create_scan_job_queue` (TASK-S2.2) — та же диспетчеризация по
    схеме, что `create_vacancy_repository`, но без сетевого PostgreSQL:
    `PostgresScanJobQueue.from_settings` подменяется заглушкой."""

    created_with: list[DatabaseSettings] = []

    class _StubQueue:
        @classmethod
        def from_settings(cls, settings: DatabaseSettings) -> Self:
            created_with.append(settings)
            return cls()

        def close(self) -> None:
            pass

    monkeypatch.setattr(factory_module, "PostgresScanJobQueue", _StubQueue)
    settings = DatabaseSettings(url="postgresql://user:pass@localhost/vacancy_agent_test")

    queue = create_scan_job_queue(settings)

    assert isinstance(queue, _StubQueue)
    assert created_with == [settings]


def test_scan_job_queue_rejects_sqlite_explicitly() -> None:
    """TASK-S2.2, «Архитектурная граница»: «Queue API доступен только с
    PostgreSQL. Для SQLite он отвечает безопасным 503» — на уровне фабрики
    это явный `ValueError`, который HTTP-слой (`api/app.py`) превращает в
    `503`, не давая SQLite тихо остаться без очереди без объяснения."""

    settings = DatabaseSettings(url="sqlite:///./data/vacancy_agent.db")

    with pytest.raises(ValueError, match="доступна только для"):
        create_scan_job_queue(settings)


def test_scan_job_queue_unknown_scheme_error_does_not_leak_credentials() -> None:
    settings = DatabaseSettings(url="mysql://user:s3cr3t@localhost/db")

    with pytest.raises(ValueError) as exc_info:
        create_scan_job_queue(settings)

    assert "s3cr3t" not in str(exc_info.value)
