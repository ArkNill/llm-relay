"""SQLite storage for API request/response usage logs."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import os

DEFAULT_DB = Path(os.getenv("LLM_RELAY_DB", str(Path.home() / ".llm-relay" / "usage.db")))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    session_id TEXT,
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_creation INTEGER DEFAULT 0,
    cache_read INTEGER DEFAULT 0,
    read_ratio REAL DEFAULT 0.0,
    status_code INTEGER,
    latency_ms REAL,
    endpoint TEXT,
    is_stream INTEGER DEFAULT 0,
    raw_usage TEXT,
    request_body_bytes INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS microcompact_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    session_id TEXT,
    request_id INTEGER REFERENCES requests(id),
    cleared_count INTEGER DEFAULT 0,
    total_tool_results INTEGER DEFAULT 0,
    cleared_indices TEXT,
    message_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS budget_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    session_id TEXT,
    msg_index INTEGER,
    tool_name TEXT,
    content_chars INTEGER DEFAULT 0,
    truncated INTEGER DEFAULT 0,
    marker TEXT
);

CREATE INDEX IF NOT EXISTS idx_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_session ON requests(session_id);
CREATE INDEX IF NOT EXISTS idx_mc_ts ON microcompact_events(ts);
CREATE INDEX IF NOT EXISTS idx_budget_ts ON budget_events(ts);
"""


_MIGRATIONS = [
    "ALTER TABLE requests ADD COLUMN request_body_bytes INTEGER DEFAULT 0",
    "ALTER TABLE requests ADD COLUMN ratelimit_headers TEXT",
    "ALTER TABLE requests ADD COLUMN estimated_cost_usd REAL DEFAULT 0.0",
    "ALTER TABLE requests ADD COLUMN message_count INTEGER DEFAULT 0",
    """CREATE TABLE IF NOT EXISTS prune_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        session_id TEXT,
        tier TEXT,
        messages_before INTEGER DEFAULT 0,
        messages_after INTEGER DEFAULT 0,
        chars_before INTEGER DEFAULT 0,
        chars_after INTEGER DEFAULT 0,
        chars_removed INTEGER DEFAULT 0,
        savings_pct REAL DEFAULT 0.0,
        strategies TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS health_checks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        check_name TEXT,
        status TEXT,
        detail TEXT,
        session_id TEXT
    )""",
]


def get_conn(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.executescript(_SCHEMA)
    # Run migrations for existing databases
    for sql in _MIGRATIONS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.row_factory = sqlite3.Row
    return conn


def log_request(
    conn: sqlite3.Connection,
    *,
    session_id: str | None = None,
    model: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation: int = 0,
    cache_read: int = 0,
    status_code: int = 0,
    latency_ms: float = 0.0,
    endpoint: str = "",
    is_stream: bool = False,
    raw_usage: dict[str, Any] | None = None,
    request_body_bytes: int = 0,
    ratelimit_headers: dict[str, str] | None = None,
) -> None:
    total_cached = cache_creation + cache_read
    read_ratio = (cache_read / total_cached * 100) if total_cached > 0 else 0.0

    conn.execute(
        """INSERT INTO requests
           (ts, session_id, model, input_tokens, output_tokens,
            cache_creation, cache_read, read_ratio, status_code,
            latency_ms, endpoint, is_stream, raw_usage, request_body_bytes,
            ratelimit_headers)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            time.time(),
            session_id,
            model,
            input_tokens,
            output_tokens,
            cache_creation,
            cache_read,
            read_ratio,
            status_code,
            latency_ms,
            endpoint,
            int(is_stream),
            json.dumps(raw_usage) if raw_usage else None,
            request_body_bytes,
            json.dumps(ratelimit_headers) if ratelimit_headers else None,
        ),
    )
    conn.commit()


def log_microcompact(
    conn: sqlite3.Connection,
    *,
    session_id: str | None = None,
    request_id: int | None = None,
    cleared_count: int = 0,
    total_tool_results: int = 0,
    cleared_indices: list[int] | None = None,
    message_count: int = 0,
) -> None:
    conn.execute(
        """INSERT INTO microcompact_events
           (ts, session_id, request_id, cleared_count, total_tool_results,
            cleared_indices, message_count)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            time.time(),
            session_id,
            request_id,
            cleared_count,
            total_tool_results,
            json.dumps(cleared_indices) if cleared_indices else None,
            message_count,
        ),
    )
    conn.commit()


def log_budget_event(
    conn: sqlite3.Connection,
    *,
    session_id: str | None = None,
    msg_index: int = 0,
    tool_name: str = "",
    content_chars: int = 0,
    truncated: bool = False,
    marker: str = "",
) -> None:
    conn.execute(
        """INSERT INTO budget_events
           (ts, session_id, msg_index, tool_name, content_chars, truncated, marker)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (time.time(), session_id, msg_index, tool_name, content_chars, int(truncated), marker),
    )
    conn.commit()


def get_budget_events(
    conn: sqlite3.Connection, limit: int = 50
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM budget_events ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_microcompact_events(
    conn: sqlite3.Connection, limit: int = 50
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM microcompact_events ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_session_summary(
    conn: sqlite3.Connection, window_hours: float = 8
) -> list[dict[str, Any]]:
    cutoff = time.time() - window_hours * 3600
    rows = conn.execute(
        """SELECT session_id,
                  COUNT(*) as turns,
                  SUM(input_tokens) as total_input,
                  SUM(output_tokens) as total_output,
                  SUM(cache_creation) as total_creation,
                  SUM(cache_read) as total_read,
                  AVG(read_ratio) as avg_read_ratio,
                  AVG(latency_ms) as avg_latency,
                  MIN(ts) as first_ts,
                  MAX(ts) as last_ts
           FROM requests
           WHERE ts > ?
           GROUP BY session_id
           ORDER BY last_ts DESC""",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_recent(
    conn: sqlite3.Connection, limit: int = 20
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM requests ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]
