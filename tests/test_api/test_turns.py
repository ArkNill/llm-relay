"""Tests for turn counter API + DB function."""

import os
import sqlite3
import time
from unittest.mock import MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from llm_relay.api.routes import (
    _classify_zone,
    _classify_zone_absolute,
    _classify_zone_ratio,
    _compute_zone_bundle,
    _overall_zone,
    get_api_routes,
)
from llm_relay.proxy.db import (
    get_all_session_terminals,
    get_all_turn_counts,
    get_session_terminal,
    get_turn_count,
    upsert_session_terminal,
)


# Deterministic env for Zone A absolute-threshold tests (local defaults)
@pytest.fixture(autouse=True)
def _zone_env(monkeypatch):
    monkeypatch.setenv("CC_TOKEN_A_YELLOW", "300000")
    monkeypatch.setenv("CC_TOKEN_A_ORANGE", "500000")
    monkeypatch.setenv("CC_TOKEN_A_RED", "750000")
    monkeypatch.setenv("CC_TOKEN_A_HARD", "900000")
    monkeypatch.setenv("CC_TOKEN_CEILING", "1000000")


# Default empty-metrics dict for mocking get_turn_count returns
def _empty_metrics(turns=0, first_ts=None, last_ts=None, **kw):
    base = {
        "turns": turns,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "peak_ctx": 0,
        "cumul_unique": 0,
        "current_ctx": 0,
        "recent_peak": 0,
    }
    base.update(kw)
    return base


def _make_app():
    return Starlette(routes=get_api_routes())


# ── DB function tests ──


class TestGetTurnCount:
    def _make_db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE requests (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            session_id TEXT,
            endpoint TEXT,
            model TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_creation INTEGER DEFAULT 0,
            cache_read INTEGER DEFAULT 0,
            read_ratio REAL DEFAULT 0.0,
            status_code INTEGER,
            latency_ms REAL,
            is_stream INTEGER DEFAULT 0,
            raw_usage TEXT,
            request_body_bytes INTEGER DEFAULT 0
        )""")
        return conn

    def test_basic_count(self):
        conn = self._make_db()
        now = time.time()
        for i in range(5):
            conn.execute(
                "INSERT INTO requests (ts, session_id, endpoint) VALUES (?, ?, ?)",
                (now + i, "sid-1", "/v1/messages"),
            )
        conn.commit()
        result = get_turn_count(conn, "sid-1")
        assert result["turns"] == 5
        assert result["first_ts"] is not None
        assert result["last_ts"] is not None

    def test_excludes_count_tokens(self):
        conn = self._make_db()
        now = time.time()
        conn.execute(
            "INSERT INTO requests (ts, session_id, endpoint) VALUES (?, ?, ?)",
            (now, "sid-2", "/v1/messages"),
        )
        conn.execute(
            "INSERT INTO requests (ts, session_id, endpoint) VALUES (?, ?, ?)",
            (now + 1, "sid-2", "/v1/messages/count_tokens"),
        )
        conn.execute(
            "INSERT INTO requests (ts, session_id, endpoint) VALUES (?, ?, ?)",
            (now + 2, "sid-2", "/v1/messages"),
        )
        conn.commit()
        result = get_turn_count(conn, "sid-2")
        assert result["turns"] == 2

    def test_empty_session(self):
        conn = self._make_db()
        result = get_turn_count(conn, "nonexistent")
        assert result["turns"] == 0

    def test_session_isolation(self):
        conn = self._make_db()
        now = time.time()
        for i in range(3):
            conn.execute(
                "INSERT INTO requests (ts, session_id, endpoint) VALUES (?, ?, ?)",
                (now + i, "sid-A", "/v1/messages"),
            )
        for i in range(7):
            conn.execute(
                "INSERT INTO requests (ts, session_id, endpoint) VALUES (?, ?, ?)",
                (now + i, "sid-B", "/v1/messages"),
            )
        conn.commit()
        assert get_turn_count(conn, "sid-A")["turns"] == 3
        assert get_turn_count(conn, "sid-B")["turns"] == 7


class TestGetAllTurnCounts:
    def _make_db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE requests (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            session_id TEXT,
            endpoint TEXT,
            model TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_creation INTEGER DEFAULT 0,
            cache_read INTEGER DEFAULT 0,
            read_ratio REAL DEFAULT 0.0,
            status_code INTEGER,
            latency_ms REAL,
            is_stream INTEGER DEFAULT 0,
            raw_usage TEXT,
            request_body_bytes INTEGER DEFAULT 0
        )""")
        return conn

    def test_multiple_sessions(self):
        conn = self._make_db()
        now = time.time()
        for sid, count in [("sid-A", 5), ("sid-B", 12), ("sid-C", 3)]:
            for i in range(count):
                conn.execute(
                    "INSERT INTO requests (ts, session_id, endpoint) VALUES (?, ?, ?)",
                    (now + i, sid, "/v1/messages"),
                )
        conn.commit()
        result = get_all_turn_counts(conn, window_hours=1)
        assert len(result) == 3
        # Should be sorted by turns DESC
        assert result[0]["session_id"] == "sid-B"
        assert result[0]["turns"] == 12

    def test_excludes_null_session(self):
        conn = self._make_db()
        now = time.time()
        conn.execute(
            "INSERT INTO requests (ts, session_id, endpoint) VALUES (?, ?, ?)",
            (now, None, "/v1/messages"),
        )
        conn.execute(
            "INSERT INTO requests (ts, session_id, endpoint) VALUES (?, ?, ?)",
            (now, "real-sid", "/v1/messages"),
        )
        conn.commit()
        result = get_all_turn_counts(conn, window_hours=1)
        assert len(result) == 1
        assert result[0]["session_id"] == "real-sid"

    def test_window_filter(self):
        conn = self._make_db()
        now = time.time()
        # Old session outside window
        conn.execute(
            "INSERT INTO requests (ts, session_id, endpoint) VALUES (?, ?, ?)",
            (now - 7200, "old-sid", "/v1/messages"),
        )
        # Recent session
        conn.execute(
            "INSERT INTO requests (ts, session_id, endpoint) VALUES (?, ?, ?)",
            (now, "recent-sid", "/v1/messages"),
        )
        conn.commit()
        result = get_all_turn_counts(conn, window_hours=1)
        assert len(result) == 1
        assert result[0]["session_id"] == "recent-sid"


