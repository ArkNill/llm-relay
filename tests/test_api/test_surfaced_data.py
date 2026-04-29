"""Tests for surfaced DB data: quota, cache hit rate, error rate, TTL tier."""

import json
import sqlite3
import time
from unittest.mock import MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from llm_relay.api.routes import get_api_routes
from llm_relay.proxy.db import (
    get_error_stats,
    get_latest_quota,
    get_session_cache_stats,
    get_ttl_tier,
    log_request,
)


def _make_app():
    return Starlette(routes=get_api_routes())


def _make_db():
    """Create an in-memory SQLite DB with the requests table including all columns."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE requests (
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
        request_body_bytes INTEGER DEFAULT 0,
        ratelimit_headers TEXT,
        estimated_cost_usd REAL DEFAULT 0.0,
        message_count INTEGER DEFAULT 0,
        ephemeral_1h_tokens INTEGER DEFAULT 0,
        ephemeral_5m_tokens INTEGER DEFAULT 0
    )""")
    return conn


# ── Quota tests (db.get_latest_quota) ──


class TestGetLatestQuota:
    def test_no_data(self):
        conn = _make_db()
        result = get_latest_quota(conn)
        assert result is None

    def test_no_ratelimit_headers(self):
        conn = _make_db()
        conn.execute(
            "INSERT INTO requests (ts, session_id, endpoint, status_code) VALUES (?, ?, ?, ?)",
            (time.time(), "sid-1", "/v1/messages", 200),
        )
        conn.commit()
        result = get_latest_quota(conn)
        assert result is None

    def test_extracts_quota_fields(self):
        conn = _make_db()
        headers = {
            "anthropic-ratelimit-unified-5h-utilization": "42.5",
            "anthropic-ratelimit-unified-7d-utilization": "18.3",
            "anthropic-ratelimit-unified-status": "active",
            "anthropic-ratelimit-unified-overage-status": "none",
            "x-ratelimit-limit-requests": "1000",
        }
        conn.execute(
            "INSERT INTO requests (ts, session_id, endpoint, ratelimit_headers) VALUES (?, ?, ?, ?)",
            (time.time(), "sid-1", "/v1/messages", json.dumps(headers)),
        )
        conn.commit()
        result = get_latest_quota(conn)
        assert result is not None
        assert result["q5h_utilization"] == "42.5"
        assert result["q7d_utilization"] == "18.3"
        assert result["unified_status"] == "active"
        assert result["overage_status"] == "none"

    def test_returns_latest(self):
        conn = _make_db()
        old_headers = {"anthropic-ratelimit-unified-5h-utilization": "10.0"}
        new_headers = {"anthropic-ratelimit-unified-5h-utilization": "99.0"}
        now = time.time()
        conn.execute(
            "INSERT INTO requests (ts, ratelimit_headers) VALUES (?, ?)",
            (now - 60, json.dumps(old_headers)),
        )
        conn.execute(
            "INSERT INTO requests (ts, ratelimit_headers) VALUES (?, ?)",
            (now, json.dumps(new_headers)),
        )
        conn.commit()
        result = get_latest_quota(conn)
        assert result["q5h_utilization"] == "99.0"

    def test_case_insensitive_header_keys(self):
        conn = _make_db()
        headers = {
            "Anthropic-Ratelimit-Unified-5h-Utilization": "55.0",
        }
        conn.execute(
            "INSERT INTO requests (ts, ratelimit_headers) VALUES (?, ?)",
            (time.time(), json.dumps(headers)),
        )
        conn.commit()
        result = get_latest_quota(conn)
        assert result["q5h_utilization"] == "55.0"


# ── Error stats tests (db.get_error_stats) ──


