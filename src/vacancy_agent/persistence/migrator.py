"""Минимальный раннер миграций SQLite.

Версионированные `.sql`-файлы вместо Alembic/SQLAlchemy — решение
зафиксировано в `docs/DECISIONS.md`, ADR-006 («единственный пользователь,
один процесс, простая схема — ORM и внешний движок миграций избыточны»).
Применённые файлы учитываются в `schema_migrations`, чтобы повторное
создание репозитория для той же базы не пересоздавало схему заново.

Каждый файл применяется и отмечается как применённый в собственной
транзакции: `connection.executescript()` из стандартной библиотеки перед
выполнением скрипта неявно коммитит любую открытую транзакцию и не даёт
атомарно откатить уже выполненные операторы скрипта при ошибке в
последующем — поэтому каждый оператор файла выполняется по отдельности
через explicit `BEGIN`, что даёт настоящий откат при ошибке (проверено
эмпирически: `executescript()` необратимо применяет часть операторов до
места сбоя, ручной `BEGIN`+`execute()` — нет). Если файл всё же упадёт
посередине, ни один из его операторов не останется в базе, а бухгалтерия
`schema_migrations` не будет закоммичена только для этого файла — при
повторном запуске он будет полностью переигран, а не наполовину.
"""

import sqlite3
from pathlib import Path


def apply_migrations(connection: sqlite3.Connection, migrations_dir: Path) -> None:
    """Применяет все `.sql`-файлы из `migrations_dir` в порядке имени файла.

    Файлы, уже отмеченные в `schema_migrations`, пропускаются — повторный
    вызов на той же базе безопасен (см. критерий TASK-004 «есть миграция
    или воспроизводимое создание схемы»).
    """

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    connection.commit()

    applied = {
        row[0] for row in connection.execute("SELECT filename FROM schema_migrations").fetchall()
    }

    for path in sorted(migrations_dir.glob("*.sql")):
        if path.name in applied:
            continue
        _apply_one(connection, path)


def _apply_one(connection: sqlite3.Connection, path: Path) -> None:
    connection.execute("BEGIN")
    try:
        for statement in _split_statements(path.read_text(encoding="utf-8")):
            connection.execute(statement)
        connection.execute("INSERT INTO schema_migrations (filename) VALUES (?)", (path.name,))
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def _split_statements(script: str) -> list[str]:
    """Делит файл миграции на отдельные операторы по `;`.

    Простое разбиение по разделителю, без разбора строковых литералов —
    достаточно для собственных `.sql`-файлов проекта (см. `migrations/`),
    не для произвольного/недоверенного SQL.
    """

    return [statement.strip() for statement in script.split(";") if statement.strip()]