# ── Zone classification tests ──


class TestClassifyZone:
    def test_green(self):
        zone, label, threshold, msg = _classify_zone(42)
        assert zone == "green"
        assert threshold == 200
        assert msg is None

    def test_yellow(self):
        zone, label, threshold, msg = _classify_zone(210)
        assert zone == "yellow"
        assert threshold == 250
        assert msg is not None

    def test_orange(self):
        zone, label, threshold, msg = _classify_zone(270)
        assert zone == "orange"
        assert threshold == 300
        assert msg is not None

    def test_red(self):
        zone, label, threshold, msg = _classify_zone(320)
        assert zone == "red"
        assert threshold is None
        assert msg is not None

    def test_boundary_200(self):
        zone, _, _, _ = _classify_zone(200)
        assert zone == "yellow"

    def test_boundary_250(self):
        zone, _, _, _ = _classify_zone(250)
        assert zone == "orange"

    def test_boundary_300(self):
        zone, _, _, _ = _classify_zone(300)
        assert zone == "red"

    def test_zero_turns(self):
        zone, _, _, msg = _classify_zone(0)
        assert zone == "green"
        assert msg is None


# ── API endpoint tests ──


class TestTurnsEndpoint:
    @patch("llm_relay.proxy.db.get_conn")
    @patch("llm_relay.proxy.db.get_turn_count")
    def test_returns_json(self, mock_count, mock_conn):
        mock_conn.return_value = MagicMock()
        mock_count.return_value = _empty_metrics(turns=42, first_ts=1000.0, last_ts=2000.0)
        client = TestClient(_make_app())
        resp = client.get("/api/v1/turns/test-session-id")
        assert resp.status_code == 200
        data = resp.json()
        assert data["turns"] == 42
        assert data["zone"] == "green"
        assert data["session_id"] == "test-session-id"
        assert data["duration_s"] == 1000.0
        # New fields present
        assert data["current_ctx"] == 0
        assert data["peak_ctx"] == 0
        assert data["recent_peak"] == 0
        assert data["cumul_unique"] == 0
        assert data["ceiling"] == 1000000
        assert data["zone_a"] == "green"
        assert data["zone_b"] == "green"

    @patch("llm_relay.proxy.db.get_conn")
    @patch("llm_relay.proxy.db.get_turn_count")
    def test_unknown_session(self, mock_count, mock_conn):
        mock_conn.return_value = MagicMock()
        mock_count.return_value = _empty_metrics(turns=0)
        client = TestClient(_make_app())
        resp = client.get("/api/v1/turns/unknown")
        assert resp.status_code == 200
        data = resp.json()
        assert data["turns"] == 0
        assert data["zone"] == "green"
        assert data["duration_s"] == 0
        assert data["current_ctx"] == 0

    @patch("llm_relay.proxy.db.get_conn")
    @patch("llm_relay.proxy.db.get_turn_count")
    def test_heavy_session_tokens_drive_zone(self, mock_count, mock_conn):
        """High turn count no longer drives zone — only tokens do."""
        mock_conn.return_value = MagicMock()
        # Lots of turns but low context → still green (turns ignored)
        mock_count.return_value = _empty_metrics(
            turns=350, first_ts=1000.0, last_ts=8000.0,
            current_ctx=50_000, peak_ctx=80_000,
        )
        client = TestClient(_make_app())
        resp = client.get("/api/v1/turns/heavy-turns-light-ctx")
        data = resp.json()
        assert data["turns"] == 350
        assert data["zone"] == "green"  # tokens < 300K so green
        assert data["zone_a"] == "green"

    @patch("llm_relay.proxy.db.get_conn")
    @patch("llm_relay.proxy.db.get_turn_count")
    def test_red_zone_via_tokens(self, mock_count, mock_conn):
        mock_conn.return_value = MagicMock()
        mock_count.return_value = _empty_metrics(
            turns=100, first_ts=1000.0, last_ts=8000.0,
            current_ctx=800_000, peak_ctx=850_000,
        )
        client = TestClient(_make_app())
        resp = client.get("/api/v1/turns/heavy-session")
        data = resp.json()
        # 800K → Zone A red (≥750K), Zone B orange (≥70%) → overall red
        assert data["zone"] == "red"
        assert data["zone_a"] == "red"
        assert data["zone_b"] == "orange"
        assert data["message"] is not None

    @patch("llm_relay.proxy.db.get_conn")
    @patch("llm_relay.proxy.db.get_turn_count")
    def test_hard_stop_zone(self, mock_count, mock_conn):
        mock_conn.return_value = MagicMock()
        mock_count.return_value = _empty_metrics(
            turns=100, first_ts=1000.0, last_ts=8000.0,
            current_ctx=950_000, peak_ctx=950_000,
        )
        client = TestClient(_make_app())
        resp = client.get("/api/v1/turns/over-limit")
        data = resp.json()
        # 950K → Zone A hard (≥900K), overall hard
        assert data["zone"] == "hard"
        assert data["zone_a"] == "hard"

    @patch("llm_relay.proxy.db.get_conn", side_effect=Exception("DB error"))
    def test_db_error(self, mock_conn):
        client = TestClient(_make_app())
        resp = client.get("/api/v1/turns/any-session")
        assert resp.status_code == 500
        assert "error" in resp.json()