class TestGetErrorStats:
    def test_no_data(self):
        conn = _make_db()
        result = get_error_stats(conn)
        assert result["total"] == 0
        assert result["error_rate_pct"] == 0.0

    def test_all_success(self):
        conn = _make_db()
        now = time.time()
        for i in range(5):
            conn.execute(
                "INSERT INTO requests (ts, status_code) VALUES (?, ?)",
                (now, 200),
            )
        conn.commit()
        result = get_error_stats(conn)
        assert result["total"] == 5
        assert result["success_2xx"] == 5
        assert result["client_4xx"] == 0
        assert result["server_5xx"] == 0
        assert result["error_rate_pct"] == 0.0

    def test_mixed_errors(self):
        conn = _make_db()
        now = time.time()
        codes = [200, 200, 200, 429, 500, 200, 400, 200]
        for code in codes:
            conn.execute(
                "INSERT INTO requests (ts, status_code) VALUES (?, ?)",
                (now, code),
            )
        conn.commit()
        result = get_error_stats(conn)
        assert result["total"] == 8
        assert result["success_2xx"] == 5
        assert result["client_4xx"] == 2  # 429 + 400
        assert result["server_5xx"] == 1
        assert result["rate_limited"] == 1
        assert result["error_rate_pct"] == 37.5  # 3/8 * 100

    def test_session_filter(self):
        conn = _make_db()
        now = time.time()
        conn.execute(
            "INSERT INTO requests (ts, session_id, status_code) VALUES (?, ?, ?)",
            (now, "sid-1", 200),
        )
        conn.execute(
            "INSERT INTO requests (ts, session_id, status_code) VALUES (?, ?, ?)",
            (now, "sid-1", 500),
        )
        conn.execute(
            "INSERT INTO requests (ts, session_id, status_code) VALUES (?, ?, ?)",
            (now, "sid-2", 200),
        )
        conn.commit()
        result = get_error_stats(conn, session_id="sid-1")
        assert result["total"] == 2
        assert result["server_5xx"] == 1

    def test_window_filter(self):
        conn = _make_db()
        now = time.time()
        conn.execute(
            "INSERT INTO requests (ts, status_code) VALUES (?, ?)",
            (now - 36000, 500),  # 10h ago, outside 8h window
        )
        conn.execute(
            "INSERT INTO requests (ts, status_code) VALUES (?, ?)",
            (now, 200),
        )
        conn.commit()
        result = get_error_stats(conn, window_hours=8)
        assert result["total"] == 1
        assert result["success_2xx"] == 1


# ── Cache stats tests (db.get_session_cache_stats) ──


class TestGetSessionCacheStats:
    def test_no_data(self):
        conn = _make_db()
        result = get_session_cache_stats(conn)
        assert result["cache_hit_rate"] == 0.0
        assert result["request_count"] == 0

    def test_perfect_cache(self):
        conn = _make_db()
        now = time.time()
        conn.execute(
            "INSERT INTO requests (ts, endpoint, cache_read, cache_creation, input_tokens) "
            "VALUES (?, ?, ?, ?, ?)",
            (now, "/v1/messages", 1000, 0, 100),
        )
        conn.commit()
        result = get_session_cache_stats(conn)
        assert result["cache_hit_rate"] == 100.0
        assert result["total_cache_read"] == 1000

    def test_mixed_cache(self):
        conn = _make_db()
        now = time.time()
        conn.execute(
            "INSERT INTO requests (ts, endpoint, cache_read, cache_creation, input_tokens) "
            "VALUES (?, ?, ?, ?, ?)",
            (now, "/v1/messages", 700, 300, 200),
        )
        conn.commit()
        result = get_session_cache_stats(conn)
        assert result["cache_hit_rate"] == 70.0  # 700 / (700+300)

    def test_no_cache_activity(self):
        conn = _make_db()
        now = time.time()
        conn.execute(
            "INSERT INTO requests (ts, endpoint, cache_read, cache_creation, input_tokens) "
            "VALUES (?, ?, ?, ?, ?)",
            (now, "/v1/messages", 0, 0, 500),
        )
        conn.commit()
        result = get_session_cache_stats(conn)
        assert result["cache_hit_rate"] == 0.0

    def test_session_filter(self):
        conn = _make_db()
        now = time.time()
        conn.execute(
            "INSERT INTO requests (ts, session_id, endpoint, cache_read, cache_creation, input_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now, "sid-1", "/v1/messages", 800, 200, 100),
        )
        conn.execute(
            "INSERT INTO requests (ts, session_id, endpoint, cache_read, cache_creation, input_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now, "sid-2", "/v1/messages", 0, 1000, 500),
        )
        conn.commit()
        result = get_session_cache_stats(conn, session_id="sid-1")
        assert result["cache_hit_rate"] == 80.0  # 800 / (800+200)

    def test_excludes_non_messages(self):
        conn = _make_db()
        now = time.time()
        conn.execute(
            "INSERT INTO requests (ts, endpoint, cache_read, cache_creation, input_tokens) "
            "VALUES (?, ?, ?, ?, ?)",
            (now, "/v1/messages/count_tokens", 1000, 0, 100),
        )
        conn.commit()
        result = get_session_cache_stats(conn)
        assert result["request_count"] == 0


