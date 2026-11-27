"""docs/migrate-provider-columns.py: the DDL plan (pure) and the report / --apply paths on a fake connection.

No database: ``plan()`` takes the observed schema as plain sets, and ``main()``
runs against a stand-in psycopg module whose connection keeps the schema in
memory and records every statement.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

from llm_relay.antigravity import store

SCRIPT = Path(__file__).resolve().parents[2] / "docs" / "migrate-provider-columns.py"

# Spec section 1: the 15 provider_requests columns and their PostgreSQL types, in table order.
NEW_COLUMNS_PG = [
    ("finish_reason", "TEXT"), ("trace_id", "TEXT"), ("response_id", "TEXT"), ("trajectory_id", "TEXT"),
    ("system_chars", "INTEGER"), ("tools_count", "INTEGER"), ("headers_ms", "REAL"),
    ("first_thought_ms", "REAL"), ("first_answer_ms", "REAL"), ("response_message", "JSONB"),
    ("thinking_blocks", "JSONB"), ("response_extra", "JSONB"), ("system_hash", "TEXT"), ("tools_hash", "TEXT"),
    ("response_hash", "TEXT"),
]


class _LockNotAvailable(Exception):
    pass


def _load(monkeypatch, connect=None):
    fake = types.ModuleType("psycopg")
    fake.connect = connect or (lambda *a, **k: pytest.fail("plan tests must not connect"))
    fake.errors = types.SimpleNamespace(LockNotAvailable=_LockNotAvailable)
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    spec = importlib.util.spec_from_file_location("_migrate_provider_columns_probe", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mig(monkeypatch):
    return _load(monkeypatch)


def _full():
    existing = {table: {name for name, _ in store.column_defs(table, "pg")} for table in store.ALL_TABLES}
    return existing, {index[0] for index in store.INDEXES}


def _production_now():
    """Spec background: the three core tables before the 2026-11 columns, no provider_blobs."""
    existing, indexes = _full()
    del existing["provider_blobs"]
    existing["provider_requests"] -= set(store.NEW_REQUEST_COLUMNS)
    indexes.discard("idx_provider_blobs_key")
    return existing, indexes


def test_complete_schema_plans_nothing(mig):
    assert mig.plan(*_full()) == []


def test_missing_request_columns_become_15_alters_with_the_spec_types(mig):
    existing, indexes = _full()
    existing["provider_requests"] -= set(store.NEW_REQUEST_COLUMNS)
    statements = mig.plan(existing, indexes)
    assert statements == ["ALTER TABLE provider_requests ADD COLUMN IF NOT EXISTS {} {}".format(name, pg_type)
                          for name, pg_type in NEW_COLUMNS_PG]


def test_some_missing_columns_only_those(mig):
    existing, indexes = _full()
    existing["provider_requests"] -= {"trace_id", "response_hash"}
    assert mig.plan(existing, indexes) == [
        "ALTER TABLE provider_requests ADD COLUMN IF NOT EXISTS trace_id TEXT",
        "ALTER TABLE provider_requests ADD COLUMN IF NOT EXISTS response_hash TEXT",
    ]


def test_missing_blob_table_and_index_become_create_table_and_unique_index(mig):
    existing, indexes = _full()
    del existing["provider_blobs"]
    indexes.discard("idx_provider_blobs_key")
    statements = mig.plan(existing, indexes)
    assert len(statements) == 2
    assert statements[0].startswith("CREATE TABLE IF NOT EXISTS provider_blobs (") and statements[0] in store.PG_DDL
    assert statements[1] == ("CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_blobs_key"
                             " ON provider_blobs (provider, kind, sha256)")


def test_no_tables_at_all_is_create_only(mig):
    statements = mig.plan({}, set())
    assert statements == store.PG_DDL  # store order: tables first, then indexes
    assert not any(s.startswith("ALTER") for s in statements)


def test_order_is_tables_then_columns_then_indexes(mig):
    statements = mig.plan(*_production_now())
    kinds = ["table" if s.startswith("CREATE TABLE") else "alter" if s.startswith("ALTER") else "index"
             for s in statements]
    assert kinds == ["table"] + ["alter"] * 15 + ["index"]
    assert statements[-1].startswith("CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_blobs_key ")


@pytest.mark.parametrize("table, column", [
    ("provider_requests", "input_tokens"),   # INTEGER DEFAULT 0
    ("provider_requests", "endpoint"),       # TEXT NOT NULL
    ("provider_turns", "storage_mode"),      # TEXT NOT NULL DEFAULT 'delta'
    ("provider_blobs", "size_bytes"),        # INTEGER NOT NULL DEFAULT 0
])
def test_not_null_or_default_column_on_existing_table_is_refused(mig, table, column):
    existing, indexes = _full()
    existing[table].discard(column)
    with pytest.raises(ValueError, match=r"NOT NULL or DEFAULT"):
        mig.plan(existing, indexes)


def test_unsafe_type_check_reads_the_store_type(mig, monkeypatch):
    original = store.column_defs

    def with_default(table, dialect):
        return [(name, "TEXT DEFAULT 'x'" if name == "trace_id" else pg_type)
                for name, pg_type in original(table, dialect)]

    monkeypatch.setattr(store, "column_defs", with_default)
    existing, indexes = _full()
    existing["provider_requests"].discard("trace_id")
    with pytest.raises(ValueError):
        mig.plan(existing, indexes)


def test_non_provider_table_name_is_refused(mig, monkeypatch):
    existing, indexes = _full()
    with pytest.raises(ValueError, match="provider_"):
        mig.plan(dict(existing, requests={"id"}), indexes)
    monkeypatch.setattr(mig, "ALL_TABLES", store.ALL_TABLES + ("conversation_turns",))
    with pytest.raises(ValueError, match="conversation_turns"):
        mig.plan(existing, indexes)


def test_plan_names_only_provider_tables(mig):
    for statement in mig.plan({}, set()) + mig.plan(*_production_now()):
        named = re.findall(r"(?:TABLE IF NOT EXISTS|ALTER TABLE|ON) (\w+)", statement)
        assert named and all(name.startswith("provider_") for name in named)


def test_script_imports_without_psycopg(monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg", None)  # import psycopg -> ImportError
    spec = importlib.util.spec_from_file_location("_migrate_provider_columns_nodriver", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.psycopg is None and module.plan(*_full()) == []
    assert module.main(["--pg", "postgresql://u@h/db"]) == 1


# ── main() on a fake connection ──


class _FakeConn:
    """Holds a schema like read_schema sees it and applies the DDL plan() emits."""

    def __init__(self, existing, indexes, rows=None, lock_on=None):
        self.existing = {table: set(columns) for table, columns in existing.items()}
        self.indexes = set(indexes)
        self.rows = rows or {}
        self.lock_on = lock_on
        self.ddl = []
        self.settings = []
        self._result = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        if "information_schema.tables" in sql:
            self._result = [(t,) for t in params if t in self.existing]
        elif "information_schema.columns" in sql:
            self._result = [(t, c) for t in params if t in self.existing for c in sorted(self.existing[t])]
        elif "pg_indexes" in sql:
            self._result = [(name,) for name in params if name in self.indexes]
        elif sql.startswith("SELECT count(*) FROM "):
            self._result = [(self.rows.get(sql.split()[-1], 0),)]
        elif "set_config" in sql:
            self.settings.append(params)
            self._result = [("",)]
        else:
            if self.lock_on and self.lock_on in sql:
                raise _LockNotAvailable()
            self.ddl.append(sql)
            self._apply(sql)
            self._result = []
        return self

    def _apply(self, sql):
        match = re.match(r"CREATE TABLE IF NOT EXISTS (\w+)", sql)
        if match:
            self.existing.setdefault(match.group(1), {n for n, _ in store.column_defs(match.group(1), "pg")})
            return
        match = re.match(r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+) ", sql)
        if match:
            self.existing[match.group(1)].add(match.group(2))
            return
        match = re.match(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS (\w+)", sql)
        assert match, sql
        self.indexes.add(match.group(1))

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None


def _run(monkeypatch, conn, argv):
    calls = []

    def connect(dsn, **kwargs):
        calls.append((dsn, kwargs))
        return conn

    module = _load(monkeypatch, connect)
    code = module.main(argv)
    assert calls == [("postgresql://u@h/db", {"autocommit": True})]
    return code


def test_main_report_only_prints_the_plan_and_changes_nothing(monkeypatch, capsys):
    existing, indexes = _production_now()
    conn = _FakeConn(existing, indexes, rows={"provider_requests": 1290})
    assert _run(monkeypatch, conn, ["--pg", "postgresql://u@h/db"]) == 0
    out = capsys.readouterr().out
    assert conn.ddl == [] and conn.settings == []
    assert "table provider_requests: present, 1290 rows, missing columns: finish_reason, trace_id" in out
    assert "table provider_blobs: missing" in out and "index idx_provider_blobs_key: missing" in out
    assert "ALTER TABLE provider_requests ADD COLUMN IF NOT EXISTS response_hash TEXT;" in out
    assert "Report only" in out


def test_main_apply_runs_the_plan_then_reports_complete(monkeypatch, capsys):
    conn = _FakeConn(*_production_now())
    assert _run(monkeypatch, conn, ["--pg", "postgresql://u@h/db", "--apply", "--lock-timeout", "2s"]) == 0
    assert conn.settings == [("2s",)]
    assert conn.ddl == _load(monkeypatch).plan(*_production_now())
    capsys.readouterr()
    # A second run finds nothing to do and runs no DDL.
    again = _FakeConn(conn.existing, conn.indexes)
    assert _run(monkeypatch, again, ["--pg", "postgresql://u@h/db", "--apply"]) == 0
    assert again.ddl == [] and "Nothing to do." in capsys.readouterr().out


def test_main_lock_timeout_stops_and_keeps_earlier_statements(monkeypatch, capsys):
    conn = _FakeConn(*_production_now(), lock_on="ADD COLUMN IF NOT EXISTS headers_ms")
    assert _run(monkeypatch, conn, ["--pg", "postgresql://u@h/db", "--apply"]) == 1
    assert conn.ddl[0].startswith("CREATE TABLE IF NOT EXISTS provider_blobs")
    assert conn.ddl[-1].endswith("tools_count INTEGER")
    assert "did not get its lock" in capsys.readouterr().out


def test_main_refuses_an_unsafe_plan_with_exit_2(monkeypatch, capsys):
    existing, indexes = _full()
    existing["provider_turns"].discard("storage_mode")
    conn = _FakeConn(existing, indexes)
    assert _run(monkeypatch, conn, ["--pg", "postgresql://u@h/db", "--apply"]) == 2
    assert conn.ddl == [] and "NOT NULL or DEFAULT" in capsys.readouterr().out


@pytest.mark.parametrize("dsn", ["sqlite:///tmp/x.db", "/home/user/.llm-relay/usage.db", "mysql://u@h/db", ""])
def test_main_refuses_non_postgres_urls(monkeypatch, capsys, dsn):
    module = _load(monkeypatch)
    assert module.main(["--pg", dsn]) == 2
    assert "must be a postgresql://" in capsys.readouterr().out
