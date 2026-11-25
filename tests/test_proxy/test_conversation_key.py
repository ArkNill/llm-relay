"""conversation_turns.conversation_key (2026-10-23, R9-7).

Each stored turn records the fingerprint of its conversation's first message, the
key history already uses to tell a session's parent conversation from its
subagents and helper calls. NULL marks legacy rows. SQLite gets the column and
index from get_conn()'s migrations; on PostgreSQL the operator applies
docs/migrate-conversation-key.py and the relay adapts to whichever schema it finds.
"""

from __future__ import annotations

import logging
import re
import sqlite3

import pytest
from test_proxy.test_history import _make_db

from llm_relay.proxy import db
from llm_relay.proxy.db import get_conn, get_session_history, log_conversation_turn
from llm_relay.proxy.history import _fingerprint_message, capture_delegation_turn, capture_turn_streamed, reset_state

RESP = [{"type": "text", "text": "ok"}]
INDEX = "idx_conversation_turns_conv"


@pytest.fixture(autouse=True)
def _reset():
    reset_state()
    yield
    reset_state()


def _columns(conn):
    return [row[1] for row in conn.execute("PRAGMA table_info(conversation_turns)")]


def test_migration_adds_the_column_and_index(tmp_path):
    conn = get_conn(tmp_path / "fresh.db")
    assert "conversation_key" in _columns(conn)
    assert INDEX in [row[1] for row in conn.execute("PRAGMA index_list(conversation_turns)")]
    assert [row[2] for row in conn.execute("PRAGMA index_info({})".format(INDEX))] == [
        "session_id", "conversation_key", "turn_number"]


def test_migration_upgrades_a_legacy_file_and_keeps_old_rows_null(tmp_path):
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(str(path))
    legacy.execute("""CREATE TABLE conversation_turns (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, session_id TEXT NOT NULL, request_id INTEGER,
        turn_number INTEGER NOT NULL, storage_mode TEXT NOT NULL DEFAULT 'delta', request_messages TEXT,
        response_message TEXT, thinking_blocks TEXT, model TEXT, temperature REAL, max_tokens INTEGER,
        total_message_count INTEGER, previous_message_count INTEGER, request_size_bytes INTEGER DEFAULT 0,
        response_size_bytes INTEGER DEFAULT 0, provider TEXT DEFAULT 'anthropic')""")
    legacy.execute("INSERT INTO conversation_turns (ts, session_id, turn_number) VALUES (0, 'old', 1)")
    legacy.commit()
    legacy.close()

    conn = get_conn(path)
    assert "conversation_key" in _columns(conn)
    assert get_session_history(conn, "old")[0]["conversation_key"] is None


def test_capture_records_the_conversation_fingerprint(tmp_path):
    conn = get_conn(tmp_path / "capture.db")
    parent = {"messages": [{"role": "user", "content": "main task"}]}
    helper = {"messages": [{"role": "user", "content": "Perform a web search for the query: x"}]}
    capture_turn_streamed(conn, "s", parent, RESP)
    capture_turn_streamed(conn, "s", helper, RESP)
    capture_delegation_turn(conn, "s", "codex", "prompt", "output")

    keys = [t["conversation_key"] for t in get_session_history(conn, "s")]
    assert keys[0] == _fingerprint_message(parent["messages"][0])
    assert keys[1] == _fingerprint_message(helper["messages"][0]) != keys[0]
    assert all(re.fullmatch(r"[0-9a-f]{16}", k) for k in keys[:2])
    assert keys[2] is None  # delegation turns have no conversation fingerprint


def test_a_table_without_the_column_still_captures_and_reads():
    conn = _make_db()  # hand-made schema, no conversation_key
    capture_turn_streamed(conn, "s", {"messages": [{"role": "user", "content": "hi"}]}, RESP)
    rows = get_session_history(conn, "s")
    assert len(rows) == 1 and rows[0]["conversation_key"] is None


class _FakePG:
    """Records statements; answers the information_schema probe and INSERT ... RETURNING id."""

    def __init__(self, has_column):
        self.has_column = has_column
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append((sql, params))
        row = None
        if "information_schema.columns" in sql:
            row = {"present": 1} if self.has_column else None
        elif "RETURNING id" in sql:
            row = {"id": 42}
        return type("_Cursor", (), {"fetchone": lambda self: row})()

    def commit(self):
        pass


@pytest.fixture
def fake_pg(monkeypatch):
    monkeypatch.setattr(db, "_USE_PG", True)
    monkeypatch.setattr(db, "Jsonb", lambda value: value, raising=False)
    monkeypatch.setattr(db, "_pg_conv_key", {"present": False, "checked": None, "warned": False})


def _inserts(conn):
    return [(sql, params) for sql, params in conn.sql if sql.startswith("INSERT INTO conversation_turns")]


def test_postgres_insert_carries_the_key_once_the_column_exists(fake_pg):
    conn = _FakePG(has_column=True)
    assert log_conversation_turn(conn, session_id="s", turn_number=1, conversation_key="0123456789abcdef") == 42
    log_conversation_turn(conn, session_id="s", turn_number=2, conversation_key="0123456789abcdef")

    (sql, params), _second = _inserts(conn)
    assert "conversation_key" in sql and params[-1] == "0123456789abcdef"
    assert sum("information_schema" in s for s, _ in conn.sql) == 1  # a present column is cached


def test_postgres_without_the_column_writes_legacy_rows_and_warns_once(fake_pg, caplog):
    conn = _FakePG(has_column=False)
    with caplog.at_level(logging.WARNING, logger="llm-relay.db"):
        log_conversation_turn(conn, session_id="s", turn_number=1, conversation_key="0123456789abcdef")
        log_conversation_turn(conn, session_id="s", turn_number=2, conversation_key="0123456789abcdef")

    assert all("conversation_key" not in sql for sql, _ in _inserts(conn))
    assert sum("information_schema" in s for s, _ in conn.sql) == 1  # re-checked at most once a minute
    assert len([r for r in caplog.records if "conversation_key" in r.getMessage()]) == 1
