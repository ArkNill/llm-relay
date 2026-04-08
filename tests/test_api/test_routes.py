"""Tests for api/routes.py — HTTP API endpoints via Starlette TestClient."""

import json
from unittest.mock import patch, MagicMock

import pytest
from starlette.testclient import TestClient
from starlette.applications import Starlette

from llm_relay.api.routes import get_api_routes
from llm_relay.orch.models import AuthMethod, CLIStatus


def _make_app():
    return Starlette(routes=get_api_routes())


def _make_statuses():
    return [
        CLIStatus("claude-code", "claude", "/usr/bin/claude", True, True, "ANTHROPIC_API_KEY", False, AuthMethod.CLI_OAUTH, "2.1.91"),
        CLIStatus("openai-codex", "codex", "/usr/bin/codex", True, False, "OPENAI_API_KEY", True, AuthMethod.API_KEY, "0.118.0"),
        CLIStatus("gemini-cli", "gemini", None, False, False, "GEMINI_API_KEY", False, AuthMethod.NONE),
    ]


class TestCliStatusEndpoint:
    @patch("llm_relay.orch.discovery.discover_all", return_value=_make_statuses())
    def test_returns_json(self, mock_discover):
        client = TestClient(_make_app())
        resp = client.get("/api/v1/cli/status")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        assert data[0]["cli_id"] == "claude-code"
        assert data[0]["usable"] is True
        assert data[2]["usable"] is False


class TestDelegationsEndpoint:
    @patch("llm_relay.orch.db.get_orch_conn")
    @patch("llm_relay.orch.db.get_delegation_history")
    def test_returns_history(self, mock_history, mock_conn):
        mock_conn.return_value = MagicMock()
        mock_history.return_value = [
            {"id": 1, "cli_id": "claude-code", "success": 1},
        ]
        client = TestClient(_make_app())
        resp = client.get("/api/v1/delegations?limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1

    @patch("llm_relay.orch.db.get_orch_conn", side_effect=Exception("DB error"))
    def test_db_error(self, mock_conn):
        client = TestClient(_make_app())
        resp = client.get("/api/v1/delegations")
        assert resp.status_code == 500
        assert "error" in resp.json()


class TestDelegationStatsEndpoint:
    @patch("llm_relay.orch.db.get_orch_conn")
    @patch("llm_relay.orch.db.get_delegation_stats")
    def test_returns_stats(self, mock_stats, mock_conn):
        mock_conn.return_value = MagicMock()
        mock_stats.return_value = {
            "window_hours": 24,
            "total_delegations": 5,
            "per_cli": {},
        }
        client = TestClient(_make_app())
        resp = client.get("/api/v1/delegations/stats?window=24")
        assert resp.status_code == 200
        assert resp.json()["total_delegations"] == 5


class TestHealthEndpoint:
    @patch("llm_relay.orch.discovery.discover_all", return_value=_make_statuses())
    def test_health_ok(self, mock_discover):
        client = TestClient(_make_app())
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cli"]["total"] == 3
        assert data["cli"]["usable"] == 2  # claude (oauth) + codex (api_key)
        assert data["orch_db"] is True  # orch_conn should work (creates file)

    @patch("llm_relay.orch.discovery.discover_all", return_value=[])
    def test_health_degraded(self, mock_discover):
        client = TestClient(_make_app())
        resp = client.get("/api/v1/health")
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["cli"]["usable"] == 0
