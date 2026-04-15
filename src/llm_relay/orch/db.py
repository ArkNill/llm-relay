"""Orchestration database -- extends the existing proxy SQLite DB with delegation tracking."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_ORCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS delegations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    cli_id TEXT NOT NULL,
    auth_method TEXT,
    prompt_hash TEXT,
    prompt_preview TEXT,
    model TEXT,
    working_dir TEXT,
    success INTEGER DEFAULT 0,
    exit_code INTEGER DEFAULT 0,
    duration_ms REAL DEFAULT 0.0,
    output_chars INTEGER DEFAULT 0,
    error TEXT,
    strategy TEXT
);

CREATE INDEX IF NOT EXISTS idx_deleg_ts ON delegations(ts);
CREATE INDEX IF NOT EXISTS idx_deleg_cli ON delegations(cli_id);
"""

DEFAULT_DB = Path(os.getenv("LLM_RELAY_DB", str(Path.home() / ".llm-relay" / "usage.db")))


def get_orch_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Get connection with orchestration tables initialized.

    Reuses proxy's DB path by default. Adds orch tables via migration.
    """
    path = db_path or DEFAULT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_ORCH_SCHEMA)
    return conn


def log_delegation(
    conn: sqlite3.Connection,
    *,
    cli_id: str,
    auth_method: str,
    prompt_hash: str,
    prompt_preview: str,
    model: Optional[str] = None,
    working_dir: Optional[str] = None,
    success: bool = False,
    exit_code: int = 0,
    duration_ms: float = 0.0,
    output_chars: int = 0,
    error: Optional[str] = None,
    strategy: Optional[str] = None,
) -> int:
    """Insert a delegation record. Returns the row id."""
    cursor = conn.execute(
        """INSERT INTO delegations
           (ts, cli_id, auth_method, prompt_hash, prompt_preview, model, working_dir,
            success, exit_code, duration_ms, output_chars, error, strategy)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            time.time(),
            cli_id,
            auth_method,
            prompt_hash,
            prompt_preview,
            model,
            working_dir,
            1 if success else 0,
            exit_code,
            duration_ms,
            output_chars,
            error,
            strategy,
        ),
    )
    conn.commit()
    return cursor.lastrowid or 0


def get_delegation_history(conn: sqlite3.Connection, limit: int = 50) -> List[Dict[str, Any]]:
    """Return recent delegations, newest first."""
    rows = conn.execute(
        "SELECT * FROM delegations ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_delegation_stats(conn: sqlite3.Connection, window_hours: float = 24) -> Dict[str, Any]:
    """Aggregate stats for delegations within the time window."""
    cutoff = time.time() - (window_hours * 3600)

    rows = conn.execute(
        """SELECT cli_id,
                  COUNT(*) as total,
                  SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successes,
                  AVG(duration_ms) as avg_duration_ms,
                  SUM(output_chars) as total_output_chars
           FROM delegations
           WHERE ts >= ?
           GROUP BY cli_id""",
        (cutoff,),
    ).fetchall()

    per_cli = {}
    for r in rows:
        row = dict(r)
        cli_id = row["cli_id"]
        total = row["total"]
        successes = row["successes"]
        per_cli[cli_id] = {
            "total": total,
            "successes": successes,
            "failures": total - successes,
            "success_rate": round(successes / total * 100, 1) if total > 0 else 0,
            "avg_duration_ms": round(row["avg_duration_ms"] or 0, 1),
            "total_output_chars": row["total_output_chars"] or 0,
        }

    total_row = conn.execute(
        "SELECT COUNT(*) as total, SUM(duration_ms) as total_duration FROM delegations WHERE ts >= ?",
        (cutoff,),
    ).fetchone()

    return {
        "window_hours": window_hours,
        "total_delegations": dict(total_row)["total"] if total_row else 0,
        "total_duration_ms": round(dict(total_row)["total_duration"] or 0, 1) if total_row else 0,
        "per_cli": per_cli,
    }
