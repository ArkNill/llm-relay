"""Dedicated provider_* tables for Antigravity CLI traffic (llm_relay.antigravity.store).

The Claude Code proxy owns ``requests`` and ``conversation_turns``; the store must
only ever name its three own tables. SQLite runs here (conftest points
LLM_RELAY_DB at a temporary file); the PostgreSQL side is checked on the DDL text
and with a fake connection, never against a database.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from llm_relay.antigravity import PROVIDER, store
from llm_relay.proxy import db
from llm_relay.proxy.db import get_conn

NUL = chr(0)
REPLACEMENT = chr(0xFFFD)
OTHER_PROVIDER = "other-cli"

# provider_requests columns of the first release, then the 15 added in 2026-11 (spec section 1, that order).
ORIGINAL_REQUEST_COLUMNS = [
    "id", "ts", "provider", "session_id", "parent_session_id", "request_type", "endpoint", "request_id",
    "model", "model_version", "thinking_level", "input_tokens", "output_tokens", "cache_read",
    "thinking_tokens", "total_tokens", "prompt_tokens", "status_code", "latency_ms", "is_stream",
    "stream_end", "message_count", "step_index", "request_body_bytes", "response_bytes",
    "client_session_id", "raw_usage", "error",
]
NEW_REQUEST_COLUMNS = [
    "finish_reason", "trace_id", "response_id", "trajectory_id", "system_chars", "tools_count", "headers_ms",
    "first_thought_ms", "first_answer_ms", "response_message", "thinking_blocks", "response_extra",
    "system_hash", "tools_hash", "response_hash",
]

# Column lists exactly as specified, in table order.
EXPECTED_COLUMNS = {
    "provider_requests": ORIGINAL_REQUEST_COLUMNS + NEW_REQUEST_COLUMNS,
    "provider_turns": [
        "id", "ts", "provider", "session_id", "parent_session_id", "request_row_id", "request_id",
        "turn_number", "storage_mode", "request_messages", "response_message", "thinking_blocks", "model",
        "total_message_count", "previous_message_count", "first_fingerprint", "step_index",
        "request_size_bytes", "response_size_bytes", "last_user_prompt",
    ],
    "provider_quota": [
        "id", "ts", "provider", "group_name", "bucket_id", "quota_window", "remaining_fraction", "reset_time",
        "display_name",
    ],
    "provider_blobs": ["id", "ts", "provider", "kind", "sha256", "size_bytes", "body"],
}
EXPECTED_INDEXES = {
    "idx_provider_requests_ts": ("provider_requests", ["ts"]),
    "idx_provider_requests_session": ("provider_requests", ["provider", "session_id", "ts"]),
    "idx_provider_turns_session": ("provider_turns", ["provider", "session_id", "turn_number"]),
    "idx_provider_turns_ts": ("provider_turns", ["ts"]),
    "idx_provider_quota_bucket": ("provider_quota", ["provider", "bucket_id", "ts"]),
    "idx_provider_blobs_key": ("provider_blobs", ["provider", "kind", "sha256"]),
}
UNIQUE_INDEX_NAMES = {"idx_provider_blobs_key"}
# "CREATE [UNIQUE] INDEX IF NOT EXISTS name ON table (columns)"
INDEX_RE = re.compile(r"CREATE (UNIQUE )?INDEX IF NOT EXISTS (\w+) ON (\w+) \(([\w, ]+)\)")


@pytest.fixture(autouse=True)
def _sqlite_only():
    # conftest points LLM_RELAY_DB at a temporary SQLite file; refuse to run against anything else.
    assert not db._USE_PG


@pytest.fixture
def conn(tmp_path):
    connection = get_conn(tmp_path / "store.db")
    store.ensure_schema(connection)
    yield connection
    connection.close()


def _parse_create_table(statement):
    """(table, [column names]) of one CREATE TABLE statement."""
    head, body = statement.split("(", 1)
    body = body.rsplit(")", 1)[0]
    return head.split()[-1], [part.split()[0] for part in body.split(",")]


def _tables_in(statements):
    return dict(_parse_create_table(s) for s in statements if s.strip().startswith("CREATE TABLE"))


def _sqlite_statements():
    return [s.strip() for s in store.SQLITE_SCHEMA.split(";") if s.strip()]


def _contains_nul(value):
    if isinstance(value, str):
        return NUL in value
    if isinstance(value, dict):
        return any(_contains_nul(k) or _contains_nul(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_nul(v) for v in value)
    return False


# ── Schema ──


def test_tables_constant_and_both_dialects_list_the_specified_columns_in_order():
    assert store.TABLES == ("provider_requests", "provider_turns", "provider_quota")
    sqlite_tables = _tables_in(_sqlite_statements())
    pg_tables = _tables_in(store.PG_DDL)
    assert list(sqlite_tables) == list(store.ALL_TABLES) == list(pg_tables)
    for table in store.ALL_TABLES:
        assert sqlite_tables[table] == pg_tables[table] == EXPECTED_COLUMNS[table], table


def test_dialect_types_differ_only_where_specified():
    def types_of(statements):
        out = {}
        for s in statements:
            if s.startswith("CREATE TABLE"):
                table = s.split()[5]
                body = s.split("(", 1)[1].rsplit(")", 1)[0]
                for part in body.split(","):
                    name, _, sql_type = part.strip().partition(" ")
                    out[(table, name)] = sql_type
        return out

    sqlite_types, pg_types = types_of(_sqlite_statements()), types_of(store.PG_DDL)
    differing = {key: (sqlite_types[key], pg_types[key]) for key in sqlite_types if sqlite_types[key] != pg_types[key]}
    ident = ("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY")
    ts = ("REAL NOT NULL", "TIMESTAMPTZ NOT NULL")
    expected = {}
    for table in store.ALL_TABLES:
        expected[(table, "id")] = ident
        expected[(table, "ts")] = ts
    expected.update({
        ("provider_requests", "status_code"): ("INTEGER", "SMALLINT"),
        ("provider_requests", "is_stream"): ("INTEGER DEFAULT 0", "BOOLEAN DEFAULT FALSE"),
        ("provider_requests", "raw_usage"): ("TEXT", "JSONB"),
        ("provider_requests", "response_message"): ("TEXT", "JSONB"),
        ("provider_requests", "thinking_blocks"): ("TEXT", "JSONB"),
        ("provider_requests", "response_extra"): ("TEXT", "JSONB"),
        ("provider_turns", "request_row_id"): ("INTEGER", "BIGINT"),
        ("provider_turns", "request_messages"): ("TEXT", "JSONB"),
        ("provider_turns", "response_message"): ("TEXT", "JSONB"),
        ("provider_turns", "thinking_blocks"): ("TEXT", "JSONB"),
        ("provider_blobs", "body"): ("TEXT", "JSONB"),
    })
    assert differing == expected
    assert sqlite_types[("provider_turns", "storage_mode")] == "TEXT NOT NULL DEFAULT 'delta'"
    assert sqlite_types[("provider_quota", "bucket_id")] == "TEXT NOT NULL"


def test_pg_ddl_only_creates_and_only_names_the_three_provider_tables():
    assert len(store.PG_DDL) == len(store.ALL_TABLES) + len(EXPECTED_INDEXES)
    seen_indexes = {}
    for statement in store.PG_DDL:
        assert statement.startswith(("CREATE TABLE IF NOT EXISTS ", "CREATE INDEX IF NOT EXISTS ",
                                     "CREATE UNIQUE INDEX IF NOT EXISTS ")), statement
        assert ";" not in statement  # one statement per element
        upper = statement.upper()
        for word in ("DROP", "ALTER", "REFERENCES", "TRUNCATE", "DELETE", "UPDATE", "INSERT", "RENAME", "LOCK",
                     "CONCURRENTLY", "GRANT", "COMMENT"):
            assert not re.search(r"\b{}\b".format(word), upper), (word, statement)
        # Claude Code proxy tables must never appear (word boundary: provider_requests is fine).
        assert not re.search(r"\b(requests|conversation_turns|microcompact_events)\b", statement), statement
        if statement.startswith("CREATE TABLE"):
            assert statement.split()[5] in store.ALL_TABLES
        else:
            match = INDEX_RE.fullmatch(statement)
            assert match, statement
            seen_indexes[match.group(2)] = (match.group(3), [c.strip() for c in match.group(4).split(",")])
            assert match.group(3) in store.ALL_TABLES
            assert bool(match.group(1)) == (match.group(2) in UNIQUE_INDEX_NAMES), statement
    assert seen_indexes == EXPECTED_INDEXES


def test_sqlite_schema_has_the_same_indexes():
    seen = {}
    for statement in _sqlite_statements():
        assert statement.startswith(("CREATE TABLE IF NOT EXISTS ", "CREATE INDEX IF NOT EXISTS ",
                                     "CREATE UNIQUE INDEX IF NOT EXISTS ")), statement
        match = INDEX_RE.fullmatch(statement)
        if match:
            seen[match.group(2)] = (match.group(3), [c.strip() for c in match.group(4).split(",")])
            assert bool(match.group(1)) == (match.group(2) in UNIQUE_INDEX_NAMES), statement
    assert seen == EXPECTED_INDEXES


def test_ensure_schema_is_idempotent_and_tables_present_before_after(tmp_path):
    connection = get_conn(tmp_path / "fresh.db")
    try:
        assert store.tables_present(connection) == {t: False for t in store.TABLES}
        store.ensure_schema(connection)
        rid = store.log_request(connection, provider=PROVIDER, endpoint="/v1internal:streamGenerateContent")
        store.ensure_schema(connection)  # second run keeps tables and rows
        assert store.tables_present(connection) == {t: True for t in store.TABLES}
        assert connection.execute("SELECT count(*) FROM provider_requests").fetchone()[0] == 1
        assert rid == 1
        for table in store.TABLES:
            columns = [row[1] for row in connection.execute("PRAGMA table_info({})".format(table))]
            assert columns == EXPECTED_COLUMNS[table]
        for name, (table, columns) in EXPECTED_INDEXES.items():
            assert [row[2] for row in connection.execute("PRAGMA index_info({})".format(name))] == columns
            assert name in [row[1] for row in connection.execute("PRAGMA index_list({})".format(table))]
    finally:
        connection.close()


def test_tables_present_reports_a_partial_schema(tmp_path):
    connection = get_conn(tmp_path / "partial.db")
    try:
        connection.execute(_sqlite_statements()[0])  # provider_requests only
        assert store.tables_present(connection) == {
            "provider_requests": True, "provider_turns": False, "provider_quota": False}
    finally:
        connection.close()


def test_ensure_schema_leaves_the_claude_code_tables_alone(conn):
    before = conn.execute("SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'provider_%'"
                          " AND name NOT LIKE 'idx_provider_%' ORDER BY name").fetchall()
    store.ensure_schema(conn)
    store.log_request(conn, provider=PROVIDER, endpoint="/x")
    after = conn.execute("SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'provider_%'"
                         " AND name NOT LIKE 'idx_provider_%' ORDER BY name").fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
    assert conn.execute("SELECT count(*) FROM requests").fetchone()[0] == 0


# ── strip_nul ──


def test_strip_nul_replaces_in_nested_values_and_dict_keys():
    value = {
        "a" + NUL: ["x" + NUL + "y", ("t" + NUL, 1, None), {"k" + NUL + "k": {"deep": "z" + NUL}}],
        "n": 3.5,
        "b": True,
    }
    out = store.strip_nul(value)
    assert out == {
        "a" + REPLACEMENT: ["x" + REPLACEMENT + "y", ("t" + REPLACEMENT, 1, None),
                            {"k" + REPLACEMENT + "k": {"deep": "z" + REPLACEMENT}}],
        "n": 3.5,
        "b": True,
    }
    assert isinstance(out["a" + REPLACEMENT][1], tuple)
    assert not _contains_nul(out)
    assert _contains_nul(value)  # input is not mutated


def test_strip_nul_leaves_other_values_unchanged():
    marker = object()
    for value in (None, 0, 1.5, True, b"bytes\x00", marker, "plain", "한국어"):
        assert store.strip_nul(value) is value


# ── log_request ──


def test_plain_text_columns_are_stripped_of_nul_too(conn):
    """PostgreSQL rejects NUL in TEXT as well as in jsonb, so every string parameter is cleaned."""
    rid = store.log_request(
        conn, provider=PROVIDER, endpoint="streamGenerateContent", session_id="s" + NUL + "1", model="m" + NUL,
    )
    row = dict(conn.execute("SELECT session_id, model FROM provider_requests WHERE id = ?", (rid,)).fetchone())
    assert row == {"session_id": "s\ufffd1", "model": "m\ufffd"}


def test_log_request_round_trip_with_raw_usage_and_returned_id(conn):
    usage = {"promptTokenCount": 120, "candidatesTokenCount": 30, "thoughtsTokenCount": 7,
             "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 120}]}
    first = store.log_request(
        conn, provider=PROVIDER, endpoint="/v1internal:streamGenerateContent", session_id="s1",
        parent_session_id="p1", request_type="agent", request_id="r-1", model="gemini-3-pro",
        model_version="gemini-3-pro-001", thinking_level="high", input_tokens=100, output_tokens=30,
        cache_read=20, thinking_tokens=7, total_tokens=157, prompt_tokens=120, status_code=200,
        latency_ms=812.5, is_stream=True, stream_end="STOP", message_count=4, step_index=2,
        request_body_bytes=2048, response_bytes=777, client_session_id="c1", raw_usage=usage,
        error="partial" + NUL + "tail",
    )
    second = store.log_request(conn, provider=PROVIDER, endpoint="/v1internal:loadCodeAssist")
    assert first > 0 and second == first + 1

    row = dict(conn.execute("SELECT * FROM provider_requests WHERE id = ?", (first,)).fetchone())
    assert json.loads(row.pop("raw_usage")) == usage
    assert isinstance(row.pop("ts"), float)
    assert row == {
        "id": first, "provider": PROVIDER, "session_id": "s1", "parent_session_id": "p1", "request_type": "agent",
        "endpoint": "/v1internal:streamGenerateContent", "request_id": "r-1", "model": "gemini-3-pro",
        "model_version": "gemini-3-pro-001", "thinking_level": "high", "input_tokens": 100, "output_tokens": 30,
        "cache_read": 20, "thinking_tokens": 7, "total_tokens": 157, "prompt_tokens": 120, "status_code": 200,
        "latency_ms": 812.5, "is_stream": 1, "stream_end": "STOP", "message_count": 4, "step_index": 2,
        "request_body_bytes": 2048, "response_bytes": 777, "client_session_id": "c1",
        "error": "partial" + REPLACEMENT + "tail",
        **{column: None for column in NEW_REQUEST_COLUMNS},  # not passed: NULL
    }

    defaults = dict(conn.execute("SELECT * FROM provider_requests WHERE id = ?", (second,)).fetchone())
    assert defaults["raw_usage"] is None and defaults["error"] is None and defaults["status_code"] is None
    assert defaults["is_stream"] == 0 and defaults["input_tokens"] == 0 and defaults["response_bytes"] == 0


# ── log_turn / get_last_turn_state ──


def test_log_turn_round_trip_keeps_korean_and_replaces_nul(conn):
    messages = [
        {"role": "user", "parts": [{"text": "안녕하세요, 코드 리뷰" + NUL + " 부탁합니다"}]},
        {"role": "model", "parts": [{"functionCall": {"name": "read_file", "args": {"경로" + NUL: "/tmp/a"}}}]},
    ]
    response = {"role": "model", "parts": [{"text": "검토 결과" + NUL}]}
    thinking = [{"thought": True, "text": "생각" + NUL + "중"}]
    turn_id = store.log_turn(
        conn, provider=PROVIDER, session_id="s1", turn_number=1, request_messages=messages,
        response_message=response, thinking_blocks=thinking, model="gemini-3-pro", parent_session_id="p1",
        request_row_id=5, request_id="r-1", total_message_count=2, previous_message_count=0,
        first_fingerprint="fp1", step_index=0, request_size_bytes=300, response_size_bytes=40,
        last_user_prompt="리뷰" + NUL + "해줘",
    )
    assert turn_id > 0

    row = conn.execute("SELECT * FROM provider_turns WHERE id = ?", (turn_id,)).fetchone()
    for column in ("request_messages", "response_message", "thinking_blocks"):
        assert "\\u0000" not in row[column], column
        assert not _contains_nul(json.loads(row[column])), column
    stored_messages = json.loads(row["request_messages"])
    assert stored_messages[0]["parts"][0]["text"] == "안녕하세요, 코드 리뷰" + REPLACEMENT + " 부탁합니다"
    assert stored_messages[1]["parts"][0]["functionCall"]["args"] == {"경로" + REPLACEMENT: "/tmp/a"}
    assert json.loads(row["response_message"]) == {"role": "model", "parts": [{"text": "검토 결과" + REPLACEMENT}]}
    assert json.loads(row["thinking_blocks"]) == [{"thought": True, "text": "생각" + REPLACEMENT + "중"}]
    assert row["last_user_prompt"] == "리뷰" + REPLACEMENT + "해줘"
    assert NUL not in row["last_user_prompt"]
    assert (row["provider"], row["session_id"], row["parent_session_id"], row["request_row_id"]) == (
        PROVIDER, "s1", "p1", 5)
    assert (row["request_id"], row["turn_number"], row["storage_mode"], row["model"]) == (
        "r-1", 1, "delta", "gemini-3-pro")
    assert (row["total_message_count"], row["previous_message_count"], row["first_fingerprint"]) == (2, 0, "fp1")
    assert (row["step_index"], row["request_size_bytes"], row["response_size_bytes"]) == (0, 300, 40)


def test_log_turn_stores_none_json_as_null(conn):
    turn_id = store.log_turn(conn, provider=PROVIDER, session_id="s1", turn_number=1, storage_mode="full")
    row = conn.execute("SELECT * FROM provider_turns WHERE id = ?", (turn_id,)).fetchone()
    assert row["request_messages"] is None and row["response_message"] is None and row["thinking_blocks"] is None
    assert row["storage_mode"] == "full" and row["total_message_count"] == 0


def test_get_last_turn_state_returns_highest_turn_per_provider_and_session(conn):
    assert store.get_last_turn_state(conn, PROVIDER, "s1") is None
    store.log_turn(conn, provider=PROVIDER, session_id="s1", turn_number=1, total_message_count=2,
                   first_fingerprint="fp-a")
    store.log_turn(conn, provider=PROVIDER, session_id="s1", turn_number=3, total_message_count=6,
                   first_fingerprint="fp-a")
    store.log_turn(conn, provider=PROVIDER, session_id="s1", turn_number=2, total_message_count=4,
                   first_fingerprint="fp-a")  # inserted later, lower turn
    # Same session id under another provider, and another session: higher numbers that must not leak.
    store.log_turn(conn, provider=OTHER_PROVIDER, session_id="s1", turn_number=9, total_message_count=99,
                   first_fingerprint="fp-other")
    store.log_turn(conn, provider=PROVIDER, session_id="s2", turn_number=7, total_message_count=14,
                   first_fingerprint="fp-s2")

    assert store.get_last_turn_state(conn, PROVIDER, "s1") == {
        "turn_number": 3, "total_message_count": 6, "first_fingerprint": "fp-a"}
    assert store.get_last_turn_state(conn, PROVIDER, "s2") == {
        "turn_number": 7, "total_message_count": 14, "first_fingerprint": "fp-s2"}
    assert store.get_last_turn_state(conn, OTHER_PROVIDER, "s1")["turn_number"] == 9
    assert store.get_last_turn_state(conn, PROVIDER, "unknown") is None
    assert store.get_last_turn_state(conn, OTHER_PROVIDER, "s2") is None


def test_get_last_turn_state_breaks_turn_ties_by_newest_id(conn):
    store.log_turn(conn, provider=PROVIDER, session_id="s1", turn_number=4, total_message_count=8,
                   first_fingerprint="old")
    store.log_turn(conn, provider=PROVIDER, session_id="s1", turn_number=4, total_message_count=9,
                   first_fingerprint="new")
    assert store.get_last_turn_state(conn, PROVIDER, "s1") == {
        "turn_number": 4, "total_message_count": 9, "first_fingerprint": "new"}


# ── quota ──


def _bucket(bucket_id, fraction, **extra):
    entry = {"group_name": "Gemini Pro", "bucket_id": bucket_id, "quota_window": "5h",
             "remaining_fraction": fraction, "reset_time": "2026-11-20T12:00:00Z", "display_name": bucket_id.upper()}
    entry.update(extra)
    return entry


def test_log_quota_skips_entries_without_bucket_id(conn):
    buckets = [
        _bucket("gemini-pro", 0.75),
        {"group_name": "no id", "remaining_fraction": 0.1},
        {"bucket_id": None, "remaining_fraction": 0.2},
        {"bucket_id": "", "remaining_fraction": 0.3},
        "not a dict",
        {"bucket_id": "flash"},  # every other key missing -> NULL
    ]
    assert store.log_quota(conn, provider=PROVIDER, buckets=buckets) == 2
    rows = [dict(r) for r in conn.execute("SELECT * FROM provider_quota ORDER BY id").fetchall()]
    assert [r["bucket_id"] for r in rows] == ["gemini-pro", "flash"]
    assert rows[0]["remaining_fraction"] == 0.75 and rows[0]["display_name"] == "GEMINI-PRO"
    assert rows[1]["group_name"] is None and rows[1]["remaining_fraction"] is None and rows[1]["reset_time"] is None
    assert rows[0]["ts"] == rows[1]["ts"]  # one snapshot, one timestamp
    assert store.log_quota(conn, provider=PROVIDER, buckets=[]) == 0
    assert store.log_quota(conn, provider=PROVIDER, buckets=[{"group_name": "x"}]) == 0


def test_get_latest_quota_returns_the_newest_row_per_bucket(conn, monkeypatch):
    clock = iter([100.0, 200.0, 300.0, 150.0])
    monkeypatch.setattr(db, "_ts_now", lambda: next(clock))
    store.log_quota(conn, provider=PROVIDER, buckets=[_bucket("pro", 0.9), _bucket("flash", 0.8)])  # ts 100
    store.log_quota(conn, provider=PROVIDER, buckets=[_bucket("pro", 0.5)])  # ts 200
    store.log_quota(conn, provider=OTHER_PROVIDER, buckets=[_bucket("pro", 0.01)])  # ts 300, other provider
    store.log_quota(conn, provider=PROVIDER, buckets=[_bucket("flash", 0.7, reset_time="later")])  # ts 150

    latest = store.get_latest_quota(conn, PROVIDER)
    assert all(type(entry) is dict for entry in latest)
    assert [(e["bucket_id"], e["remaining_fraction"], e["ts"]) for e in latest] == [
        ("flash", 0.7, 150.0), ("pro", 0.5, 200.0)]
    assert latest[0]["reset_time"] == "later" and latest[0]["provider"] == PROVIDER
    assert set(latest[0]) == set(EXPECTED_COLUMNS["provider_quota"])
    assert [e["remaining_fraction"] for e in store.get_latest_quota(conn, OTHER_PROVIDER)] == [0.01]
    assert store.get_latest_quota(conn, "nobody") == []


def test_get_latest_quota_orders_by_ts_before_id(conn, monkeypatch):
    clock = iter([500.0, 400.0])  # a clock step back: the later insert carries the older ts
    monkeypatch.setattr(db, "_ts_now", lambda: next(clock))
    store.log_quota(conn, provider=PROVIDER, buckets=[_bucket("pro", 0.4)])
    store.log_quota(conn, provider=PROVIDER, buckets=[_bucket("pro", 0.6)])
    assert [e["remaining_fraction"] for e in store.get_latest_quota(conn, PROVIDER)] == [0.4]


# ── PostgreSQL code paths without a database ──


class _FakePGConn:
    """Records SQL like the _PGWrapper would receive it; returns dict rows like psycopg's dict_row."""

    def __init__(self, fetchall_rows=None):
        self.calls = []
        self.commits = 0
        self._fetchall_rows = fetchall_rows or []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return self

    def fetchone(self):
        return {"id": 42}

    def fetchall(self):
        return self._fetchall_rows

    def executescript(self, sql):
        raise AssertionError("ensure_schema must not run SQL on PostgreSQL")

    def commit(self):
        self.commits += 1


