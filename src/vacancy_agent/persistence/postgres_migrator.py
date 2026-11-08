"""Минимальный раннер PostgreSQL-миграций (TASK-S2.1).

Тот же принцип, что `persistence/migrator.py` (SQLite, ADR-006): без
SQLAlchemy/Alembic — версионированные `.sql`-файлы и таблица учёта
`schema_migrations`, применяются по одной транзакции на файл. Не общий код
с SQLite-раннером: `migrator.py` типизирован на `sqlite3.Connection` и его
конкретные исключения (`sqlite3.OperationalError`), а psycopg — отдельная
библиотека с собственным типом соединения/курсора и собственной иерархией
исключений (`psycopg.Error`) — общая функция потребовала бы либо `Any`,
либо протокол ради одного места вызова, не оправданный для ~40 строк кода.

Как и в SQLite-версии, каждый файл — своя транзакция: ошибка в середине
файла откатывает все операторы этого файла (не только последний), а
бухгалтерия `schema_migrations` не коммитится для него — при повторном
запуске файл переигрывается полностью."""

import logging
import re
from pathlib import Path
from typing import Any

import psycopg

_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_MIGRATION_LOCK_KEY = 2_025_090_016
logger = logging.getLogger("vacancy_agent.persistence.postgres_migrator")


def apply_migrations(connection: psycopg.Connection[Any], migrations_dir: Path) -> None:
    """Применяет все `.sql`-файлы из `migrations_dir` в порядке имени файла.

    Файлы, уже отмеченные в `schema_migrations`, пропускаются — повторный
    вызов на той же базе безопасен (TASK-S2.1, критерий «повторный запуск
    PostgreSQL migrations безопасен»)."""

    _acquire_migration_lock(connection)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    filename TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        connection.commit()

        # Явный `tuple_row`, а не `row[0]` на курсоре без параметра: реальный
        # вызывающий код (`postgres_repository.py`) открывает `connection` с
        # `row_factory=psycopg.rows.dict_row` — тогда `row[0]` на dict-строке
        # падает `KeyError` (найдено round-2 прогоном против реальной
        # PostgreSQL). Мигратору не нужно и не следует зависеть от row_factory,
        # выбранного вызывающим кодом для его собственных запросов.
        with connection.cursor(row_factory=psycopg.rows.tuple_row) as cursor:
            cursor.execute("SELECT filename FROM schema_migrations")
            applied = {row[0] for row in cursor.fetchall()}

        for path in sorted(migrations_dir.glob("*.sql")):
            if path.name in applied:
                continue
            _apply_one(connection, path)
    except BaseException:
        _release_migration_lock(connection, suppress_errors=True)
        raise
    else:
        _release_migration_lock(connection, suppress_errors=False)


def _acquire_migration_lock(connection: psycopg.Connection[Any]) -> None:
    """Сериализует migrator между API и worker одного PostgreSQL-кластера.

    Session-level advisory lock переживает промежуточные `commit()` — каждый
    migration-файл по-прежнему остаётся отдельной транзакцией. PostgreSQL сам
    освобождает lock при потере соединения, а штатный путь делает это явно.
    """

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK_KEY,))


def _release_migration_lock(
    connection: psycopg.Connection[Any], *, suppress_errors: bool
) -> None:
    try:
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_KEY,))
        connection.commit()
    except Exception as exc:
        try:
            connection.rollback()
        except Exception:
            pass
        if suppress_errors:
            logger.warning(
                "Не удалось освободить PostgreSQL migration lock: %s",
                type(exc).__name__,
            )
        else:
            raise


def _apply_one(connection: psycopg.Connection[Any], path: Path) -> None:
    try:
        with connection.cursor() as cursor:
            for statement in _split_statements(path.read_text(encoding="utf-8")):
                cursor.execute(statement)
            cursor.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
            )
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def _split_statements(script: str) -> list[str]:
    """Делит файл миграции на отдельные операторы по `;`, сначала вырезая
    `--`-комментарии построчно (найдено round-2 прогоном против реальной
    PostgreSQL, TASK-S2.1 критерий готовности №8: `0001_initial.sql` содержит
    комментарий на русском с литеральным `;` — "защищает от дубликатов;
    списки хранятся..." — наивный split по всему файлу резал оператор
    посреди этого комментария и передавал psycopg синтаксически неверный
    кусок текста). Комментарии не несут SQL-семантики, поэтому их можно
    вырезать целиком до разбиения — остаётся тот же простой приём, что
    `persistence/migrator._split_statements` (SQLite), но безопасный для
    любого будущего комментария с `;` в этих же `.sql`-файлах; по-прежнему
    не рассчитан на произвольный/недоверенный SQL со строковыми литералами,
    содержащими `;` или `--`."""

    without_comments = _LINE_COMMENT_RE.sub("", script)
    return [statement.strip() for statement in without_comments.split(";") if statement.strip()]
