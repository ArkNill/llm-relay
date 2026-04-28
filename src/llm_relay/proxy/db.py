"""SQLite storage for API request/response usage logs."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

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
    """CREATE TABLE IF NOT EXISTS session_terminals (
        session_id TEXT PRIMARY KEY,
        tty TEXT,
        cc_pid INTEGER,
        term_pid INTEGER,
        term_name TEXT,
        updated_ts REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS cache_diagnostics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        session_id TEXT,
        cc_version TEXT,
        fingerprint TEXT,
        system_block_count INTEGER DEFAULT 0,
        system_preview TEXT,
        msg0_block_count INTEGER DEFAULT 0,
        msg0_preview TEXT,
        drifted_blocks TEXT,
        tools_count INTEGER DEFAULT 0,
        tools_reordered INTEGER DEFAULT 0,
        ttl_injected INTEGER DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_cache_diag_ts ON cache_diagnostics(ts)",
    "CREATE INDEX IF NOT EXISTS idx_cache_diag_session ON cache_diagnostics(session_id)",
    # ── Session history tables (LLM_RELAY_HISTORY=1) ──
    """CREATE TABLE IF NOT EXISTS conversation_turns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        session_id TEXT NOT NULL,
        request_id INTEGER,
        turn_number INTEGER NOT NULL,
        storage_mode TEXT NOT NULL DEFAULT 'delta',
        request_messages TEXT,
        response_message TEXT,
        thinking_blocks TEXT,
        model TEXT,
        temperature REAL,
        max_tokens INTEGER,
        total_message_count INTEGER,
        previous_message_count INTEGER,
        request_size_bytes INTEGER DEFAULT 0,
        response_size_bytes INTEGER DEFAULT 0,
        provider TEXT DEFAULT 'anthropic'
    )""",
    "CREATE INDEX IF NOT EXISTS idx_conv_turns_session ON conversation_turns(session_id, turn_number)",
    """CREATE TABLE IF NOT EXISTS compaction_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        session_id TEXT NOT NULL,
        turn_number INTEGER NOT NULL,
        previous_count INTEGER,
        current_count INTEGER,
        dropped_count INTEGER,
        previous_tokens INTEGER,
        current_tokens INTEGER,
        token_drop_pct REAL,
        dropped_roles TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_compaction_session ON compaction_events(session_id)",
    # TTL tier tracking (ephemeral cache token fields from SSE message_start)
    "ALTER TABLE requests ADD COLUMN ephemeral_1h_tokens INTEGER DEFAULT 0",
    "ALTER TABLE requests ADD COLUMN ephemeral_5m_tokens INTEGER DEFAULT 0",
]


def get_conn(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    # WAL mode -- allows concurrent reads during writes and replaces the
    # per-commit fsync chain with a single wal checkpoint, cutting hot-path
    # latency for the proxy logger. synchronous=NORMAL is the standard WAL
    # companion (loss-window ≤ 1 tx on crash, acceptable for a usage log).
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError:
        # Some filesystems (e.g. network mounts) reject WAL -- fall back quietly.
        pass
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
    ephemeral_1h_tokens: int = 0,
    ephemeral_5m_tokens: int = 0,
) -> None:
    total_cached = cache_creation + cache_read
    read_ratio = (cache_read / total_cached * 100) if total_cached > 0 else 0.0

    conn.execute(
        """INSERT INTO requests
           (ts, session_id, model, input_tokens, output_tokens,
            cache_creation, cache_read, read_ratio, status_code,
            latency_ms, endpoint, is_stream, raw_usage, request_body_bytes,
            ratelimit_headers, ephemeral_1h_tokens, ephemeral_5m_tokens)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            ephemeral_1h_tokens,
            ephemeral_5m_tokens,
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


def log_cache_diagnostic(
    conn: sqlite3.Connection,
    *,
    session_id: Optional[str] = None,
    cc_version: Optional[str] = None,
    fingerprint: Optional[str] = None,
    system_block_count: int = 0,
    system_preview: Optional[str] = None,
    msg0_block_count: int = 0,
    msg0_preview: Optional[str] = None,
    drifted_blocks: Optional[str] = None,
    tools_count: int = 0,
    tools_reordered: int = 0,
    ttl_injected: int = 0,
) -> None:
    conn.execute(
        """INSERT INTO cache_diagnostics
           (ts, session_id, cc_version, fingerprint,
            system_block_count, system_preview,
            msg0_block_count, msg0_preview,
            drifted_blocks, tools_count, tools_reordered, ttl_injected)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            time.time(),
            session_id,
            cc_version,
            fingerprint,
            system_block_count,
            system_preview,
            msg0_block_count,
            msg0_preview,
            drifted_blocks,
            tools_count,
            tools_reordered,
            ttl_injected,
        ),
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


def get_turn_count(
    conn: sqlite3.Connection, session_id: str
) -> dict[str, Any]:
    """Return turn count + 4 token metrics for a specific session (/v1/messages only).

    Returns dict with keys:
      turns, first_ts, last_ts,
      current_ctx  -- latest request's cache_read + cache_creation + input_tokens
      peak_ctx     -- MAX over session of (cache_read + cache_creation + input_tokens)
      recent_peak  -- MAX over last 5 requests of the same
      cumul_unique -- SUM over session of (input_tokens + cache_creation + output_tokens)
    """
    row = conn.execute(
        """WITH ranked AS (
             SELECT ts,
                    cache_read + cache_creation + input_tokens AS ctx,
                    input_tokens + cache_creation + output_tokens AS unique_tokens,
                    ROW_NUMBER() OVER (ORDER BY ts DESC) AS rn_desc
             FROM requests
             WHERE session_id = ?
               AND endpoint = '/v1/messages'
           )
           SELECT COUNT(*) AS turns,
                  MIN(ts) AS first_ts,
                  MAX(ts) AS last_ts,
                  COALESCE(MAX(ctx), 0) AS peak_ctx,
                  COALESCE(SUM(unique_tokens), 0) AS cumul_unique,
                  COALESCE(MAX(CASE WHEN rn_desc = 1 THEN ctx END), 0) AS current_ctx,
                  COALESCE(MAX(CASE WHEN rn_desc <= 5 THEN ctx END), 0) AS recent_peak
           FROM ranked""",
        (session_id,),
    ).fetchone()
    if row is None or row["turns"] == 0:
        return {
            "turns": 0,
            "first_ts": None,
            "last_ts": None,
            "peak_ctx": 0,
            "cumul_unique": 0,
            "current_ctx": 0,
            "recent_peak": 0,
        }
    return dict(row)


def upsert_session_terminal(
    conn: sqlite3.Connection,
    session_id: str,
    tty: Optional[str] = None,
    cc_pid: Optional[int] = None,
    term_pid: Optional[int] = None,
    term_name: Optional[str] = None,
) -> None:
    """Insert or update terminal info for a session.

    When a new session registers the same cc_pid as an older session
    (terminal reuse), the old session's terminal record is cleared so it
    no longer appears alive on the display page.
    """
    now = time.time()
    # Clear stale sessions that had the same cc_pid (terminal reuse)
    if cc_pid:
        conn.execute(
            """UPDATE session_terminals
               SET cc_pid = NULL, tty = NULL
               WHERE cc_pid = ? AND session_id != ?""",
            (cc_pid, session_id),
        )
    # term_name preservation rule: if the incoming value is a generic shell or
    # process name AND the existing row already has a non-generic value, keep
    # the existing one. This lets users (or per-project SessionStart hooks) set
    # human-readable agent labels without auto-registration clobbering them on
    # the next sub-agent spawn. Explicit overrides still work — just POST a
    # non-generic name.
    GENERIC_TERM_NAMES = (
        "claude.exe",
        "bash",
        "sh",
        "zsh",
        "fish",
        "dash",
        "ksh",
        "tmux",
        "screen",
    )
    placeholders = ",".join(["?"] * len(GENERIC_TERM_NAMES))
    conn.execute(
        f"""INSERT INTO session_terminals (session_id, tty, cc_pid, term_pid, term_name, updated_ts)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET
               tty = excluded.tty,
               cc_pid = excluded.cc_pid,
               term_pid = excluded.term_pid,
               term_name = CASE
                   WHEN excluded.term_name IN ({placeholders})
                        AND term_name IS NOT NULL
                        AND term_name NOT IN ({placeholders})
                   THEN term_name
                   ELSE excluded.term_name
               END,
               updated_ts = excluded.updated_ts""",
        (session_id, tty, cc_pid, term_pid, term_name, now, *GENERIC_TERM_NAMES, *GENERIC_TERM_NAMES),
    )
    conn.commit()


def get_session_terminal(
    conn: sqlite3.Connection, session_id: str
) -> Optional[dict[str, Any]]:
    """Return terminal info for a session, or None if not recorded."""
    row = conn.execute(
        "SELECT session_id, tty, cc_pid, term_pid, term_name, updated_ts FROM session_terminals WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return dict(row) if row else None


def get_all_session_terminals(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, Any]]:
    """Return all session terminal info as a dict keyed by session_id."""
    rows = conn.execute(
        "SELECT session_id, tty, cc_pid, term_pid, term_name, updated_ts FROM session_terminals"
    ).fetchall()
    return {r["session_id"]: dict(r) for r in rows}


def get_all_turn_counts(
    conn: sqlite3.Connection, window_hours: float = 4
) -> list[dict[str, Any]]:
    """Return turn counts + 4 token metrics for all recent sessions.

    Each row contains the same keys as get_turn_count() plus session_id.
    Uses a single window-function CTE to compute per-session aggregates and
    rank-based "latest / recent-5" snapshots in one scan.
    """
    cutoff = time.time() - window_hours * 3600
    rows = conn.execute(
        """WITH ranked AS (
             SELECT session_id, ts,
                    cache_read + cache_creation + input_tokens AS ctx,
                    input_tokens + cache_creation + output_tokens AS unique_tokens,
                    ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY ts DESC) AS rn_desc
             FROM requests
             WHERE ts > ?
               AND endpoint = '/v1/messages'
               AND session_id IS NOT NULL
           )
           SELECT session_id,
                  COUNT(*) AS turns,
                  MIN(ts) AS first_ts,
                  MAX(ts) AS last_ts,
                  COALESCE(MAX(ctx), 0) AS peak_ctx,
                  COALESCE(SUM(unique_tokens), 0) AS cumul_unique,
                  COALESCE(MAX(CASE WHEN rn_desc = 1 THEN ctx END), 0) AS current_ctx,
                  COALESCE(MAX(CASE WHEN rn_desc <= 5 THEN ctx END), 0) AS recent_peak
           FROM ranked
           GROUP BY session_id
           ORDER BY turns DESC""",
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


def get_latest_quota(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
    """Return the latest ratelimit quota data from the most recent request with headers.

    Extracts anthropic-ratelimit-unified-5h/7d-utilization and overage status
    from the stored ratelimit_headers JSON.
    """
    row = conn.execute(
        """SELECT ratelimit_headers, ts
           FROM requests
           WHERE ratelimit_headers IS NOT NULL
           ORDER BY ts DESC
           LIMIT 1"""
    ).fetchone()
    if not row or not row["ratelimit_headers"]:
        return None
    try:
        headers = json.loads(row["ratelimit_headers"])
    except (json.JSONDecodeError, TypeError):
        return None

    # Normalize header keys to lowercase for consistent lookup
    lower = {k.lower(): v for k, v in headers.items()}
    return {
        "ts": row["ts"],
        "q5h_utilization": lower.get("anthropic-ratelimit-unified-5h-utilization"),
        "q7d_utilization": lower.get("anthropic-ratelimit-unified-7d-utilization"),
        "unified_status": lower.get("anthropic-ratelimit-unified-status"),
        "overage_status": lower.get("anthropic-ratelimit-unified-overage-status"),
    }


def get_error_stats(
    conn: sqlite3.Connection,
    session_id: Optional[str] = None,
    window_hours: float = 8,
) -> dict[str, Any]:
    """Return error rate statistics from status_code data.

    Returns total requests, success (2xx), client errors (4xx), server errors (5xx),
    and the error rate as a percentage.
    """
    cutoff = time.time() - window_hours * 3600
    params: list[Any] = [cutoff]
    where = "WHERE ts > ?"
    if session_id:
        where += " AND session_id = ?"
        params.append(session_id)

    row = conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN status_code >= 200 AND status_code < 300 THEN 1 ELSE 0 END) AS success_2xx,
                  SUM(CASE WHEN status_code >= 400 AND status_code < 500 THEN 1 ELSE 0 END) AS client_4xx,
                  SUM(CASE WHEN status_code >= 500 THEN 1 ELSE 0 END) AS server_5xx,
                  SUM(CASE WHEN status_code = 429 THEN 1 ELSE 0 END) AS rate_limited
           FROM requests
           """
        + where,
        params,
    ).fetchone()
    if not row or row["total"] == 0:
        return {
            "total": 0,
            "success_2xx": 0,
            "client_4xx": 0,
            "server_5xx": 0,
            "rate_limited": 0,
            "error_rate_pct": 0.0,
        }
    total = row["total"]
    errors = (row["client_4xx"] or 0) + (row["server_5xx"] or 0)
    return {
        "total": total,
        "success_2xx": row["success_2xx"] or 0,
        "client_4xx": row["client_4xx"] or 0,
        "server_5xx": row["server_5xx"] or 0,
        "rate_limited": row["rate_limited"] or 0,
        "error_rate_pct": round(errors / total * 100, 2) if total else 0.0,
    }


