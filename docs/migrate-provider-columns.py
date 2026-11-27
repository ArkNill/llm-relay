"""Bring the provider_* tables of a PostgreSQL store up to the current schema (2026-11-27).

Adds what ``llm_relay.antigravity.store`` defines and the database lacks, in this
order: missing tables (``provider_blobs``), missing columns of existing tables
(the 15 ``NEW_REQUEST_COLUMNS`` of ``provider_requests``), missing indexes
(``idx_provider_blobs_key``, unique). Every statement comes from the store
(``PG_DDL``, ``column_defs``), so the result matches what the relay creates on
SQLite. ``plan()`` builds the list from the observed schema and can be tested
without a database.

The relay does not change the PostgreSQL schema itself. Until this has run it
keeps writing rows with the columns that exist (the new ones are left out of
the INSERT); it looks at the schema again about once a minute while something
is missing and starts filling the new columns, and storing blobs, without a
restart.

Safe to run while the relay is up:
- ``ADD COLUMN`` of a nullable column without DEFAULT only changes the catalogue
  (no table rewrite, existing rows read NULL). ``plan()`` refuses (exit 2) a
  column whose type carries NOT NULL or DEFAULT, so that stays true.
- It still needs a brief ACCESS EXCLUSIVE lock on ``provider_requests``: each
  statement waits at most --lock-timeout behind running queries, then gives up
  (exit 1; the statements before it are kept and all of them are IF NOT EXISTS,
  so running it again resumes).
- Only ``provider_*`` tables are named (any other name is refused), so the
  Claude Code proxy's tables (``requests``, ``conversation_turns``, ...) are
  neither referenced nor locked.

Usage:
    python docs/migrate-provider-columns.py --pg "$LLM_RELAY_DB"            # report and print the plan
    python docs/migrate-provider-columns.py --pg "$LLM_RELAY_DB" --apply    # run it

Idempotent. Nothing is dropped, rewritten or updated.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    import psycopg
except ImportError:  # checked in main(), so plan() stays importable without the driver
    psycopg = None

try:
    from llm_relay.antigravity import store
except ImportError:  # a checkout without the package installed
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from llm_relay.antigravity import store

ALL_TABLES = store.ALL_TABLES
INDEXES = store.INDEXES
INDEX_NAMES = tuple(index[0] for index in INDEXES)
TABLE_PREFIX = "provider_"

_CREATE_TABLE = re.compile(r"^CREATE TABLE IF NOT EXISTS (\w+) \(")
_CREATE_INDEX = re.compile(r"^CREATE (?:UNIQUE )?INDEX IF NOT EXISTS (\w+) ON (\w+) \(")
# Type fragments that would make ADD COLUMN more than a catalogue change (or fail on existing rows).
_UNSAFE_TYPE = re.compile(r"\b(NOT\s+NULL|DEFAULT)\b", re.IGNORECASE)


def _ddl_by_name() -> Tuple[Dict[str, str], Dict[str, str]]:
    """``PG_DDL`` split into {table: CREATE TABLE} and {index: CREATE INDEX}."""
    tables: Dict[str, str] = {}
    indexes: Dict[str, str] = {}
    for statement in store.PG_DDL:
        match = _CREATE_TABLE.match(statement)
        if match:
            tables[match.group(1)] = statement
            continue
        match = _CREATE_INDEX.match(statement)
        if match:
            indexes[match.group(1)] = statement
            continue
        raise ValueError("unexpected statement in store.PG_DDL: {}".format(statement.splitlines()[0]))
    return tables, indexes


def _check_table_name(table: str) -> None:
    if not table.startswith(TABLE_PREFIX):
        raise ValueError("refusing to touch table {!r}: only {}* tables are migrated here".format(table, TABLE_PREFIX))


def plan(existing: Dict[str, Set[str]], indexes: Set[str]) -> List[str]:
    """The DDL that brings the observed schema up to ``store``; empty when nothing is missing.

    ``existing`` maps each provider table that exists to its column names (a
    missing table has no key); ``indexes`` holds the names of the indexes that
    exist. Statements come in this order: ``CREATE TABLE IF NOT EXISTS`` for
    missing tables, ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` for each column
    an existing table lacks, ``CREATE [UNIQUE] INDEX IF NOT EXISTS`` for
    missing indexes.

    Raises ValueError for a table name outside ``provider_*`` (in ``existing``
    or in the store's tables and indexes) and for a column to be added to an
    existing table whose PostgreSQL type has NOT NULL or DEFAULT: those are
    not catalogue-only changes and are left to a person.
    """
    for table in list(existing) + list(ALL_TABLES) + [index[1] for index in INDEXES]:
        _check_table_name(table)
    create_table, create_index = _ddl_by_name()

    statements: List[str] = []
    for table in ALL_TABLES:
        if table not in existing:
            statements.append(create_table[table])
    for table in ALL_TABLES:
        if table not in existing:
            continue
        for column, pg_type in store.column_defs(table, "pg"):
            if column in existing[table]:
                continue
            if _UNSAFE_TYPE.search(pg_type):
                raise ValueError("column {}.{} ({}) cannot be added to an existing table here: NOT NULL or "
                                 "DEFAULT is not a catalogue-only change".format(table, column, pg_type))
            # Names and types come from store._COLUMNS, never from input.
            statements.append("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} {}".format(table, column, pg_type))
    for name in INDEX_NAMES:
        if name not in indexes:
            statements.append(create_index[name])
    return statements


def _placeholders(values: Tuple[str, ...]) -> str:
    return ", ".join(["%s"] * len(values))


def read_schema(conn) -> Tuple[Dict[str, Set[str]], Set[str]]:
    """(existing, indexes) for ``plan()``: provider tables with their columns, provider index names. Read-only."""
    existing: Dict[str, Set[str]] = {
        row[0]: set() for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
            " AND table_name IN ({})".format(_placeholders(ALL_TABLES)), ALL_TABLES,
        ).fetchall()
    }
    for table, column in conn.execute(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = current_schema()"
        " AND table_name IN ({})".format(_placeholders(ALL_TABLES)), ALL_TABLES,
    ).fetchall():
        if table in existing:
            existing[table].add(column)
    indexes = {
        row[0] for row in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
            " AND tablename IN ({}) AND indexname IN ({})".format(
                _placeholders(ALL_TABLES), _placeholders(INDEX_NAMES)),
            ALL_TABLES + INDEX_NAMES,
        ).fetchall()
    }
    return existing, indexes


def _report(conn) -> Tuple[Dict[str, Set[str]], Set[str]]:
    """Print tables (with row counts), missing columns and indexes; return what ``read_schema`` saw."""
    existing, indexes = read_schema(conn)
    for table in ALL_TABLES:
        if table not in existing:
            print("table {}: missing".format(table))
            continue
        # The name comes from the fixed ALL_TABLES tuple, never from input.
        rows = conn.execute("SELECT count(*) FROM " + table).fetchone()[0]
        missing = [column for column, _ in store.column_defs(table, "pg") if column not in existing[table]]
        print("table {}: present, {} rows, {}".format(
            table, rows, "missing columns: " + ", ".join(missing) if missing else "all columns present"))
    for name in INDEX_NAMES:
        print("index {}: {}".format(name, "present" if name in indexes else "missing"))
    return existing, indexes


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Add missing provider_* tables, columns and indexes to PostgreSQL.")
    parser.add_argument("--pg", required=True, help="PostgreSQL DSN (postgresql://...)")
    parser.add_argument("--apply", action="store_true", help="run the DDL (default: report only)")
    parser.add_argument("--lock-timeout", default="5s", help="how long one statement may wait for a lock (default 5s)")
    args = parser.parse_args(argv)
    if not args.pg.startswith(("postgresql://", "postgres://")):
        print("ERROR: --pg must be a postgresql:// or postgres:// DSN")
        return 2
    if psycopg is None:
        print("ERROR: psycopg not installed. Run: pip install psycopg[binary]")
        return 1

    with psycopg.connect(args.pg, autocommit=True) as conn:
        try:
            statements = plan(*_report(conn))
        except ValueError as exc:
            print("ERROR: {}".format(exc))
            return 2
        if not statements:
            print("Nothing to do.")
            return 0
        print("Planned statements:")
        for statement in statements:
            print(statement + ";")
        if not args.apply:
            print("Report only: run again with --apply to run them.")
            return 0

        conn.execute("SELECT set_config('lock_timeout', %s, false)", (args.lock_timeout,))
        for statement in statements:
            first_line = statement.splitlines()[0].rstrip(" (")
            started = time.monotonic()
            try:
                conn.execute(statement)
            except psycopg.errors.LockNotAvailable:
                # Statements before this one are kept; all of them are IF NOT EXISTS, so a rerun resumes.
                print("A statement did not get its lock within {}: {}\nRun again later.".format(
                    args.lock_timeout, first_line))
                return 1
            print("{} ({:.3f} s)".format(first_line, time.monotonic() - started))

        try:
            complete = not plan(*_report(conn))
        except ValueError as exc:
            print("ERROR: {}".format(exc))
            return 1
        return 0 if complete else 1


if __name__ == "__main__":
    sys.exit(main())