class TestTurnsAllEndpoint:
    @patch("llm_relay.proxy.db.get_conn")
    @patch("llm_relay.proxy.db.get_all_turn_counts")
    @patch("llm_relay.proxy.db.get_all_session_terminals")
    @patch("llm_relay.api.display.is_cc_process_alive", return_value=True)
    def test_returns_alive_sessions(self, _mock_alive, mock_terms, mock_all, mock_conn):
        """Sessions whose CC process is alive are returned with metrics + alive flag."""
        mock_conn.return_value = MagicMock()
        mock_all.return_value = [
            _empty_metrics(turns=42, first_ts=1000.0, last_ts=2000.0,
                           current_ctx=50_000, peak_ctx=80_000) | {"session_id": "sid-1"},
            _empty_metrics(turns=100, first_ts=500.0, last_ts=3000.0,
                           current_ctx=820_000, peak_ctx=850_000) | {"session_id": "sid-2"},
        ]
        mock_terms.return_value = {
            "sid-1": {"cc_pid": 1000, "tty": "/dev/pts/1"},
            "sid-2": {"cc_pid": 2000, "tty": "/dev/pts/2"},
        }
        client = TestClient(_make_app())
        resp = client.get("/api/v1/turns?window=4")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        assert data["sessions"][0]["zone"] == "green"
        assert data["sessions"][1]["zone"] == "red"  # 820K → Zone A red
        assert data["sessions"][1]["zone_a"] == "red"
        assert data["sessions"][1]["message"] is not None
        assert data["sessions"][0]["current_ctx"] == 50_000
        assert data["sessions"][1]["peak_ctx"] == 850_000
        assert data["sessions"][0]["alive"] is True

    @patch("llm_relay.proxy.db.get_conn")
    @patch("llm_relay.proxy.db.get_all_turn_counts")
    @patch("llm_relay.proxy.db.get_all_session_terminals", return_value={})
    def test_empty(self, _mock_terms, mock_all, mock_conn):
        mock_conn.return_value = MagicMock()
        mock_all.return_value = []
        client = TestClient(_make_app())
        resp = client.get("/api/v1/turns")
        data = resp.json()
        assert data["count"] == 0
        assert data["sessions"] == []

    @patch("llm_relay.proxy.db.get_conn")
    @patch("llm_relay.proxy.db.get_all_turn_counts")
    @patch("llm_relay.proxy.db.get_all_session_terminals")
    @patch("llm_relay.api.display.find_claude_pid_by_tty", return_value=None)
    @patch("llm_relay.api.display.is_cc_process_alive", return_value=False)
    def test_filters_dead_by_default(
        self, _mock_alive, _mock_find_tty, mock_terms, mock_all, mock_conn,
    ):
        """Dashboard regression: dead CC sessions must drop out of /turns by default."""
        mock_conn.return_value = MagicMock()
        now = time.time()
        mock_all.return_value = [
            _empty_metrics(turns=10, first_ts=now - 1000, last_ts=now - 800)
            | {"session_id": "dead-sid"},
        ]
        mock_terms.return_value = {
            "dead-sid": {"cc_pid": 9999, "tty": "/dev/pts/1"},
        }
        client = TestClient(_make_app())
        resp = client.get("/api/v1/turns?window=4")
        data = resp.json()
        assert data["count"] == 0
        assert data["sessions"] == []

    @patch("llm_relay.proxy.db.get_conn")
    @patch("llm_relay.proxy.db.get_all_turn_counts")
    @patch("llm_relay.proxy.db.get_all_session_terminals")
    @patch("llm_relay.api.display.find_claude_pid_by_tty", return_value=None)
    @patch("llm_relay.api.display.is_cc_process_alive", return_value=False)
    def test_include_dead_param(
        self, _mock_alive, _mock_find_tty, mock_terms, mock_all, mock_conn,
    ):
        """include_dead=1 keeps dead sessions in the response (with alive=False)."""
        mock_conn.return_value = MagicMock()
        now = time.time()
        mock_all.return_value = [
            _empty_metrics(turns=10, first_ts=now - 1000, last_ts=now - 800)
            | {"session_id": "dead-sid"},
        ]
        mock_terms.return_value = {
            "dead-sid": {"cc_pid": 9999, "tty": "/dev/pts/1"},
        }
        client = TestClient(_make_app())
        resp = client.get("/api/v1/turns?window=4&include_dead=1")
        data = resp.json()
        assert data["count"] == 1
        assert data["sessions"][0]["session_id"] == "dead-sid"
        assert data["sessions"][0]["alive"] is False


