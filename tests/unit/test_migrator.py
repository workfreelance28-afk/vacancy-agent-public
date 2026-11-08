import sqlite3
from pathlib import Path

import pytest

from vacancy_agent.persistence.migrator import apply_migrations


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _applied_filenames(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT filename FROM schema_migrations")}


def test_applies_all_statements_in_a_single_migration_file(tmp_path: Path) -> None:
    (tmp_path / "0001_two_tables.sql").write_text(
        "CREATE TABLE a (id INTEGER); CREATE TABLE b (id INTEGER);", encoding="utf-8"
    )
    connection = sqlite3.connect(":memory:")

    apply_migrations(connection, tmp_path)

    assert _table_exists(connection, "a")
    assert _table_exists(connection, "b")
    assert _applied_filenames(connection) == {"0001_two_tables.sql"}


def test_is_idempotent_on_repeated_calls(tmp_path: Path) -> None:
    (tmp_path / "0001_initial.sql").write_text(
        "CREATE TABLE a (id INTEGER);", encoding="utf-8"
    )
    connection = sqlite3.connect(":memory:")

    apply_migrations(connection, tmp_path)
    apply_migrations(connection, tmp_path)  # не должно упасть и не должно повторно выполнить файл

    assert _applied_filenames(connection) == {"0001_initial.sql"}


def test_files_are_applied_in_filename_order(tmp_path: Path) -> None:
    (tmp_path / "0002_second.sql").write_text(
        "CREATE TABLE second (id INTEGER);", encoding="utf-8"
    )
    (tmp_path / "0001_first.sql").write_text(
        "CREATE TABLE first (id INTEGER);", encoding="utf-8"
    )
    connection = sqlite3.connect(":memory:")

    apply_migrations(connection, tmp_path)

    assert _applied_filenames(connection) == {"0001_first.sql", "0002_second.sql"}


def test_failed_statement_leaves_no_partial_state_and_is_not_marked_applied(
    tmp_path: Path,
) -> None:
    (tmp_path / "0001_ok.sql").write_text("CREATE TABLE ok (id INTEGER);", encoding="utf-8")
    (tmp_path / "0002_bad.sql").write_text(
        "CREATE TABLE partial (id INTEGER); THIS IS NOT VALID SQL;", encoding="utf-8"
    )
    connection = sqlite3.connect(":memory:")

    with pytest.raises(sqlite3.OperationalError):
        apply_migrations(connection, tmp_path)

    # 0001 успел примениться и зафиксироваться до сбоя на 0002.
    assert _table_exists(connection, "ok")
    assert "0001_ok.sql" in _applied_filenames(connection)

    # 0002 упал на втором операторе — первый (CREATE TABLE partial) не должен
    # остаться в базе, а сам файл не должен быть отмечен как применённый.
    assert not _table_exists(connection, "partial")
    assert "0002_bad.sql" not in _applied_filenames(connection)


def test_retrying_after_a_fixed_migration_file_succeeds(tmp_path: Path) -> None:
    ok_path = tmp_path / "0001_ok.sql"
    ok_path.write_text("CREATE TABLE ok (id INTEGER);", encoding="utf-8")
    bad_path = tmp_path / "0002_will_be_fixed.sql"
    bad_path.write_text(
        "CREATE TABLE broken (id INTEGER); THIS IS NOT VALID SQL;", encoding="utf-8"
    )
    connection = sqlite3.connect(":memory:")

    with pytest.raises(sqlite3.OperationalError):
        apply_migrations(connection, tmp_path)

    bad_path.write_text("CREATE TABLE fixed (id INTEGER);", encoding="utf-8")
    apply_migrations(connection, tmp_path)

    assert _table_exists(connection, "fixed")
    assert not _table_exists(connection, "broken")
    assert _applied_filenames(connection) == {"0001_ok.sql", "0002_will_be_fixed.sql"}
