"""Migrate llm-relay data from SQLite to PostgreSQL.

Status: DRAFT — do not execute without review.

Usage:
    python migrate-sqlite-to-pg.py \
        --sqlite ~/.llm-relay/usage.db \
        --pg postgresql://relay:relay@localhost:5432/llm_relay

What this does:
    1. Reads all tables from SQLite
    2. Converts types (REAL→TIMESTAMPTZ, TEXT→JSONB, INTEGER→BOOLEAN)
    3. Deduplicates by (session_id, ts, endpoint) for requests
    4. Inserts into PostgreSQL in batches
    5. Verifies row counts match
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except ImportError:
    print("ERROR: psycopg not installed. Run: pip install psycopg[binary]")
    sys.exit(1)


# ── Type conversion helpers ──


def ts_to_timestamptz(ts_real: Optional[float]) -> Optional[datetime]:
    """SQLite REAL epoch → Python datetime (UTC)."""
    if ts_real is None:
        return None
    return datetime.fromtimestamp(ts_real, tz=timezone.utc)


def _strip_null_bytes(obj: Any) -> Any:
    """Recursively strip \\u0000 from strings in JSON-like objects."""
    if isinstance(obj, str):
        return obj.replace("\x00", "")
    if isinstance(obj, dict):
        return {k: _strip_null_bytes(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_null_bytes(v) for v in obj]
    return obj


def text_to_jsonb(text: Optional[str]) -> Any:
    """SQLite TEXT → Jsonb wrapper for psycopg (or None)."""
    if text is None or text == "":
        return None
    # Strip null bytes — PostgreSQL rejects \u0000 in text/JSONB
    text = text.replace("\x00", "")
    try:
        parsed = json.loads(text)
        return Jsonb(_strip_null_bytes(parsed))
    except (json.JSONDecodeError, TypeError):
        return Jsonb(text)


def int_to_bool(val: Optional[int]) -> bool:
    """SQLite INTEGER (0/1) → Python bool."""
    return bool(val) if val is not None else False


# ── Table migration configs ──

# Each entry: (sqlite_table, pg_table, column_transforms)
# column_transforms: dict of {pg_column: (sqlite_column, converter)}
# If converter is None, copy as-is.

TABLES = [
    {
        "name": "requests",
        "batch_size": 5000,
        "dedup_key": ("session_id", "ts", "endpoint"),
        "transforms": {
            "ts": ("ts", ts_to_timestamptz),
            "is_stream": ("is_stream", int_to_bool),
            "raw_usage": ("raw_usage", text_to_jsonb),
            "ratelimit_headers": ("ratelimit_headers", text_to_jsonb),
        },
    },
    {
        "name": "microcompact_events",
        "batch_size": 5000,
        "transforms": {
            "ts": ("ts", ts_to_timestamptz),
            "cleared_indices": ("cleared_indices", text_to_jsonb),
        },
    },
    {
        "name": "budget_events",
        "batch_size": 10000,
        "transforms": {
            "ts": ("ts", ts_to_timestamptz),
            "truncated": ("truncated", int_to_bool),
        },
    },
    {
        "name": "prune_events",
        "batch_size": 1000,
        "transforms": {
            "ts": ("ts", ts_to_timestamptz),
        },
    },
    {
        "name": "health_checks",
        "batch_size": 1000,
        "transforms": {
            "ts": ("ts", ts_to_timestamptz),
        },
    },
    {
        "name": "intercept_events",
        "batch_size": 1000,
        "transforms": {
            "ts": ("ts", ts_to_timestamptz),
        },
    },
    {
        "name": "session_terminals",
        "batch_size": 500,
        "transforms": {
            "updated_ts": ("updated_ts", ts_to_timestamptz),
        },
    },
    {
        "name": "cache_diagnostics",
        "batch_size": 5000,
        "transforms": {
            "ts": ("ts", ts_to_timestamptz),
            "tools_reordered": ("tools_reordered", int_to_bool),
        },
    },
    {
        "name": "conversation_turns",
        "batch_size": 1000,
        "transforms": {
            "ts": ("ts", ts_to_timestamptz),
            "request_messages": ("request_messages", text_to_jsonb),
            "response_message": ("response_message", text_to_jsonb),
            "thinking_blocks": ("thinking_blocks", text_to_jsonb),
        },
    },
    {
        "name": "compaction_events",
        "batch_size": 2000,
        "transforms": {
            "ts": ("ts", ts_to_timestamptz),
        },
    },
    {
        "name": "delegations",
        "batch_size": 500,
        "transforms": {
            "ts": ("ts", ts_to_timestamptz),
            "success": ("success", int_to_bool),
        },
    },
]


# ── Migration logic ──


def get_sqlite_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    """Get column names for a SQLite table."""
    cursor = conn.execute("PRAGMA table_info({})".format(table))
    return [row[1] for row in cursor.fetchall()]


def migrate_table(
    sqlite_conn: sqlite3.Connection,
    pg_conn: psycopg.Connection,
    config: Dict[str, Any],
) -> Tuple[int, int]:
    """Migrate a single table. Returns (sqlite_count, pg_inserted)."""
    table = config["name"]
    batch_size = config.get("batch_size", 1000)
    transforms = config.get("transforms", {})
    dedup_key = config.get("dedup_key")

    # Get SQLite columns (skip 'id' — PostgreSQL uses SERIAL)
    sqlite_cols = get_sqlite_columns(sqlite_conn, table)
    data_cols = [c for c in sqlite_cols if c != "id"]

    # Read all rows from SQLite
    sqlite_count = sqlite_conn.execute(
        "SELECT COUNT(*) FROM {}".format(table)
    ).fetchone()[0]

    if sqlite_count == 0:
        print("  {} — empty, skipping".format(table))
        return 0, 0

    rows = sqlite_conn.execute(
        "SELECT {} FROM {} ORDER BY rowid".format(", ".join(data_cols), table)
    ).fetchall()

    # Deduplicate if needed
    if dedup_key:
        seen = set()
        unique_rows = []
        key_indices = [data_cols.index(k) for k in dedup_key]
        for row in rows:
            key = tuple(row[i] for i in key_indices)
            if key not in seen:
                seen.add(key)
                unique_rows.append(row)
        deduped = len(rows) - len(unique_rows)
        if deduped > 0:
            print("  {} — deduplicated: {} → {} (-{})".format(
                table, len(rows), len(unique_rows), deduped
            ))
        rows = unique_rows

    # Map SQLite columns to PG columns (same names, apply transforms)
    pg_cols = data_cols[:]

    # Build INSERT statement
    placeholders = ", ".join(["%s"] * len(pg_cols))
    insert_sql = "INSERT INTO {} ({}) VALUES ({})".format(
        table, ", ".join(pg_cols), placeholders
    )

    # Transform and insert in batches
    inserted = 0
    batch = []

    for row in rows:
        transformed = []
        for i, col in enumerate(data_cols):
            val = row[i]
            if col in transforms:
                _, converter = transforms[col]
                val = converter(val)
            transformed.append(val)
        batch.append(tuple(transformed))

        if len(batch) >= batch_size:
            with pg_conn.cursor() as cur:
                cur.executemany(insert_sql, batch)
            pg_conn.commit()
            inserted += len(batch)
            batch = []

    # Flush remaining
    if batch:
        with pg_conn.cursor() as cur:
            cur.executemany(insert_sql, batch)
        pg_conn.commit()
        inserted += len(batch)

    return sqlite_count, inserted


def verify_counts(
    sqlite_conn: sqlite3.Connection,
    pg_conn: psycopg.Connection,
) -> List[str]:
    """Compare row counts between SQLite and PG. Returns list of mismatches."""
    mismatches = []
    for config in TABLES:
        table = config["name"]
        s_count = sqlite_conn.execute(
            "SELECT COUNT(*) FROM {}".format(table)
        ).fetchone()[0]

        with pg_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM {}".format(table))
            p_count = cur.fetchone()[0]

        status = "OK" if p_count >= s_count or config.get("dedup_key") else "MISMATCH"
        if status == "MISMATCH":
            mismatches.append("{}: sqlite={} pg={}".format(table, s_count, p_count))
        print("  {} {:>25s}: sqlite={:>8,}  pg={:>8,}  {}".format(
            status, table, s_count, p_count,
            "(dedup applied)" if config.get("dedup_key") and p_count < s_count else ""
        ))

    return mismatches


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate llm-relay SQLite → PostgreSQL")
    parser.add_argument("--sqlite", required=True, help="Path to SQLite DB file")
    parser.add_argument("--pg", required=True, help="PostgreSQL connection URL")
    parser.add_argument("--dry-run", action="store_true", help="Show plan without executing")
    args = parser.parse_args()

    # Connect SQLite
    sqlite_conn = sqlite3.connect(args.sqlite)
    print("SQLite: {}".format(args.sqlite))
    print("  Size: {:.1f} MB".format(
        __import__("os").path.getsize(args.sqlite) / 1024 / 1024
    ))

    if args.dry_run:
        print("\n=== DRY RUN — showing plan only ===\n")
        for config in TABLES:
            table = config["name"]
            count = sqlite_conn.execute(
                "SELECT COUNT(*) FROM {}".format(table)
            ).fetchone()[0]
            print("  {} — {:,} rows (batch={}){}".format(
                table, count, config.get("batch_size", 1000),
                " [dedup by {}]".format(config.get("dedup_key")) if config.get("dedup_key") else ""
            ))
        sqlite_conn.close()
        print("\nDone (no changes made).")
        return

    # Connect PostgreSQL
    pg_conn = psycopg.connect(args.pg)
    print("PostgreSQL: connected\n")

    # Migrate each table
    print("=== Migrating ===\n")
    start = time.time()
    totals = {"sqlite": 0, "pg": 0}

    for config in TABLES:
        s_count, p_count = migrate_table(sqlite_conn, pg_conn, config)
        totals["sqlite"] += s_count
        totals["pg"] += p_count
        if s_count > 0:
            print("  {} — sqlite: {:,} → pg: {:,}".format(
                config["name"], s_count, p_count
            ))

    elapsed = time.time() - start
    print("\nMigrated {:,} rows in {:.1f}s\n".format(totals["pg"], elapsed))

    # Verify
    print("=== Verification ===\n")
    mismatches = verify_counts(sqlite_conn, pg_conn)

    if mismatches:
        print("\nWARNING: {} mismatches found:".format(len(mismatches)))
        for m in mismatches:
            print("  - {}".format(m))
    else:
        print("\nAll counts verified.")

    # Reset sequences
    print("\n=== Resetting sequences ===\n")
    for config in TABLES:
        table = config["name"]
        if table == "session_terminals":
            continue  # no serial column
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT setval(pg_get_serial_sequence('{t}', 'id'), "
                "COALESCE(MAX(id), 0) + 1, false) FROM {t}".format(t=table)
            )
        pg_conn.commit()
        print("  {} — sequence reset".format(table))

    sqlite_conn.close()
    pg_conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
