"""Реализация `vacancy_agent.domain.ports.VacancyRepository` и схема БД.

SQLite без ORM (см. `docs/DECISIONS.md`, ADR-006/ADR-011): схема —
версионированные `.sql`-файлы в `migrations/`, применяются
`migrator.apply_migrations`.

`PersistenceError`/`VacancyNotFoundError` переэкспортированы для удобства
из `domain.repository_errors` — это часть контракта `VacancyRepository`
(домен), а не деталь конкретной реализации на SQLite.
"""

from vacancy_agent.domain.repository_errors import PersistenceError, VacancyNotFoundError
from vacancy_agent.persistence.sqlite_repository import SqliteVacancyRepository

__all__ = [
    "PersistenceError",
    "SqliteVacancyRepository",
    "VacancyNotFoundError",
]
