"""Add conversation_turns.conversation_key and its index to a PostgreSQL store (2026-10-23, R9-7).

The relay does not change the PostgreSQL schema itself. It writes the key as soon as
the column exists (it looks again at most once a minute, no restart needed) and
reads NULL for the rows stored before. SQLite stores are migrated by the relay.

Safe to run while the relay is up: ADD COLUMN without a default only changes the
catalogue, but it needs a brief ACCESS EXCLUSIVE lock, so it waits at most
--lock-timeout behind running queries and then gives up (run it again later); the
index is built with CREATE INDEX CONCURRENTLY, so writes continue meanwhile. Every
statement runs in autocommit, outside any transaction, as CONCURRENTLY requires.

Usage:
    python docs/migrate-conversation-key.py --pg "$LLM_RELAY_DB"            # report only
    python docs/migrate-conversation-key.py --pg "$LLM_RELAY_DB" --apply    # run the DDL

Idempotent. Nothing is dropped or rewritten: an index left INVALID by an interrupted
build is reported, not removed.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import List, Optional

try:
    import psycopg
except ImportError:
    print("ERROR: psycopg not installed. Run: pip install psycopg[binary]")
    sys.exit(1)

INDEX_NAME = "idx_conversation_turns_conv"
ADD_COLUMN = "ALTER TABLE conversation_turns ADD COLUMN IF NOT EXISTS conversation_key TEXT"
CREATE_INDEX = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS " + INDEX_NAME
    + " ON conversation_turns (session_id, conversation_key, turn_number)"
)


def column_exists(conn) -> bool:
    row = conn.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()"
        " AND table_name = 'conversation_turns' AND column_name = 'conversation_key'"
    ).fetchone()
    return row is not None


def index_state(conn) -> Optional[bool]:
    """None when the index is missing, else whether it is valid (an interrupted CONCURRENTLY build is not)."""
    row = conn.execute(
        "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid"
        " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relname = %s AND n.nspname = current_schema()",
        (INDEX_NAME,),
    ).fetchone()
    return None if row is None else bool(row[0])


def _describe(has_column: bool, index: Optional[bool]) -> str:
    state = {None: "missing", True: "valid", False: "INVALID"}[index]
    column = "present" if has_column else "missing"
    return "column conversation_key: {} / index {}: {}".format(column, INDEX_NAME, state)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Add conversation_turns.conversation_key (R9-7) to PostgreSQL.")
    parser.add_argument("--pg", default=os.getenv("LLM_RELAY_DB"), help="PostgreSQL DSN (default: $LLM_RELAY_DB)")
    parser.add_argument("--apply", action="store_true", help="run the DDL (default: report only)")
    parser.add_argument("--lock-timeout", default="5s", help="how long ALTER TABLE may wait for its lock (default 5s)")
    args = parser.parse_args(argv)
    if not args.pg or not args.pg.startswith(("postgresql://", "postgres://")):
        print("ERROR: --pg (or LLM_RELAY_DB) must be a postgresql:// DSN")
        return 2

    with psycopg.connect(args.pg, autocommit=True) as conn:
        has_column = column_exists(conn)
        index = index_state(conn)
        print(_describe(has_column, index))
        if index is False:
            print("The index exists but is INVALID (an interrupted build). Review and drop it, then run again.")
            return 1
        if has_column and index is True:
            print("Nothing to do.")
            return 0
        if not args.apply:
            print("Report only: run again with --apply to add what is missing.")
            return 0

        if not has_column:
            conn.execute("SELECT set_config('lock_timeout', %s, false)", (args.lock_timeout,))
            started = time.monotonic()
            try:
                conn.execute(ADD_COLUMN)
            except psycopg.errors.LockNotAvailable:
                print("ALTER TABLE did not get its lock within {}: a long query holds conversation_turns. "
                      "Nothing changed; run again later.".format(args.lock_timeout))
                return 1
            print("Added the column in {:.2f} s.".format(time.monotonic() - started))
        if index is None:
            # CONCURRENTLY waits for older transactions instead of blocking writers: no lock timeout.
            conn.execute("SELECT set_config('lock_timeout', '0', false)")
            started = time.monotonic()
            conn.execute(CREATE_INDEX)
            print("Built the index in {:.1f} s.".format(time.monotonic() - started))

        has_column, index = column_exists(conn), index_state(conn)
        print(_describe(has_column, index))
        return 0 if has_column and index is True else 1


if __name__ == "__main__":
    sys.exit(main())
