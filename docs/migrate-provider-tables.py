"""Create the provider_* tables for Antigravity CLI traffic in a PostgreSQL store (2026-11-20).

The Antigravity relay (``llm_relay.antigravity``) records requests, conversation
turns and quota snapshots in three dedicated tables -- provider_requests,
provider_turns, provider_quota -- and keeps system prompts, tool lists and model
snapshots once each in provider_blobs (optional: capture runs without it). The
relay does not change the PostgreSQL schema itself: it checks for the three
required tables and leaves capture off until they exist. SQLite stores get the
same tables from the relay (``store.ensure_schema``).

The report and the "complete" verdict cover every table and index of
``ALL_TABLES``. Columns added to a table after it was created are not this
script's job: run ``docs/migrate-provider-columns.py`` for those.

New tables only. Every statement is ``CREATE TABLE IF NOT EXISTS`` or
``CREATE [UNIQUE] INDEX IF NOT EXISTS`` on one of the provider tables (``PG_DDL`` in
``llm_relay.antigravity.store`` is the single source of truth). No existing table
is referenced, so the Claude Code proxy's tables (``requests``,
``conversation_turns``, ...) are neither altered nor locked, and it is safe to run
while the relay is up. Each statement runs in autocommit; an index on a provider
table that already holds rows waits at most --lock-timeout for its lock and then
gives up (run it again later).

Usage:
    python docs/migrate-provider-tables.py --pg "$LLM_RELAY_DB"            # report only
    python docs/migrate-provider-tables.py --pg "$LLM_RELAY_DB" --apply    # run the DDL

Idempotent. Nothing is dropped, altered or rewritten.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

try:
    import psycopg
except ImportError:
    print("ERROR: psycopg not installed. Run: pip install psycopg[binary]")
    sys.exit(1)

try:
    from llm_relay.antigravity.store import ALL_TABLES, INDEXES, PG_DDL
except ImportError:  # a checkout without the package installed
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from llm_relay.antigravity.store import ALL_TABLES, INDEXES, PG_DDL

INDEX_NAMES = tuple(index[0] for index in INDEXES)


def table_rows(conn) -> Dict[str, Optional[int]]:
    """Row count per provider table, None when the table is missing."""
    placeholders = ", ".join(["%s"] * len(ALL_TABLES))
    present = {
        row[0] for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
            " AND table_name IN ({})".format(placeholders), ALL_TABLES,
        ).fetchall()
    }
    counts: Dict[str, Optional[int]] = {}
    for table in ALL_TABLES:
        # The name comes from the fixed ALL_TABLES tuple, never from input.
        counts[table] = conn.execute("SELECT count(*) FROM " + table).fetchone()[0] if table in present else None
    return counts


def indexes_present(conn) -> Dict[str, bool]:
    """Whether each provider index exists on its provider table in the current schema."""
    found = {
        row[0] for row in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
            " AND tablename IN ({}) AND indexname IN ({})".format(
                ", ".join(["%s"] * len(ALL_TABLES)), ", ".join(["%s"] * len(INDEX_NAMES))),
            ALL_TABLES + INDEX_NAMES,
        ).fetchall()
    }
    return {name: name in found for name in INDEX_NAMES}


def _report(conn) -> bool:
    """Print which tables and indexes exist; return whether all of them do."""
    counts = table_rows(conn)
    indexes = indexes_present(conn)
    for table in ALL_TABLES:
        rows = counts[table]
        print("table {}: {}".format(table, "missing" if rows is None else "present, {} rows".format(rows)))
    for name in INDEX_NAMES:
        print("index {}: {}".format(name, "present" if indexes[name] else "missing"))
    return all(rows is not None for rows in counts.values()) and all(indexes.values())


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Create the provider_* tables (Antigravity CLI) in PostgreSQL.")
    parser.add_argument("--pg", required=True, help="PostgreSQL DSN (postgresql://...)")
    parser.add_argument("--apply", action="store_true", help="run the DDL (default: report only)")
    parser.add_argument("--lock-timeout", default="5s", help="how long one statement may wait for a lock (default 5s)")
    args = parser.parse_args(argv)
    if not args.pg.startswith(("postgresql://", "postgres://")):
        print("ERROR: --pg must be a postgresql:// or postgres:// DSN")
        return 2

    with psycopg.connect(args.pg, autocommit=True) as conn:
        complete = _report(conn)
        if complete:
            print("Nothing to do.")
            return 0
        if not args.apply:
            print("Report only: run again with --apply to create what is missing.")
            return 0

        conn.execute("SELECT set_config('lock_timeout', %s, false)", (args.lock_timeout,))
        for statement in PG_DDL:
            started = time.monotonic()
            try:
                conn.execute(statement)
            except psycopg.errors.LockNotAvailable:
                # Statements before this one are kept; all of them are IF NOT EXISTS, so a rerun resumes.
                print("A statement did not get its lock within {}: {}\nRun again later.".format(
                    args.lock_timeout, statement.splitlines()[0]))
                return 1
            print("{} ({:.2f} s)".format(statement.splitlines()[0].rstrip(" ("), time.monotonic() - started))

        return 0 if _report(conn) else 1


if __name__ == "__main__":
    sys.exit(main())