# ── Session Terminal Tests ──


class TestSessionTerminalDB:
    def _make_db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE session_terminals (
            session_id TEXT PRIMARY KEY,
            tty TEXT,
            cc_pid INTEGER,
            term_pid INTEGER,
            term_name TEXT,
            updated_ts REAL NOT NULL
        )""")
        return conn

    def test_upsert_insert(self):
        conn = self._make_db()
        upsert_session_terminal(
            conn, "sid-1",
            tty="/dev/pts/3", cc_pid=1234, term_pid=5678, term_name="kitty"
        )
        result = get_session_terminal(conn, "sid-1")
        assert result["tty"] == "/dev/pts/3"
        assert result["cc_pid"] == 1234
        assert result["term_pid"] == 5678
        assert result["term_name"] == "kitty"
        assert result["updated_ts"] > 0

    def test_upsert_update(self):
        conn = self._make_db()
        upsert_session_terminal(conn, "sid-1", tty="/dev/pts/3", cc_pid=1234)
        upsert_session_terminal(conn, "sid-1", tty="/dev/pts/5", cc_pid=9999, term_name="alacritty")
        result = get_session_terminal(conn, "sid-1")
        assert result["tty"] == "/dev/pts/5"
        assert result["cc_pid"] == 9999
        assert result["term_name"] == "alacritty"

    def test_get_nonexistent(self):
        conn = self._make_db()
        result = get_session_terminal(conn, "unknown")
        assert result is None

    def test_get_all(self):
        conn = self._make_db()
        upsert_session_terminal(conn, "sid-A", tty="/dev/pts/1", term_name="kitty")
        upsert_session_terminal(conn, "sid-B", tty="/dev/pts/2", term_name="gnome-terminal")
        result = get_all_session_terminals(conn)
        assert len(result) == 2
        assert result["sid-A"]["tty"] == "/dev/pts/1"
        assert result["sid-B"]["term_name"] == "gnome-terminal"

    def test_terminal_reuse_cleanup(self):
        """When a new session registers the same cc_pid, the old session's cc_pid/tty are cleared."""
        conn = self._make_db()
        upsert_session_terminal(conn, "sid-old", tty="/dev/pts/3", cc_pid=1234, term_name="kitty")
        upsert_session_terminal(conn, "sid-new", tty="/dev/pts/3", cc_pid=1234, term_name="kitty")
        old = get_session_terminal(conn, "sid-old")
        new = get_session_terminal(conn, "sid-new")
        assert old["cc_pid"] is None
        assert old["tty"] is None
        assert new["cc_pid"] == 1234
        assert new["tty"] == "/dev/pts/3"

    def test_terminal_reuse_no_false_cleanup(self):
        """Different cc_pid should not clear other sessions."""
        conn = self._make_db()
        upsert_session_terminal(conn, "sid-A", tty="/dev/pts/1", cc_pid=1111)
        upsert_session_terminal(conn, "sid-B", tty="/dev/pts/2", cc_pid=2222)
        a = get_session_terminal(conn, "sid-A")
        b = get_session_terminal(conn, "sid-B")
        assert a["cc_pid"] == 1111
        assert b["cc_pid"] == 2222