def get_session_cache_stats(
    conn: sqlite3.Connection,
    session_id: Optional[str] = None,
    window_hours: float = 8,
) -> dict[str, Any]:
    """Return cache hit rate statistics.

    cache_hit_rate = cache_read / (cache_read + cache_creation) when there is cached data.
    Also returns fresh_input (tokens not from cache).
    """
    cutoff = time.time() - window_hours * 3600
    params: list[Any] = [cutoff]
    where = "WHERE ts > ? AND endpoint = '/v1/messages'"
    if session_id:
        where += " AND session_id = ?"
        params.append(session_id)

    row = conn.execute(
        """SELECT SUM(cache_read) AS total_cache_read,
                  SUM(cache_creation) AS total_cache_creation,
                  SUM(input_tokens) AS total_input_tokens,
                  COUNT(*) AS request_count
           FROM requests
           """
        + where,
        params,
    ).fetchone()
    if not row or row["request_count"] == 0:
        return {
            "total_cache_read": 0,
            "total_cache_creation": 0,
            "total_input_tokens": 0,
            "cache_hit_rate": 0.0,
            "request_count": 0,
        }
    cr = row["total_cache_read"] or 0
    cc = row["total_cache_creation"] or 0
    total_cached = cr + cc
    return {
        "total_cache_read": cr,
        "total_cache_creation": cc,
        "total_input_tokens": row["total_input_tokens"] or 0,
        "cache_hit_rate": round(cr / total_cached * 100, 2) if total_cached > 0 else 0.0,
        "request_count": row["request_count"],
    }


