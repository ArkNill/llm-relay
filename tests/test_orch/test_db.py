"""Tests for orch/db.py — delegation database."""

import time

import pytest

from llm_relay.orch.db import (
    get_delegation_history,
    get_delegation_stats,
    get_orch_conn,
    log_delegation,
)


@pytest.fixture
def db_conn(tmp_path):
    """Create a fresh in-memory-like DB for each test."""
    db_path = tmp_path / "test.db"
    conn = get_orch_conn(db_path)
    yield conn
    conn.close()


class TestSchema:
    def test_creates_table(self, db_conn):
        tables = db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [dict(r)["name"] for r in tables]
        assert "delegations" in table_names

    def test_idempotent(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn1 = get_orch_conn(db_path)
        conn1.close()
        conn2 = get_orch_conn(db_path)
        tables = conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        assert len([r for r in tables if dict(r)["name"] == "delegations"]) == 1
        conn2.close()


class TestLogDelegation:
    def test_insert(self, db_conn):
        row_id = log_delegation(
            db_conn,
            cli_id="claude-code",
            auth_method="cli_oauth",
            prompt_hash="abc123",
            prompt_preview="hello world",
            success=True,
            duration_ms=1234.5,
            output_chars=100,
        )
        assert row_id > 0

    def test_insert_with_error(self, db_conn):
        row_id = log_delegation(
            db_conn,
            cli_id="gemini-cli",
            auth_method="api_key",
            prompt_hash="def456",
            prompt_preview="test prompt",
            success=False,
            exit_code=1,
            error="auth failed",
        )
        assert row_id > 0

    def test_insert_with_all_fields(self, db_conn):
        row_id = log_delegation(
            db_conn,
            cli_id="openai-codex",
            auth_method="cli_oauth",
            prompt_hash="ghi789",
            prompt_preview="full test",
            model="gpt-5.4",
            working_dir="/tmp/project",
            success=True,
            exit_code=0,
            duration_ms=5000.0,
            output_chars=2000,
            strategy="strongest",
        )
        assert row_id > 0


class TestGetHistory:
    def test_empty(self, db_conn):
        history = get_delegation_history(db_conn)
        assert history == []

    def test_returns_newest_first(self, db_conn):
        log_delegation(db_conn, cli_id="a", auth_method="cli_oauth", prompt_hash="1", prompt_preview="first")
        time.sleep(0.01)
        log_delegation(db_conn, cli_id="b", auth_method="cli_oauth", prompt_hash="2", prompt_preview="second")
        history = get_delegation_history(db_conn, limit=10)
        assert len(history) == 2
        assert history[0]["cli_id"] == "b"
        assert history[1]["cli_id"] == "a"

    def test_limit(self, db_conn):
        for i in range(5):
            log_delegation(db_conn, cli_id="test", auth_method="cli_oauth", prompt_hash=str(i), prompt_preview=str(i))
        history = get_delegation_history(db_conn, limit=3)
        assert len(history) == 3


class TestGetStats:
    def test_empty(self, db_conn):
        stats = get_delegation_stats(db_conn)
        assert stats["total_delegations"] == 0
        assert stats["per_cli"] == {}

    def test_aggregation(self, db_conn):
        log_delegation(db_conn, cli_id="claude-code", auth_method="cli_oauth",
                       prompt_hash="1", prompt_preview="a", success=True, duration_ms=100, output_chars=50)
        log_delegation(db_conn, cli_id="claude-code", auth_method="cli_oauth",
                       prompt_hash="2", prompt_preview="b", success=False, duration_ms=200, output_chars=0)
        log_delegation(db_conn, cli_id="gemini-cli", auth_method="api_key",
                       prompt_hash="3", prompt_preview="c", success=True, duration_ms=50, output_chars=100)

        stats = get_delegation_stats(db_conn, window_hours=1)
        assert stats["total_delegations"] == 3
        assert "claude-code" in stats["per_cli"]
        assert stats["per_cli"]["claude-code"]["total"] == 2
        assert stats["per_cli"]["claude-code"]["successes"] == 1
        assert stats["per_cli"]["claude-code"]["success_rate"] == 50.0
        assert stats["per_cli"]["gemini-cli"]["total"] == 1
        assert stats["per_cli"]["gemini-cli"]["success_rate"] == 100.0