# ── TTL tier tests (db.get_ttl_tier) ──


class TestGetTTLTier:
    def test_no_data(self):
        conn = _make_db()
        result = get_ttl_tier(conn)
        assert result["tier"] == "unknown"
        assert result["request_count"] == 0

    def test_1h_tier(self):
        conn = _make_db()
        now = time.time()
        conn.execute(
            "INSERT INTO requests (ts, ephemeral_1h_tokens, ephemeral_5m_tokens) VALUES (?, ?, ?)",
            (now, 5000, 0),
        )
        conn.commit()
        result = get_ttl_tier(conn)
        assert result["tier"] == "1h"
        assert result["ephemeral_1h_tokens"] == 5000

    def test_5m_tier(self):
        conn = _make_db()
        now = time.time()
        conn.execute(
            "INSERT INTO requests (ts, ephemeral_1h_tokens, ephemeral_5m_tokens) VALUES (?, ?, ?)",
            (now, 0, 3000),
        )
        conn.commit()
        result = get_ttl_tier(conn)
        assert result["tier"] == "5m"
        assert result["ephemeral_5m_tokens"] == 3000

    def test_mixed_tier(self):
        conn = _make_db()
        now = time.time()
        conn.execute(
            "INSERT INTO requests (ts, ephemeral_1h_tokens, ephemeral_5m_tokens) VALUES (?, ?, ?)",
            (now, 2000, 1000),
        )
        conn.commit()
        result = get_ttl_tier(conn)
        assert result["tier"] == "mixed"

    def test_session_filter(self):
        conn = _make_db()
        now = time.time()
        conn.execute(
            "INSERT INTO requests (ts, session_id, ephemeral_1h_tokens, ephemeral_5m_tokens) VALUES (?, ?, ?, ?)",
            (now, "sid-1", 5000, 0),
        )
        conn.execute(
            "INSERT INTO requests (ts, session_id, ephemeral_1h_tokens, ephemeral_5m_tokens) VALUES (?, ?, ?, ?)",
            (now, "sid-2", 0, 3000),
        )
        conn.commit()
        result = get_ttl_tier(conn, session_id="sid-1")
        assert result["tier"] == "1h"
        result2 = get_ttl_tier(conn, session_id="sid-2")
        assert result2["tier"] == "5m"


# ── log_request ephemeral columns test ──


class TestLogRequestEphemeral:
    def test_ephemeral_stored(self):
        conn = _make_db()
        log_request(
            conn,
            session_id="sid-1",
            endpoint="/v1/messages",
            status_code=200,
            ephemeral_1h_tokens=4000,
            ephemeral_5m_tokens=1000,
        )
        row = conn.execute("SELECT ephemeral_1h_tokens, ephemeral_5m_tokens FROM requests").fetchone()
        assert row["ephemeral_1h_tokens"] == 4000
        assert row["ephemeral_5m_tokens"] == 1000

    def test_ephemeral_defaults_zero(self):
        conn = _make_db()
        log_request(conn, session_id="sid-1", endpoint="/v1/messages", status_code=200)
        row = conn.execute("SELECT ephemeral_1h_tokens, ephemeral_5m_tokens FROM requests").fetchone()
        assert row["ephemeral_1h_tokens"] == 0
        assert row["ephemeral_5m_tokens"] == 0