def get_ttl_tier(
    conn: sqlite3.Connection,
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """Detect cache TTL tier (1h vs 5m) from ephemeral token columns.

    Returns the latest non-zero ephemeral token data and inferred tier.
    Tier logic: if ephemeral_1h_tokens > 0 → '1h', if ephemeral_5m_tokens > 0 → '5m',
    otherwise 'unknown'.
    """
    params: list[Any] = []
    where = "WHERE (ephemeral_1h_tokens > 0 OR ephemeral_5m_tokens > 0)"
    if session_id:
        where += " AND session_id = ?"
        params.append(session_id)

    row = conn.execute(
        """SELECT SUM(ephemeral_1h_tokens) AS total_1h,
                  SUM(ephemeral_5m_tokens) AS total_5m,
                  COUNT(*) AS request_count
           FROM requests
           """
        + where,
        params,
    ).fetchone()

    total_1h = (row["total_1h"] or 0) if row else 0
    total_5m = (row["total_5m"] or 0) if row else 0
    req_count = (row["request_count"] or 0) if row else 0

    if total_1h > 0 and total_5m == 0:
        tier = "1h"
    elif total_5m > 0 and total_1h == 0:
        tier = "5m"
    elif total_1h > 0 and total_5m > 0:
        tier = "mixed"
    else:
        tier = "unknown"

    return {
        "tier": tier,
        "ephemeral_1h_tokens": total_1h,
        "ephemeral_5m_tokens": total_5m,
        "request_count": req_count,
    }


# ── Session history storage & query functions ──


def log_conversation_turn(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    turn_number: int,
    storage_mode: str = "delta",
    request_messages: Optional[str] = None,
    response_message: Optional[str] = None,
    thinking_blocks: Optional[str] = None,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    total_message_count: int = 0,
    previous_message_count: int = 0,
    request_size_bytes: int = 0,
    response_size_bytes: int = 0,
    provider: str = "anthropic",
    request_id: Optional[int] = None,
) -> int:
    """Insert a conversation turn record. Returns the row id."""
    cursor = conn.execute(
        """INSERT INTO conversation_turns
           (ts, session_id, request_id, turn_number, storage_mode,
            request_messages, response_message, thinking_blocks,
            model, temperature, max_tokens,
            total_message_count, previous_message_count,
            request_size_bytes, response_size_bytes, provider)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            time.time(),
            session_id,
            request_id,
            turn_number,
            storage_mode,
            request_messages,
            response_message,
            thinking_blocks,
            model,
            temperature,
            max_tokens,
            total_message_count,
            previous_message_count,
            request_size_bytes,
            response_size_bytes,
            provider,
        ),
    )
    conn.commit()
    return cursor.lastrowid or 0


def log_compaction_event(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    turn_number: int,
    previous_count: int = 0,
    current_count: int = 0,
    dropped_count: int = 0,
    previous_tokens: int = 0,
    current_tokens: int = 0,
    token_drop_pct: float = 0.0,
    dropped_roles: Optional[str] = None,
) -> None:
    """Insert a compaction detection event."""
    conn.execute(
        """INSERT INTO compaction_events
           (ts, session_id, turn_number,
            previous_count, current_count, dropped_count,
            previous_tokens, current_tokens, token_drop_pct,
            dropped_roles)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            time.time(),
            session_id,
            turn_number,
            previous_count,
            current_count,
            dropped_count,
            previous_tokens,
            current_tokens,
            token_drop_pct,
            dropped_roles,
        ),
    )
    conn.commit()


