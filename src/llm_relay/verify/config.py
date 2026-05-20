"""verify config -- confirm local llm-relay state (DB, config files, ports).

Checks examine `~/.llm-relay/` (or whatever `db_dir_for_env()` returns) and
neighbouring filesystem state. They are read-only with one exception:
`db_writable` opens a transaction, inserts a marker row, then rolls back --
this is the only reliable way to detect a read-only mount or quota issue.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from llm_relay.setup_init import _is_port_in_use, _read_json, db_dir_for_env
from llm_relay.verify import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_WARN,
    VerifyCheck,
    VerifyReport,
    run_check,
)

# Tables we expect after a successful `llm-relay init`.
_REQUIRED_TABLES = {"requests"}

# Env vars that were renamed during the cc-relay → llm-relay merge.
# Their presence is not destructive, but they're ignored by current code
# and silently shadowed by their LLM_RELAY_* equivalents.
_DEPRECATED_ENV_PREFIXES = ("CCPULSE_", "CC_RELAY_")

_DEFAULT_PROXY_PORT = 8083


def _check_db_dir_exists() -> VerifyCheck:
    db_dir = db_dir_for_env()
    if db_dir.is_dir():
        return VerifyCheck(
            id="", label="",
            status=STATUS_PASS,
            detail="db dir exists at {}".format(db_dir),
            data={"path": str(db_dir)},
        )
    return VerifyCheck(
        id="", label="",
        status=STATUS_FAIL,
        detail="db dir not found at {}".format(db_dir),
        remediation="Run `llm-relay init` to create the directory.",
        data={"path": str(db_dir)},
    )


def _check_db_initialized() -> VerifyCheck:
    """usage.db exists AND has the expected table schema."""
    db_path = db_dir_for_env() / "usage.db"
    if not db_path.is_file():
        return VerifyCheck(
            id="", label="",
            status=STATUS_FAIL,
            detail="usage.db not found at {}".format(db_path),
            remediation="Run `llm-relay init` to initialize the database.",
            data={"path": str(db_path)},
        )
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            present = {row[0] for row in rows}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return VerifyCheck(
            id="", label="",
            status=STATUS_FAIL,
            detail="failed to open usage.db: {}".format(exc),
            remediation="Run `llm-relay init` (the file may be corrupt).",
            data={"path": str(db_path)},
        )
    missing = _REQUIRED_TABLES - present
    if missing:
        return VerifyCheck(
            id="", label="",
            status=STATUS_FAIL,
            detail="usage.db is missing tables: {}".format(", ".join(sorted(missing))),
            remediation="Run `llm-relay init` to recreate the schema.",
            data={"missing_tables": sorted(missing), "present_tables": sorted(present)},
        )
    return VerifyCheck(
        id="", label="",
        status=STATUS_PASS,
        detail="usage.db schema OK ({} table(s) present)".format(len(present)),
        data={"present_tables": sorted(present)},
    )


def _check_db_writable() -> VerifyCheck:
    """Round-trip a transaction to detect read-only mounts / quota issues."""
    db_path = db_dir_for_env() / "usage.db"
    if not db_path.is_file():
        return VerifyCheck(
            id="", label="",
            status=STATUS_FAIL,
            detail="usage.db not found (cannot test write)",
            remediation="Run `llm-relay init` first.",
        )
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("BEGIN")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS _verify_probe (ts INTEGER PRIMARY KEY)"
            )
            conn.execute("INSERT INTO _verify_probe (ts) VALUES (?)", (1,))
            conn.execute("ROLLBACK")
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return VerifyCheck(
            id="", label="",
            status=STATUS_FAIL,
            detail="db write probe failed: {}".format(exc),
            remediation="Check filesystem permissions and free space on the db dir.",
        )
    return VerifyCheck(
        id="", label="",
        status=STATUS_PASS,
        detail="db accepts writes (probe transaction rolled back)",
    )


def _check_config_file() -> VerifyCheck:
    """config.json is optional but expected after init."""
    config_path = db_dir_for_env() / "config.json"
    if not config_path.is_file():
        return VerifyCheck(
            id="", label="",
            status=STATUS_WARN,
            detail="config.json not found at {}".format(config_path),
            remediation="Run `llm-relay init` to generate the config file.",
            data={"path": str(config_path)},
        )
    try:
        data = _read_json(config_path)
    except Exception as exc:  # noqa: BLE001
        return VerifyCheck(
            id="", label="",
            status=STATUS_FAIL,
            detail="config.json present but unparseable: {}".format(exc),
            remediation="Re-run `llm-relay init` or delete the file and let init recreate it.",
        )
    return VerifyCheck(
        id="", label="",
        status=STATUS_PASS,
        detail="config.json parseable ({} top-level keys)".format(len(data)),
        data={"path": str(config_path), "keys": sorted(data.keys())},
    )


def _check_knowledge_dir() -> VerifyCheck:
    """Knowledge directory is optional (only used if the knowledge module
    is enabled). Missing → warn, not fail."""
    knowledge_dir = db_dir_for_env() / "knowledge"
    if knowledge_dir.is_dir():
        return VerifyCheck(
            id="", label="",
            status=STATUS_PASS,
            detail="knowledge dir exists at {}".format(knowledge_dir),
            data={"path": str(knowledge_dir)},
        )
    return VerifyCheck(
        id="", label="",
        status=STATUS_WARN,
        detail="knowledge dir not found at {}".format(knowledge_dir),
        remediation="Run `llm-relay init` (knowledge dir is auto-created).",
        data={"path": str(knowledge_dir)},
    )


def _check_port_available(port: int = _DEFAULT_PROXY_PORT) -> VerifyCheck:
    """Default proxy port should be either free, or already bound by our own
    server. We can't distinguish those without a deeper probe, so we report
    `warn` when the port is busy and let the operator/agent decide.
    """
    if not _is_port_in_use(port):
        return VerifyCheck(
            id="", label="",
            status=STATUS_PASS,
            detail="proxy port {} is free".format(port),
            data={"port": port, "in_use": False},
        )
    return VerifyCheck(
        id="", label="",
        status=STATUS_WARN,
        detail="proxy port {} is already bound (could be our own server)".format(port),
        remediation="Pass `--port <other>` to `llm-relay serve` or stop the conflicting process.",
        data={"port": port, "in_use": True},
    )


def _check_no_deprecated_env() -> VerifyCheck:
    """CCPULSE_* / CC_RELAY_* env vars are silently ignored by current code
    and indicate a stale shell profile -- worth flagging so the operator
    cleans them up.
    """
    found = [
        name for name in os.environ
        if any(name.startswith(prefix) for prefix in _DEPRECATED_ENV_PREFIXES)
    ]
    if not found:
        return VerifyCheck(
            id="", label="",
            status=STATUS_PASS,
            detail="no deprecated env vars set",
        )
    return VerifyCheck(
        id="", label="",
        status=STATUS_WARN,
        detail="deprecated env vars set: {}".format(", ".join(sorted(found))),
        remediation=(
            "Rename `CCPULSE_*` / `CC_RELAY_*` to `LLM_RELAY_*` in your shell rc "
            "or remove them. Current code ignores the legacy names."
        ),
        data={"found": sorted(found)},
    )


def _check(check_id, label, fn):
    """Local convenience to keep the registration table compact."""
    return (check_id, label, fn)


_CHECKS = [
    _check("db_dir_exists", "llm-relay db directory exists", _check_db_dir_exists),
    _check("db_initialized", "usage.db schema is initialized", _check_db_initialized),
    _check("db_writable", "usage.db accepts writes", _check_db_writable),
    _check("config_file", "config.json is present and parseable", _check_config_file),
    _check("knowledge_dir", "knowledge directory exists (optional)", _check_knowledge_dir),
    _check("port_available", "default proxy port is usable", _check_port_available),
    _check("no_deprecated_env", "no CCPULSE_*/CC_RELAY_* env vars set", _check_no_deprecated_env),
]


def verify_config(*, port: int = _DEFAULT_PROXY_PORT) -> VerifyReport:
    """Run all config-time checks and return a structured report.

    `port` is the proxy port to probe for availability (default 8083).
    """
    report = VerifyReport(target="config")
    for check_id, label, fn in _CHECKS:
        if check_id == "port_available":
            report.checks.append(
                run_check(check_id, label, lambda p=port: _check_port_available(p))
            )
        else:
            report.checks.append(run_check(check_id, label, fn))
    return report


# Re-export for tests that want to call the underlying helpers directly.
__all__ = ["verify_config", "Path"]