# ── API endpoint tests ──


@pytest.fixture(autouse=True)
def _zone_env(monkeypatch):
    monkeypatch.setenv("LLM_TOKEN_A_YELLOW", "300000")
    monkeypatch.setenv("LLM_TOKEN_A_ORANGE", "500000")
    monkeypatch.setenv("LLM_TOKEN_A_RED", "750000")
    monkeypatch.setenv("LLM_TOKEN_A_HARD", "900000")
    monkeypatch.setenv("LLM_TOKEN_CEILING", "1000000")


class TestQuotaEndpoint:
    def test_no_data(self):
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = None
        with patch("llm_relay.proxy.db.get_conn", return_value=mock_conn):
            client = TestClient(_make_app())
            resp = client.get("/api/v1/quota")
            assert resp.status_code == 200
            data = resp.json()
            assert data["available"] is False

    def test_with_data(self):
        mock_conn = MagicMock()
        mock_row = MagicMock()
        mock_row.__getitem__ = lambda s, k: {
            "ratelimit_headers": json.dumps({
                "anthropic-ratelimit-unified-5h-utilization": "30.0",
                "anthropic-ratelimit-unified-7d-utilization": "15.0",
            }),
            "ts": 1234567890.0,
        }[k]
        mock_conn.execute.return_value.fetchone.return_value = mock_row
        with patch("llm_relay.proxy.db.get_conn", return_value=mock_conn):
            client = TestClient(_make_app())
            resp = client.get("/api/v1/quota")
            assert resp.status_code == 200
            data = resp.json()
            assert data["available"] is True
            assert data["q5h_utilization"] == "30.0"


class TestErrorsEndpoint:
    def test_no_data(self):
        mock_conn = MagicMock()
        mock_row = MagicMock()
        _err_data = {"total": 0, "success_2xx": 0, "client_4xx": 0, "server_5xx": 0, "rate_limited": 0}
        mock_row.__getitem__ = lambda s, k: _err_data[k]
        mock_conn.execute.return_value.fetchone.return_value = mock_row
        with patch("llm_relay.proxy.db.get_conn", return_value=mock_conn):
            client = TestClient(_make_app())
            resp = client.get("/api/v1/errors")
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 0


class TestCacheEndpoint:
    def test_no_data(self):
        mock_conn = MagicMock()
        mock_row = MagicMock()
        _cache_data = {"total_cache_read": 0, "total_cache_creation": 0, "total_input_tokens": 0, "request_count": 0}
        mock_row.__getitem__ = lambda s, k: _cache_data[k]
        mock_conn.execute.return_value.fetchone.return_value = mock_row
        with patch("llm_relay.proxy.db.get_conn", return_value=mock_conn):
            client = TestClient(_make_app())
            resp = client.get("/api/v1/cache")
            assert resp.status_code == 200
            data = resp.json()
            assert data["cache_hit_rate"] == 0.0


class TestTTLEndpoint:
    def test_unknown(self):
        mock_conn = MagicMock()
        mock_row = MagicMock()
        mock_row.__getitem__ = lambda s, k: {"total_1h": 0, "total_5m": 0, "request_count": 0}[k]
        mock_conn.execute.return_value.fetchone.return_value = mock_row
        with patch("llm_relay.proxy.db.get_conn", return_value=mock_conn):
            client = TestClient(_make_app())
            resp = client.get("/api/v1/ttl")
            assert resp.status_code == 200
            data = resp.json()
            assert data["tier"] == "unknown"