def get_session_history(
    conn: sqlite3.Connection,
    session_id: str,
    turn_start: int = 0,
    turn_end: int = -1,
    include_thinking: bool = False,
) -> list[dict[str, Any]]:
    """Return conversation turns for a session, optionally filtering by turn range.

    When include_thinking is False, the thinking_blocks field is excluded.
    Turn range is inclusive on both ends. turn_end=-1 means no upper limit.
    """
    params: list[Any] = [session_id, turn_start]
    sql = """SELECT id, ts, session_id, request_id, turn_number, storage_mode,
                    request_messages, response_message, thinking_blocks,
                    model, temperature, max_tokens,
                    total_message_count, previous_message_count,
                    request_size_bytes, response_size_bytes, provider
             FROM conversation_turns
             WHERE session_id = ? AND turn_number >= ?"""
    if turn_end >= 0:
        sql += " AND turn_number <= ?"
        params.append(turn_end)
    sql += " ORDER BY turn_number ASC"

    rows = conn.execute(sql, params).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        if not include_thinking:
            d.pop("thinking_blocks", None)
        result.append(d)
    return result


def get_session_compactions(
    conn: sqlite3.Connection,
    session_id: str,
) -> list[dict[str, Any]]:
    """Return compaction events for a session."""
    rows = conn.execute(
        "SELECT * FROM compaction_events WHERE session_id = ? ORDER BY turn_number ASC",
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_history_sessions(
    conn: sqlite3.Connection,
    window_hours: float = 24,
) -> list[dict[str, Any]]:
    """Return sessions that have conversation history, with summary stats."""
    cutoff = time.time() - window_hours * 3600
    rows = conn.execute(
        """SELECT session_id,
                  COUNT(*) as total_turns,
                  MIN(ts) as first_ts,
                  MAX(ts) as last_ts,
                  SUM(request_size_bytes) as total_request_bytes,
                  SUM(response_size_bytes) as total_response_bytes,
                  provider
           FROM conversation_turns
           WHERE ts > ?
           GROUP BY session_id
           ORDER BY last_ts DESC""",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]
