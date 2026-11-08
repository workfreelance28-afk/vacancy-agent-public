"""Concurrent startup contract for PostgreSQL migrations (TASK-S2.3)."""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from vacancy_agent.persistence.postgres_migrator import apply_migrations

_TEST_DSN = os.environ.get("TEST_DATABASE_URL", "")
_CONFIRM_ENV_VAR = "TEST_DATABASE_URL_CONFIRM_DESTRUCTIVE"
_CONFIRMED = os.environ.get(_CONFIRM_ENV_VAR, "") == "1"
_MIGRATIONS_DIR = (
    Path(__file__).parents[2]
    / "src"
    / "vacancy_agent"
    / "persistence"
    / "postgres_migrations"
)

pytestmark = pytest.mark.skipif(
    not _TEST_DSN,
    reason="TEST_DATABASE_URL не задан — concurrent migration test пропущен",
)


def _require_safe_test_dsn(dsn: str) -> None:
    if not _CONFIRMED:
        pytest.fail(f"Для destructive PostgreSQL test требуется {_CONFIRM_ENV_VAR}=1")
    db_name = dsn.rsplit("/", 1)[-1].split("?", 1)[0]
    if "test" not in db_name.lower():
        pytest.fail("Concurrent migration test разрешён только для БД с 'test' в имени")


def test_two_processes_can_apply_migrations_on_clean_schema() -> None:
    """API и worker могут одновременно стартовать на полностью чистой схеме."""

    _require_safe_test_dsn(_TEST_DSN)
    schema_name = f"migration_test_{uuid4().hex}"
    barrier = Barrier(2)

    with psycopg.connect(_TEST_DSN) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))

    def migrate() -> None:
        with psycopg.connect(_TEST_DSN) as connection:
            connection.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name))
            )
            connection.commit()
            barrier.wait(timeout=10)
            apply_migrations(connection, _MIGRATIONS_DIR)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(migrate) for _ in range(2)]
            for future in futures:
                future.result(timeout=30)

        with psycopg.connect(_TEST_DSN) as connection:
            connection.execute(
                sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name))
            )
            filenames = {
                row[0]
                for row in connection.execute(
                    "SELECT filename FROM schema_migrations ORDER BY filename"
                ).fetchall()
            }
        assert filenames == {path.name for path in _MIGRATIONS_DIR.glob("*.sql")}
    finally:
        with psycopg.connect(_TEST_DSN) as admin:
            admin.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema_name)
                )
            )
