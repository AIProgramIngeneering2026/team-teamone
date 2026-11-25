"""Версионированный запуск пронумерованных SQL-миграций для SQLite."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3


DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def _iter_sql_statements(sql_text: str) -> list[str]:
    """Разбирает SQL-скрипт на отдельные операторы с учётом многострочных блоков."""
    statements: list[str] = []
    buffer: list[str] = []

    for raw_line in sql_text.splitlines():
        stripped = raw_line.strip()
        if not buffer and (not stripped or stripped.startswith("--")):
            continue
        buffer.append(raw_line)
        candidate = "\n".join(buffer).strip()
        if candidate.endswith(";") and sqlite3.complete_statement(candidate):
            statements.append(candidate)
            buffer.clear()

    tail = "\n".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )


def get_applied_migrations(conn: sqlite3.Connection) -> list[str]:
    """Возвращает отсортированный список версий уже применённых миграций."""
    row = conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name = 'schema_migrations'
        """
    ).fetchone()
    if row is None:
        return []
    rows = conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version ASC"
    ).fetchall()
    return [str(item[0]) for item in rows]


def apply_migrations(
    conn: sqlite3.Connection,
    migrations_dir: str | Path = DEFAULT_MIGRATIONS_DIR,
) -> list[str]:
    """Применяет все неприменённые пронумерованные .sql-файлы в отдельных транзакциях.

    Возвращает список версий миграций, применённых в этом вызове.
    Повторный запуск на уже мигрированной БД ничего не меняет и возвращает [].
    """
    directory = Path(migrations_dir)
    if not directory.is_dir():
        raise FileNotFoundError(f"Каталог миграций не найден: {directory}")

    sql_files = sorted(directory.glob("*.sql"), key=lambda path: path.name)
    previous_isolation = conn.isolation_level
    conn.isolation_level = None
    applied_now: list[str] = []

    try:
        conn.execute("PRAGMA foreign_keys = ON")
        _ensure_migrations_table(conn)
        already_applied = set(get_applied_migrations(conn))

        for sql_file in sql_files:
            version = sql_file.name
            if version in already_applied:
                continue

            sql_text = sql_file.read_text(encoding="utf-8")
            statements = _iter_sql_statements(sql_text)
            applied_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, applied_at),
                )
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

            applied_now.append(version)
            already_applied.add(version)
    finally:
        conn.isolation_level = previous_isolation

    return applied_now


def apply_migrations_to_path(
    db_path: str | Path,
    migrations_dir: str | Path = DEFAULT_MIGRATIONS_DIR,
) -> list[str]:
    """Открывает файл SQLite, включает WAL-режим и применяет все новые миграции."""
    path = Path(db_path)
    if path.parent and str(path.parent) != ".":
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        return apply_migrations(conn, migrations_dir=migrations_dir)
    finally:
        conn.close()