def test_postgres_paths_skip_ddl_and_use_returning_id():
    fake = _FakePGConn(fetchall_rows=[{"table_name": "provider_turns"}])
    store.ensure_schema(fake)
    assert fake.calls == []

    assert store.tables_present(fake) == {
        "provider_requests": False, "provider_turns": True, "provider_quota": False}
    sql, params = fake.calls[-1]
    assert "information_schema.tables" in sql and "current_schema()" in sql and "?" not in sql
    assert tuple(params) == store.TABLES

    assert store.log_request(fake, provider=PROVIDER, endpoint="/x") == 42
    assert store.log_turn(fake, provider=PROVIDER, session_id="s", turn_number=1) == 42
    inserts = [sql for sql, _ in fake.calls if sql.startswith("INSERT")]
    assert inserts[0].startswith("INSERT INTO provider_requests ") and inserts[0].endswith(" RETURNING id")
    assert inserts[1].startswith("INSERT INTO provider_turns ") and inserts[1].endswith(" RETURNING id")
    assert fake.commits == 2


# ── Import isolation ──


def _module_level_imports(source):
    """Modules imported when the file is executed (function bodies excluded, class bodies included)."""
    names = []
    stack = list(ast.parse(source).body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            names.append(base)
            names.extend("{}.{}".format(base, alias.name) for alias in node.names)
        stack.extend(ast.iter_child_nodes(node))
    return names


def test_store_has_no_module_level_import_of_proxy_db():
    imports = _module_level_imports(Path(store.__file__).read_text(encoding="utf-8"))
    assert imports  # the walker sees the module's own imports
    assert not [name for name in imports if "proxy" in name or name.startswith(".")], imports


def test_store_imports_with_proxy_db_blocked(monkeypatch):
    import llm_relay.proxy as proxy_pkg

    # None in sys.modules makes `import llm_relay.proxy.db` raise; removing the package
    # attribute makes `from llm_relay.proxy import db` raise too. monkeypatch restores both.
    monkeypatch.setitem(sys.modules, "llm_relay.proxy.db", None)
    if hasattr(proxy_pkg, "db"):
        monkeypatch.delattr(proxy_pkg, "db")
    spec = importlib.util.spec_from_file_location("_agy_store_import_probe", store.__file__)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    assert probe.TABLES == store.TABLES and probe.PG_DDL == store.PG_DDL
    assert sys.modules["llm_relay.proxy.db"] is None
    assert "_agy_store_import_probe" not in sys.modules


# ── Migration script argument handling (no database) ──

MIGRATION = Path(__file__).resolve().parents[2] / "docs" / "migrate-provider-tables.py"


class _ConnectBlocked(Exception):
    pass


def _load_migration(monkeypatch):
    calls = []

    def connect(*args, **kwargs):
        calls.append(args)
        raise _ConnectBlocked()

    fake = types.ModuleType("psycopg")
    fake.connect = connect
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    spec = importlib.util.spec_from_file_location("_migrate_provider_tables_probe", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, calls


def test_migration_script_uses_the_store_ddl(monkeypatch):
    module, _ = _load_migration(monkeypatch)
    # The report covers the optional provider_blobs too, else its index would always read "missing".
    assert module.PG_DDL is store.PG_DDL and module.ALL_TABLES is store.ALL_TABLES
    assert module.INDEX_NAMES == tuple(EXPECTED_INDEXES)
    assert "proxy" not in " ".join(_module_level_imports(MIGRATION.read_text(encoding="utf-8")))


@pytest.mark.parametrize("dsn", ["sqlite:///tmp/x.db", "/home/user/.llm-relay/usage.db", "mysql://u@h/db",
                                 "POSTGRESQL://u@h/db", " postgresql://u@h/db", ""])
def test_migration_script_refuses_non_postgres_urls(monkeypatch, capsys, dsn):
    module, calls = _load_migration(monkeypatch)
    assert module.main(["--pg", dsn]) == 2
    assert calls == []
    assert "must be a postgresql://" in capsys.readouterr().out


def test_migration_script_requires_pg(monkeypatch, capsys):
    module, calls = _load_migration(monkeypatch)
    with pytest.raises(SystemExit) as excinfo:
        module.main([])
    assert excinfo.value.code == 2 and calls == []
    capsys.readouterr()


@pytest.mark.parametrize("dsn", ["postgresql://u@h/db", "postgres://u@h/db"])
def test_migration_script_accepts_postgres_urls(monkeypatch, dsn):
    module, calls = _load_migration(monkeypatch)
    with pytest.raises(_ConnectBlocked):
        module.main(["--pg", dsn, "--apply"])
    assert calls == [(dsn,)]


# ── 2026-11 extension: request columns, provider_blobs, upgrading an older SQLite file ──

# Spec section 1: (SQLite type, PostgreSQL type) of each new provider_requests column.
NEW_REQUEST_COLUMN_TYPES = {
    "finish_reason": ("TEXT", "TEXT"),
    "trace_id": ("TEXT", "TEXT"),
    "response_id": ("TEXT", "TEXT"),
    "trajectory_id": ("TEXT", "TEXT"),
    "system_chars": ("INTEGER", "INTEGER"),
    "tools_count": ("INTEGER", "INTEGER"),
    "headers_ms": ("REAL", "REAL"),
    "first_thought_ms": ("REAL", "REAL"),
    "first_answer_ms": ("REAL", "REAL"),
    "response_message": ("TEXT", "JSONB"),
    "thinking_blocks": ("TEXT", "JSONB"),
    "response_extra": ("TEXT", "JSONB"),
    "system_hash": ("TEXT", "TEXT"),
    "tools_hash": ("TEXT", "TEXT"),
    "response_hash": ("TEXT", "TEXT"),
}
# Spec section 2.
BLOB_COLUMN_TYPES = [
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT", "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"),
    ("ts", "REAL NOT NULL", "TIMESTAMPTZ NOT NULL"),
    ("provider", "TEXT NOT NULL", "TEXT NOT NULL"),
    ("kind", "TEXT NOT NULL", "TEXT NOT NULL"),
    ("sha256", "TEXT NOT NULL", "TEXT NOT NULL"),
    ("size_bytes", "INTEGER NOT NULL DEFAULT 0", "INTEGER NOT NULL DEFAULT 0"),
    ("body", "TEXT", "JSONB"),
]
OLD_INDEXES = [name for name in EXPECTED_INDEXES if name != "idx_provider_blobs_key"]
SYSTEM_HASH, TOOLS_HASH, RESPONSE_HASH = "a" * 64, "b" * 64, "c" * 64

RESPONSE_MESSAGE = [
    {"type": "text", "text": "검색 결과" + NUL + " 요약"},
    {"type": "tool_use", "id": "call_1", "name": "search", "input": {"질의" + NUL: "x"}},
]
THINKING_BLOCKS = [{"type": "thinking", "thinking": "생각" + NUL + "중"}]
RESPONSE_EXTRA = {"groundingMetadata": {"webSearchQueries": ["q" + NUL]}, "_event_metadata": {"k": 1}}
NEW_VALUES = {
    "finish_reason": "STOP", "trace_id": "trace-1", "response_id": "resp-1", "trajectory_id": "traj-1",
    "system_chars": 1234, "tools_count": 17, "headers_ms": 12.5, "first_thought_ms": 80.25,
    "first_answer_ms": 410.0, "response_message": RESPONSE_MESSAGE, "thinking_blocks": THINKING_BLOCKS,
    "response_extra": RESPONSE_EXTRA, "system_hash": SYSTEM_HASH, "tools_hash": TOOLS_HASH,
    "response_hash": RESPONSE_HASH,
}


def _create_old_schema(connection):
    """The provider schema of the first release: three tables, five indexes, no new request columns."""
    for table in store.TABLES:
        defs = [(name, sql_type) for name, sql_type in store.column_defs(table, "sqlite")
                if not (table == "provider_requests" and name in NEW_REQUEST_COLUMNS)]
        connection.execute("CREATE TABLE {} ({})".format(table, ", ".join("{} {}".format(*d) for d in defs)))
    for name in OLD_INDEXES:
        table, columns = EXPECTED_INDEXES[name]
        connection.execute("CREATE INDEX {} ON {} ({})".format(name, table, ", ".join(columns)))
    connection.commit()
    assert [row[1] for row in connection.execute("PRAGMA table_info(provider_requests)")] == ORIGINAL_REQUEST_COLUMNS


def _schema_dump(connection):
    rows = connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name")
    return [tuple(row) for row in rows]


def test_new_constants_match_the_spec():
    assert store.NEW_REQUEST_COLUMNS == tuple(NEW_REQUEST_COLUMNS) == tuple(NEW_REQUEST_COLUMN_TYPES)
    assert store.OPTIONAL_TABLES == ("provider_blobs",)
    assert store.ALL_TABLES == store.TABLES + store.OPTIONAL_TABLES
    assert store.UNIQUE_INDEXES == frozenset(UNIQUE_INDEX_NAMES)
    assert ("idx_provider_blobs_key", "provider_blobs", ("provider", "kind", "sha256")) in store.INDEXES
    assert all(len(index) == 3 and isinstance(index[2], tuple) for index in store.INDEXES)  # tuple format kept


def test_column_defs_in_both_dialects():
    for table in store.ALL_TABLES:
        sqlite_defs, pg_defs = store.column_defs(table, "sqlite"), store.column_defs(table, "pg")
        assert [name for name, _ in sqlite_defs] == [name for name, _ in pg_defs] == EXPECTED_COLUMNS[table]
        # The same text the DDL is generated from: one "    name type" line per column.
        head = "CREATE TABLE IF NOT EXISTS {} (".format(table)
        sqlite_lines = next(s for s in _sqlite_statements() if s.startswith(head)).splitlines()[1:-1]
        pg_lines = next(s for s in store.PG_DDL if s.startswith(head)).splitlines()[1:-1]
        assert [line.rstrip(",") for line in sqlite_lines] == ["    {} {}".format(*d) for d in sqlite_defs]
        assert [line.rstrip(",") for line in pg_lines] == ["    {} {}".format(*d) for d in pg_defs]
    sqlite_types = dict(store.column_defs("provider_requests", "sqlite"))
    pg_types = dict(store.column_defs("provider_requests", "pg"))
    for name, (sqlite_type, pg_type) in NEW_REQUEST_COLUMN_TYPES.items():
        assert (sqlite_types[name], pg_types[name]) == (sqlite_type, pg_type), name
        for text in (sqlite_types[name], pg_types[name]):  # nullable, no DEFAULT: a catalog-only ALTER
            assert "NOT NULL" not in text and "DEFAULT" not in text
    assert store.column_defs("provider_blobs", "sqlite") == [(name, s) for name, s, _ in BLOB_COLUMN_TYPES]
    assert store.column_defs("provider_blobs", "pg") == [(name, p) for name, _, p in BLOB_COLUMN_TYPES]
    with pytest.raises(ValueError):
        store.column_defs("provider_requests", "mysql")
    with pytest.raises(KeyError):
        store.column_defs("requests", "sqlite")  # a Claude Code table is not ours


def test_pg_ddl_snapshot_of_the_new_table_columns_and_unique_index():
    assert store.PG_DDL[3] == (
        "CREATE TABLE IF NOT EXISTS provider_blobs (\n"
        "    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,\n"
        "    ts TIMESTAMPTZ NOT NULL,\n"
        "    provider TEXT NOT NULL,\n"
        "    kind TEXT NOT NULL,\n"
        "    sha256 TEXT NOT NULL,\n"
        "    size_bytes INTEGER NOT NULL DEFAULT 0,\n"
        "    body JSONB\n"
        ")"
    )
    assert store.PG_DDL[-1] == (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_blobs_key ON provider_blobs (provider, kind, sha256)")
    assert store.PG_DDL[0].endswith(
        "    error TEXT,\n    finish_reason TEXT,\n    trace_id TEXT,\n    response_id TEXT,\n"
        "    trajectory_id TEXT,\n    system_chars INTEGER,\n    tools_count INTEGER,\n    headers_ms REAL,\n"
        "    first_thought_ms REAL,\n    first_answer_ms REAL,\n    response_message JSONB,\n"
        "    thinking_blocks JSONB,\n    response_extra JSONB,\n    system_hash TEXT,\n    tools_hash TEXT,\n"
        "    response_hash TEXT\n)")
    assert [s for s in store.PG_DDL if "UNIQUE" in s] == [store.PG_DDL[-1]]


def test_tables_present_default_names_only_the_required_tables(conn, tmp_path):
    assert store.tables_present(conn) == {table: True for table in store.TABLES}  # no provider_blobs key
    assert store.tables_present(conn, store.ALL_TABLES) == {table: True for table in store.ALL_TABLES}
    assert store.tables_present(conn, store.OPTIONAL_TABLES) == {"provider_blobs": True}
    assert store.tables_present(conn, ()) == {}
    old = get_conn(tmp_path / "old.db")
    try:
        _create_old_schema(old)
        assert store.tables_present(old) == {table: True for table in store.TABLES}
        assert store.tables_present(old, store.ALL_TABLES) == {
            "provider_requests": True, "provider_turns": True, "provider_quota": True, "provider_blobs": False}
    finally:
        old.close()


def test_request_columns_present_on_sqlite(conn, tmp_path):
    assert store.request_columns_present(conn) == set(EXPECTED_COLUMNS["provider_requests"])
    empty = get_conn(tmp_path / "empty.db")
    try:
        assert store.request_columns_present(empty) == set()  # table missing
        _create_old_schema(empty)
        assert store.request_columns_present(empty) == set(ORIGINAL_REQUEST_COLUMNS)
    finally:
        empty.close()


def test_log_request_new_columns_round_trip(conn):
    rid = store.log_request(conn, provider=PROVIDER, endpoint="/v1internal:streamGenerateContent", **NEW_VALUES)
    row = dict(conn.execute("SELECT * FROM provider_requests WHERE id = ?", (rid,)).fetchone())
    for column in ("response_message", "thinking_blocks", "response_extra"):
        assert "\\u0000" not in row[column], column
        assert not _contains_nul(json.loads(row[column])), column
        assert json.loads(row[column]) == store.strip_nul(NEW_VALUES[column]), column
    assert json.loads(row["response_message"])[0]["text"] == "검색 결과" + REPLACEMENT + " 요약"
    scalars = {column: row[column] for column in NEW_REQUEST_COLUMNS if column not in (
        "response_message", "thinking_blocks", "response_extra")}
    assert scalars == {column: value for column, value in NEW_VALUES.items() if column in scalars}
    assert isinstance(row["system_chars"], int) and isinstance(row["headers_ms"], float)
    # The full column set changes nothing.
    available = store.request_columns_present(conn)
    again = store.log_request(conn, provider=PROVIDER, endpoint="/x", available_columns=available, **NEW_VALUES)
    assert dict(conn.execute("SELECT finish_reason, response_hash FROM provider_requests WHERE id = ?",
                             (again,)).fetchone()) == {"finish_reason": "STOP", "response_hash": RESPONSE_HASH}


def test_log_request_available_columns_leaves_out_missing_new_columns(tmp_path):
    connection = get_conn(tmp_path / "old.db")
    try:
        _create_old_schema(connection)
        available = store.request_columns_present(connection)
        with pytest.raises(sqlite3.OperationalError):  # None = every column, which the old table lacks
            store.log_request(connection, provider=PROVIDER, endpoint="/x", **NEW_VALUES)

        rid = store.log_request(connection, provider=PROVIDER, endpoint="/v1internal:streamGenerateContent",
                                session_id="s1", model="gemini-x", input_tokens=5, raw_usage={"promptTokenCount": 5},
                                available_columns=available, **NEW_VALUES)
        row = dict(connection.execute("SELECT * FROM provider_requests WHERE id = ?", (rid,)).fetchone())
        assert list(row) == ORIGINAL_REQUEST_COLUMNS
        assert (row["provider"], row["endpoint"], row["session_id"], row["model"], row["input_tokens"]) == (
            PROVIDER, "/v1internal:streamGenerateContent", "s1", "gemini-x", 5)
        assert json.loads(row["raw_usage"]) == {"promptTokenCount": 5}

        # A partly migrated table: the new columns it has are written, the others left out.
        connection.execute("ALTER TABLE provider_requests ADD COLUMN finish_reason TEXT")
        connection.execute("ALTER TABLE provider_requests ADD COLUMN response_extra TEXT")
        rid = store.log_request(connection, provider=PROVIDER, endpoint="/x",
                                available_columns=store.request_columns_present(connection), **NEW_VALUES)
        row = dict(connection.execute("SELECT * FROM provider_requests WHERE id = ?", (rid,)).fetchone())
        assert row["finish_reason"] == "STOP"
        assert json.loads(row["response_extra"]) == store.strip_nul(RESPONSE_EXTRA)
        assert "system_hash" not in row

        # The original columns are always written, even when the set does not name them.
        rid = store.log_request(connection, provider=PROVIDER, endpoint="/core", session_id="s3",
                                available_columns=set(), **NEW_VALUES)
        row = dict(connection.execute("SELECT * FROM provider_requests WHERE id = ?", (rid,)).fetchone())
        assert (row["endpoint"], row["session_id"], row["finish_reason"], row["response_extra"]) == (
            "/core", "s3", None, None)
        assert connection.execute("SELECT count(*) FROM provider_requests").fetchone()[0] == 3
    finally:
        connection.close()


def test_log_blob_is_idempotent_and_keeps_the_first_row(conn, monkeypatch):
    clock = iter([100.0, 200.0, 300.0, 400.0])
    monkeypatch.setattr(db, "_ts_now", lambda: next(clock))
    body = {"role": "user", "parts": [{"text": "시스템 프롬프트" + NUL}]}
    sha = "d" * 64
    assert store.log_blob(conn, provider=PROVIDER, kind="system_instruction", sha256=sha, size_bytes=120,
                          body=body) is True
    assert store.log_blob(conn, provider=PROVIDER, kind="system_instruction", sha256=sha, size_bytes=7,
                          body={"other": 1}) is False
    rows = [dict(r) for r in conn.execute("SELECT * FROM provider_blobs").fetchall()]
    assert len(rows) == 1
    assert json.loads(rows[0].pop("body")) == {"role": "user", "parts": [{"text": "시스템 프롬프트" + REPLACEMENT}]}
    assert rows[0] == {"id": 1, "ts": 100.0, "provider": PROVIDER, "kind": "system_instruction", "sha256": sha,
                       "size_bytes": 120}
    # The key is (provider, kind, sha256): the same hash under another kind or provider is another row.
    assert store.log_blob(conn, provider=PROVIDER, kind="tools", sha256=sha, size_bytes=1, body=[1]) is True
    assert store.log_blob(conn, provider=OTHER_PROVIDER, kind="system_instruction", sha256=sha, size_bytes=1,
                          body=[1]) is True
    assert conn.execute("SELECT count(*) FROM provider_blobs").fetchone()[0] == 3


def test_ensure_schema_upgrades_an_old_sqlite_file_and_keeps_its_rows(tmp_path):
    path = tmp_path / "old.db"
    old = get_conn(path)
    _create_old_schema(old)
    old.execute("INSERT INTO provider_requests (ts, provider, endpoint, session_id, model, input_tokens, raw_usage)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)", (10.0, PROVIDER, "/old", "s-old", "m-old", 42, '{"a": 1}'))
    old.execute("INSERT INTO provider_turns (ts, provider, session_id, turn_number, request_messages)"
                " VALUES (?, ?, ?, ?, ?)", (11.0, PROVIDER, "s-old", 1, '[{"role": "user"}]'))
    old.execute("INSERT INTO provider_quota (ts, provider, bucket_id, remaining_fraction) VALUES (?, ?, ?, ?)",
                (12.0, PROVIDER, "b1", 0.5))
    old.commit()
    before = {table: [dict(r) for r in old.execute("SELECT * FROM {}".format(table))] for table in store.TABLES}
    old.close()

    connection = get_conn(path)
    try:
        store.ensure_schema(connection)
        assert store.tables_present(connection, store.ALL_TABLES) == {table: True for table in store.ALL_TABLES}
        info = {row[1]: row for row in connection.execute("PRAGMA table_info(provider_requests)")}
        assert list(info) == EXPECTED_COLUMNS["provider_requests"]  # appended in the spec order
        for name, (sqlite_type, _) in NEW_REQUEST_COLUMN_TYPES.items():
            assert (info[name][2], info[name][3], info[name][4]) == (sqlite_type, 0, None), name  # nullable, no DEFAULT
        assert [row[1] for row in connection.execute("PRAGMA table_info(provider_blobs)")] == EXPECTED_COLUMNS[
            "provider_blobs"]
        blob_indexes = {row[1]: row[2] for row in connection.execute("PRAGMA index_list(provider_blobs)")}
        assert blob_indexes == {"idx_provider_blobs_key": 1}  # unique

        # Old rows are kept as they were; the new columns read NULL.
        rows = [dict(r) for r in connection.execute("SELECT * FROM provider_requests")]
        assert [{k: r[k] for k in ORIGINAL_REQUEST_COLUMNS} for r in rows] == before["provider_requests"]
        assert all(rows[0][column] is None for column in NEW_REQUEST_COLUMNS)
        for table in ("provider_turns", "provider_quota"):
            assert [dict(r) for r in connection.execute("SELECT * FROM {}".format(table))] == before[table]

        # The upgraded file takes new rows in every column, and blobs.
        rid = store.log_request(connection, provider=PROVIDER, endpoint="/new", **NEW_VALUES)
        assert rid == 2
        assert store.log_blob(connection, provider=PROVIDER, kind="tools", sha256=TOOLS_HASH, size_bytes=3,
                              body=[1]) is True

        # A second run changes nothing.
        dump = _schema_dump(connection)
        store.ensure_schema(connection)
        assert _schema_dump(connection) == dump
        assert connection.execute("SELECT count(*) FROM provider_requests").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM provider_blobs").fetchone()[0] == 1
    finally:
        connection.close()


def test_ensure_schema_twice_on_a_fresh_file_is_idempotent(tmp_path):
    connection = get_conn(tmp_path / "fresh.db")
    try:
        store.ensure_schema(connection)
        dump = _schema_dump(connection)
        assert store.log_blob(connection, provider=PROVIDER, kind="experiments", sha256="e" * 64, size_bytes=2,
                              body={}) is True
        store.log_request(connection, provider=PROVIDER, endpoint="/x", **NEW_VALUES)
        store.ensure_schema(connection)
        assert _schema_dump(connection) == dump
        assert connection.execute("SELECT count(*) FROM provider_blobs").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM provider_requests").fetchone()[0] == 1
        assert [row[1] for row in connection.execute("PRAGMA table_info(provider_requests)")] == EXPECTED_COLUMNS[
            "provider_requests"]
    finally:
        connection.close()


class _ConflictPGConn(_FakePGConn):
    """ON CONFLICT DO NOTHING hit: RETURNING yields no row."""

    def fetchone(self):
        return None


def test_postgres_paths_for_columns_blobs_and_limited_inserts():
    fake = _FakePGConn(fetchall_rows=[{"column_name": "id"}, {"column_name": "finish_reason"}])
    assert store.request_columns_present(fake) == {"id", "finish_reason"}
    sql, params = fake.calls[-1]
    assert "information_schema.columns" in sql and "current_schema()" in sql and "'provider_requests'" in sql
    assert "?" not in sql and "%" not in sql and tuple(params) == ()

    assert store.log_blob(fake, provider=PROVIDER, kind="tools", sha256=TOOLS_HASH, size_bytes=3, body=[1]) is True
    sql, params = fake.calls[-1]
    assert sql == ("INSERT INTO provider_blobs (ts, provider, kind, sha256, size_bytes, body) VALUES (?, ?, ?, ?, ?, ?)"
                   " ON CONFLICT (provider, kind, sha256) DO NOTHING RETURNING id")
    assert tuple(params[1:5]) == (PROVIDER, "tools", TOOLS_HASH, 3)
    conflict = _ConflictPGConn()
    assert store.log_blob(conflict, provider=PROVIDER, kind="tools", sha256=TOOLS_HASH, size_bytes=3,
                          body=[1]) is False
    assert conflict.commits == 1

    limited = _FakePGConn()
    store.log_request(limited, provider=PROVIDER, endpoint="/x", available_columns=set(ORIGINAL_REQUEST_COLUMNS) | {
        "finish_reason", "system_hash"}, **NEW_VALUES)
    sql, params = limited.calls[-1]
    columns = sql.split("(", 1)[1].split(")", 1)[0].split(", ")
    assert columns == ORIGINAL_REQUEST_COLUMNS[1:] + ["finish_reason", "system_hash"]
    assert len(params) == len(columns) and sql.endswith(" RETURNING id")