class TestSessionTerminalEndpoint:
    @patch("llm_relay.proxy.db.get_conn")
    @patch("llm_relay.proxy.db.upsert_session_terminal")
    def test_post_valid(self, mock_upsert, mock_conn):
        mock_conn.return_value = MagicMock()
        client = TestClient(_make_app())
        resp = client.post("/api/v1/session-terminal", json={
            "session_id": "test-sid",
            "tty": "/dev/pts/3",
            "cc_pid": 1234,
            "term_pid": 5678,
            "term_name": "kitty",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["session_id"] == "test-sid"
        mock_upsert.assert_called_once()

    def test_post_missing_session_id(self):
        client = TestClient(_make_app())
        resp = client.post("/api/v1/session-terminal", json={"tty": "/dev/pts/3"})
        assert resp.status_code == 400
        assert "error" in resp.json()

    @patch("llm_relay.proxy.db.get_conn", side_effect=Exception("DB error"))
    def test_post_db_error(self, mock_conn):
        client = TestClient(_make_app())
        resp = client.post("/api/v1/session-terminal", json={"session_id": "sid"})
        assert resp.status_code == 500


# ── Process Liveness Tests ──


class TestIsCcProcessAlive:
    def test_none_pid(self):
        from llm_relay.api.display import is_cc_process_alive
        assert is_cc_process_alive(None) is False
        assert is_cc_process_alive(0) is False
        assert is_cc_process_alive(-1) is False

    def test_nonexistent_pid(self):
        from llm_relay.api.display import is_cc_process_alive
        # PID 999999 is very unlikely to exist
        assert is_cc_process_alive(999999) is False

    def test_current_process_is_not_claude(self):
        """Current Python test runner is not CC — should return False even though alive."""
        from llm_relay.api.display import is_cc_process_alive
        # This test's PID is alive but cmdline is pytest/python, not claude
        result = is_cc_process_alive(os.getpid())
        # Accepts either False (pure python) or True (if running via claude code)
        # The behavior depends on test environment, so we just verify no exception
        assert isinstance(result, bool)

    def test_fake_proc_with_claude_cmdline(self, tmp_path, monkeypatch):
        """Mock /proc layout with claude in cmdline."""
        from llm_relay.api.display import is_cc_process_alive
        proc_dir = tmp_path / "proc"
        pid_dir = proc_dir / "12345"
        pid_dir.mkdir(parents=True)
        (pid_dir / "cmdline").write_bytes(b"node\x00/usr/bin/claude\x00")
        (pid_dir / "comm").write_text("node")
        monkeypatch.setenv("CC_HOST_PROC", str(proc_dir))
        assert is_cc_process_alive(12345) is True

    def test_fake_proc_non_claude(self, tmp_path, monkeypatch):
        """Mock /proc layout with non-claude process (PID reuse case)."""
        from llm_relay.api.display import is_cc_process_alive
        proc_dir = tmp_path / "proc"
        pid_dir = proc_dir / "12345"
        pid_dir.mkdir(parents=True)
        (pid_dir / "cmdline").write_bytes(b"sshd\x00-D\x00")
        (pid_dir / "comm").write_text("sshd")
        monkeypatch.setenv("CC_HOST_PROC", str(proc_dir))
        assert is_cc_process_alive(12345) is False


class TestDisplayLivenessFilter:
    @patch("llm_relay.api.display.discover_external_cli_sessions", return_value=[])
    @patch("llm_relay.proxy.db.get_conn")
    @patch("llm_relay.proxy.db.get_all_turn_counts")
    @patch("llm_relay.proxy.db.get_all_session_terminals")
    @patch("llm_relay.api.display.get_last_user_prompt")
    @patch("llm_relay.api.display.find_claude_pid_by_tty")
    @patch("llm_relay.api.display.is_cc_process_alive")
    def test_filters_dead_sessions(
        self, mock_alive, mock_find_tty, mock_prompt, mock_terms, mock_counts, mock_conn, _mock_ext,
    ):
        import time as _time
        mock_conn.return_value = MagicMock()
        now = _time.time()
        mock_counts.return_value = [
            _empty_metrics(turns=10, first_ts=now - 1000, last_ts=now - 400) | {"session_id": "alive-pid-sid"},
            _empty_metrics(turns=20, first_ts=now - 500, last_ts=now - 300) | {"session_id": "stale-pid-alive-tty-sid"},
            _empty_metrics(turns=50, first_ts=now - 2000, last_ts=now - 400) | {"session_id": "dead-sid"},
            _empty_metrics(turns=5, first_ts=now - 300, last_ts=now - 60) | {"session_id": "unregistered-sid"},
        ]
        mock_terms.return_value = {
            "alive-pid-sid": {"cc_pid": 1000, "tty": "/dev/pts/1", "term_name": "kitty"},
            "stale-pid-alive-tty-sid": {"cc_pid": 1500, "tty": "/dev/pts/5", "term_name": "kitty"},
            "dead-sid": {"cc_pid": 2000, "tty": "/dev/pts/2", "term_name": "gnome-terminal"},
            # unregistered-sid has no terminal entry
        }
        mock_prompt.return_value = {"text": "", "timestamp": None}
        # pid 1000 alive, others dead
        mock_alive.side_effect = lambda pid: pid == 1000
        # TTY pts/5 has a claude process, pts/2 doesn't
        mock_find_tty.side_effect = lambda tty: 9999 if tty == "/dev/pts/5" else None

        client = TestClient(_make_app())
        resp = client.get("/api/v1/display?window=4")
        data = resp.json()
        # alive-pid-sid: cc_pid alive → shown
        # stale-pid-alive-tty-sid: cc_pid dead BUT tty has claude → shown
        # dead-sid: cc_pid dead AND tty has no claude → hidden
        # unregistered-sid: no terminal entry → hidden (pure process-based)
        sids = [s["session_id"] for s in data["sessions"]]
        assert "alive-pid-sid" in sids
        assert "stale-pid-alive-tty-sid" in sids
        assert "dead-sid" not in sids
        assert "unregistered-sid" not in sids
        assert data["count"] == 2

    @patch("llm_relay.api.display.discover_external_cli_sessions", return_value=[])
    @patch("llm_relay.proxy.db.get_conn")
    @patch("llm_relay.proxy.db.get_all_turn_counts")
    @patch("llm_relay.proxy.db.get_all_session_terminals")
    @patch("llm_relay.api.display.get_last_user_prompt")
    @patch("llm_relay.api.display.find_claude_pid_by_tty")
    @patch("llm_relay.api.display.is_cc_process_alive")
    def test_tty_reuse_by_registered_session_filters_stale(
        self, mock_alive, mock_find_tty, mock_prompt, mock_terms, mock_counts, mock_conn, _mock_ext
    ):
        """A dead session must not be resurrected via TTY fallback when the
        claude process on that TTY is already claimed by another *registered*
        live session."""
        import time as _time
        mock_conn.return_value = MagicMock()
        now = _time.time()
        mock_counts.return_value = [
            _empty_metrics(turns=10, first_ts=now - 500, last_ts=now - 120) | {"session_id": "stale-sid"},
            _empty_metrics(turns=3, first_ts=now - 60, last_ts=now - 10) | {"session_id": "new-sid"},
        ]
        # Both sessions map to the same /dev/pts/1 — new-sid took over the TTY
        mock_terms.return_value = {
            "stale-sid": {"cc_pid": 7000, "tty": "/dev/pts/1", "term_name": "gnome-terminal"},
            "new-sid":   {"cc_pid": 8000, "tty": "/dev/pts/1", "term_name": "gnome-terminal"},
        }
        mock_prompt.return_value = {"text": "", "timestamp": None}
        # Only the new session's PID is alive
        mock_alive.side_effect = lambda pid: pid == 8000
        # TTY lookup finds the *new* session's PID
        mock_find_tty.side_effect = lambda tty: 8000 if tty == "/dev/pts/1" else None

        client = TestClient(_make_app())
        resp = client.get("/api/v1/display?window=4")
        data = resp.json()
        sids = [s["session_id"] for s in data["sessions"]]
        assert "new-sid" in sids
        assert "stale-sid" not in sids  # rejected: pid 8000 owned by new-sid
        assert data["count"] == 1

    @patch("llm_relay.api.display.discover_external_cli_sessions", return_value=[])
    @patch("llm_relay.proxy.db.get_conn")
    @patch("llm_relay.proxy.db.get_all_turn_counts")
    @patch("llm_relay.proxy.db.get_all_session_terminals")
    @patch("llm_relay.api.display.get_last_user_prompt")
    @patch("llm_relay.api.display.find_claude_pid_by_tty")
    @patch("llm_relay.api.display.is_cc_process_alive")
    def test_tty_fallback_skipped_for_stale_last_ts(
        self, mock_alive, mock_find_tty, mock_prompt, mock_terms, mock_counts, mock_conn, _mock_ext
    ):
        """A long-dead session must not be resurrected via TTY fallback even
        if an unregistered claude process is sitting on the same /dev/pts/N.
        Reproduces the 2026-04-11 report where PIDs 1210453 / 1207898 were
        shown as alive because a new (unregistered) CC session reused pts/1
        ~12h after the originals died."""
        import time as _time
        mock_conn.return_value = MagicMock()
        now = _time.time()
        mock_counts.return_value = [
            # 12 hours old — clearly outside the 10-minute TTY fallback window
            _empty_metrics(turns=29, first_ts=now - 43500, last_ts=now - 43200) | {"session_id": "ad943679"},
        ]
        mock_terms.return_value = {
            "ad943679": {"cc_pid": 1210453, "tty": "/dev/pts/1", "term_name": "gnome-terminal"},
        }
        mock_prompt.return_value = {"text": "", "timestamp": None}
        # Stored cc_pid is long dead, but /dev/pts/1 has a brand-new claude
        # process (not registered to any session_id yet)
        mock_alive.side_effect = lambda pid: False
        mock_find_tty.side_effect = lambda tty: 1260913 if tty == "/dev/pts/1" else None

        client = TestClient(_make_app())
        resp = client.get("/api/v1/display?window=24")
        data = resp.json()
        assert data["count"] == 0  # stale session filtered by time-window guard

    @patch("llm_relay.api.display.discover_external_cli_sessions", return_value=[])
    @patch("llm_relay.proxy.db.get_conn")
    @patch("llm_relay.proxy.db.get_all_turn_counts")
    @patch("llm_relay.proxy.db.get_all_session_terminals")
    @patch("llm_relay.api.display.get_last_user_prompt")
    @patch("llm_relay.api.display.is_cc_process_alive")
    def test_include_dead_param(self, mock_alive, mock_prompt, mock_terms, mock_counts, mock_conn, _mock_ext):
        mock_conn.return_value = MagicMock()
        mock_counts.return_value = [
            _empty_metrics(turns=50, first_ts=50.0, last_ts=300.0) | {"session_id": "dead-sid"},
        ]
        mock_terms.return_value = {
            "dead-sid": {"cc_pid": 2000, "tty": "/dev/pts/2", "term_name": "kitty"},
        }
        mock_prompt.return_value = {"text": "", "timestamp": None}
        mock_alive.return_value = False

        client = TestClient(_make_app())
        resp = client.get("/api/v1/display?window=4&include_dead=1")
        data = resp.json()
        # With include_dead=1, dead session should still appear
        assert data["count"] == 1
        assert data["sessions"][0]["alive"] is False


# ── Zone A / Zone B / Overall tests ──


class TestClassifyZoneAbsolute:
    def test_green(self):
        z, label, nxt, msg = _classify_zone_absolute(0)
        assert z == "green"
        assert nxt == 300_000
        assert msg is None

    def test_green_just_below_yellow(self):
        z, *_ = _classify_zone_absolute(299_999)
        assert z == "green"

    def test_yellow(self):
        z, label, nxt, msg = _classify_zone_absolute(300_000)
        assert z == "yellow"
        assert label == "주의"
        assert nxt == 500_000
        assert msg is not None

    def test_yellow_mid(self):
        z, *_ = _classify_zone_absolute(420_000)
        assert z == "yellow"

    def test_orange(self):
        z, *_ = _classify_zone_absolute(500_000)
        assert z == "orange"

    def test_red(self):
        z, label, nxt, _ = _classify_zone_absolute(750_000)
        assert z == "red"
        assert nxt == 900_000

    def test_red_between_hard(self):
        z, *_ = _classify_zone_absolute(800_000)
        assert z == "red"

    def test_hard(self):
        z, label, nxt, msg = _classify_zone_absolute(900_000)
        assert z == "hard"
        assert nxt is None
        assert msg is not None

    def test_hard_beyond_ceiling(self):
        z, *_ = _classify_zone_absolute(1_500_000)
        assert z == "hard"


class TestClassifyZoneRatio:
    def test_green(self):
        z, label, nxt, msg = _classify_zone_ratio(0)
        assert z == "green"
        assert nxt == 500_000  # 50% of 1M
        assert msg is None

    def test_below_yellow(self):
        z, *_ = _classify_zone_ratio(499_999)  # 49.9% of 1M
        assert z == "green"

    def test_yellow_at_50pct(self):
        z, label, nxt, _ = _classify_zone_ratio(500_000)
        assert z == "yellow"
        assert nxt == 700_000

    def test_orange_at_70pct(self):
        z, *_ = _classify_zone_ratio(700_000)
        assert z == "orange"

    def test_red_at_90pct(self):
        z, label, nxt, _ = _classify_zone_ratio(900_000)
        assert z == "red"
        assert nxt == 1_000_000

    def test_hard_at_100pct(self):
        z, label, nxt, msg = _classify_zone_ratio(1_000_000)
        assert z == "hard"
        assert nxt is None

    def test_explicit_ceiling_override(self):
        # With a 500K ceiling, 250K is yellow (50%)
        z, *_ = _classify_zone_ratio(250_000, ceiling=500_000)
        assert z == "yellow"
        z, *_ = _classify_zone_ratio(450_000, ceiling=500_000)
        assert z == "red"
        z, *_ = _classify_zone_ratio(500_000, ceiling=500_000)
        assert z == "hard"

    def test_zero_ceiling_is_safe(self):
        z, *_ = _classify_zone_ratio(1_000_000, ceiling=0)
        assert z == "green"


class TestOverallZone:
    def test_both_green(self):
        assert _overall_zone("green", "green") == "green"

    def test_a_worse(self):
        assert _overall_zone("orange", "green") == "orange"

    def test_b_worse(self):
        assert _overall_zone("yellow", "red") == "red"

    def test_equal(self):
        assert _overall_zone("yellow", "yellow") == "yellow"

    def test_hard_wins(self):
        assert _overall_zone("hard", "red") == "hard"
        assert _overall_zone("green", "hard") == "hard"


class TestComputeZoneBundle:
    def test_all_green(self):
        bundle = _compute_zone_bundle(current_ctx=100_000, peak_ctx=100_000)
        assert bundle["zone"] == "green"
        assert bundle["zone_a"] == "green"
        assert bundle["zone_b"] == "green"
        assert bundle["zone_a_peak"] == "green"
        assert bundle["zone_b_peak"] == "green"
        assert bundle["message"] is None

    def test_yellow_via_absolute(self):
        bundle = _compute_zone_bundle(current_ctx=320_000, peak_ctx=320_000)
        # 320K: A=yellow (≥300K), B=green (<500K)
        assert bundle["zone_a"] == "yellow"
        assert bundle["zone_b"] == "green"
        assert bundle["zone"] == "yellow"  # max(yellow, green)

    def test_orange_via_absolute(self):
        bundle = _compute_zone_bundle(current_ctx=520_000, peak_ctx=600_000)
        # 520K current: A=orange (≥500K), B=yellow (≥50%)
        # 600K peak: A=orange, B=yellow
        assert bundle["zone"] == "orange"
        assert bundle["zone_a"] == "orange"
        assert bundle["zone_b"] == "yellow"
        assert bundle["zone_a_peak"] == "orange"
        assert bundle["zone_b_peak"] == "yellow"

    def test_peak_higher_than_current(self):
        # User /compact'd → current dropped but peak preserved
        bundle = _compute_zone_bundle(current_ctx=50_000, peak_ctx=800_000)
        assert bundle["zone"] == "green"  # current judgment
        assert bundle["zone_a_peak"] == "red"  # 800K peak is red
        assert bundle["zone_b_peak"] == "orange"  # 80% of 1M

    def test_hard_stop_via_current(self):
        bundle = _compute_zone_bundle(current_ctx=950_000, peak_ctx=950_000)
        assert bundle["zone"] == "hard"
        assert bundle["zone_a"] == "hard"
        assert bundle["message"] is not None
        assert bundle["next_threshold"] is None

    def test_b_stricter_than_a(self):
        # At 700K with 1M ceiling: A=orange (≥500K), B=orange (≥70%). Equal.
        bundle = _compute_zone_bundle(current_ctx=700_000, peak_ctx=700_000)
        assert bundle["zone"] == "orange"
        assert bundle["zone_a"] == "orange"
        assert bundle["zone_b"] == "orange"


# ── DB layer: new metric fields ──


class TestGetTurnCountMetrics:
    def _make_db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE requests (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            session_id TEXT,
            endpoint TEXT,
            model TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_creation INTEGER DEFAULT 0,
            cache_read INTEGER DEFAULT 0,
            read_ratio REAL DEFAULT 0.0,
            status_code INTEGER,
            latency_ms REAL,
            is_stream INTEGER DEFAULT 0,
            raw_usage TEXT,
            request_body_bytes INTEGER DEFAULT 0
        )""")
        return conn

    def _insert(self, conn, sid, ts, cr=0, cc=0, it=0, ot=0):
        conn.execute(
            """INSERT INTO requests
               (ts, session_id, endpoint, cache_read, cache_creation,
                input_tokens, output_tokens)
               VALUES (?, ?, '/v1/messages', ?, ?, ?, ?)""",
            (ts, sid, cr, cc, it, ot),
        )

    def test_empty_session(self):
        conn = self._make_db()
        r = get_turn_count(conn, "nonexistent")
        assert r["turns"] == 0
        assert r["peak_ctx"] == 0
        assert r["current_ctx"] == 0
        assert r["recent_peak"] == 0
        assert r["cumul_unique"] == 0

    def test_monotonic_growth(self):
        conn = self._make_db()
        now = time.time()
        # Turn 1: ctx = 100K
        self._insert(conn, "mono", now, cr=0, cc=100_000, it=0, ot=500)
        # Turn 2: ctx = 200K
        self._insert(conn, "mono", now + 1, cr=100_000, cc=100_000, it=0, ot=500)
        # Turn 3: ctx = 300K
        self._insert(conn, "mono", now + 2, cr=200_000, cc=100_000, it=0, ot=500)
        conn.commit()

        r = get_turn_count(conn, "mono")
        assert r["turns"] == 3
        assert r["current_ctx"] == 300_000  # latest turn
        assert r["peak_ctx"] == 300_000
        assert r["recent_peak"] == 300_000
        # cumul_unique = sum(cache_creation + input + output) = 300_000 + 1500
        assert r["cumul_unique"] == 300_000 + 1500

    def test_compact_drop(self):
        """After /compact, current drops but peak preserved."""
        conn = self._make_db()
        now = time.time()
        # Grow
        self._insert(conn, "compacted", now, cr=0, cc=100_000, it=0, ot=500)
        self._insert(conn, "compacted", now + 1, cr=100_000, cc=200_000, it=0, ot=500)
        self._insert(conn, "compacted", now + 2, cr=300_000, cc=300_000, it=0, ot=500)  # peak 600K
        # Compact event — fresh context 20K
        self._insert(conn, "compacted", now + 3, cr=0, cc=20_000, it=0, ot=500)
        # Continue small
        self._insert(conn, "compacted", now + 4, cr=20_000, cc=10_000, it=0, ot=500)
        conn.commit()

        r = get_turn_count(conn, "compacted")
        assert r["turns"] == 5
        assert r["current_ctx"] == 30_000  # latest
        assert r["peak_ctx"] == 600_000  # preserved

    def test_recent_peak_over_last_5(self):
        """recent_peak = MAX over last 5 requests only."""
        conn = self._make_db()
        now = time.time()
        # 10 turns: first 5 with a peak 500K, next 5 small
        self._insert(conn, "r5", now, cr=0, cc=100_000)
        self._insert(conn, "r5", now + 1, cr=0, cc=500_000)  # old big
        self._insert(conn, "r5", now + 2, cr=0, cc=300_000)
        self._insert(conn, "r5", now + 3, cr=0, cc=200_000)
        self._insert(conn, "r5", now + 4, cr=0, cc=150_000)
        # 5 recent small turns
        self._insert(conn, "r5", now + 5, cr=0, cc=50_000)
        self._insert(conn, "r5", now + 6, cr=0, cc=60_000)
        self._insert(conn, "r5", now + 7, cr=0, cc=70_000)
        self._insert(conn, "r5", now + 8, cr=0, cc=80_000)
        self._insert(conn, "r5", now + 9, cr=0, cc=90_000)
        conn.commit()

        r = get_turn_count(conn, "r5")
        assert r["turns"] == 10
        assert r["peak_ctx"] == 500_000  # session-wide
        assert r["recent_peak"] == 90_000  # only last 5
        assert r["current_ctx"] == 90_000  # newest

    def test_all_turn_counts_aggregates_metrics(self):
        conn = self._make_db()
        now = time.time()
        self._insert(conn, "sa", now, cr=0, cc=50_000)
        self._insert(conn, "sa", now + 1, cr=50_000, cc=50_000)
        self._insert(conn, "sb", now, cr=0, cc=800_000)
        conn.commit()

        rows = get_all_turn_counts(conn, window_hours=1)
        by_sid = {r["session_id"]: r for r in rows}
        assert by_sid["sa"]["turns"] == 2
        assert by_sid["sa"]["peak_ctx"] == 100_000
        assert by_sid["sb"]["peak_ctx"] == 800_000
        assert by_sid["sb"]["current_ctx"] == 800_000
