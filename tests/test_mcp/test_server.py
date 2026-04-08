"""Tests for mcp/server.py — MCP tool functions (unit tests, no actual CLI calls)."""

import json
from unittest.mock import patch, MagicMock

import pytest

from llm_relay.orch.models import AuthMethod, CLIStatus, DelegationResult


def _make_statuses():
    return [
        CLIStatus("claude-code", "claude", "/usr/bin/claude", True, True, "ANTHROPIC_API_KEY", False, AuthMethod.CLI_OAUTH, "2.1.91"),
        CLIStatus("openai-codex", "codex", "/usr/bin/codex", True, True, "OPENAI_API_KEY", False, AuthMethod.CLI_OAUTH, "0.118.0"),
        CLIStatus("gemini-cli", "gemini", "/usr/bin/gemini", True, True, "GEMINI_API_KEY", False, AuthMethod.CLI_OAUTH, "0.36.0"),
    ]


class TestCliStatus:
    @patch("llm_relay.orch.discovery.discover_all", return_value=_make_statuses())
    def test_returns_all_clis(self, mock_discover):
        from llm_relay.mcp.server import cli_status
        result = json.loads(cli_status())
        assert len(result) == 3
        assert result[0]["cli_id"] == "claude-code"
        assert result[0]["usable"] is True
        assert result[0]["version"] == "2.1.91"

    @patch("llm_relay.orch.discovery.discover_all", return_value=[])
    def test_empty_when_no_clis(self, mock_discover):
        from llm_relay.mcp.server import cli_status
        result = json.loads(cli_status())
        assert result == []


class TestCliProbe:
    @patch("llm_relay.orch.discovery.discover_all", return_value=_make_statuses())
    def test_probe_by_short_name(self, mock_discover):
        from llm_relay.mcp.server import cli_probe
        result = json.loads(cli_probe("claude"))
        assert result["cli_id"] == "claude-code"
        assert result["binary_path"] == "/usr/bin/claude"
        assert result["installed"] is True

    @patch("llm_relay.orch.discovery.discover_all", return_value=_make_statuses())
    def test_probe_by_cli_id(self, mock_discover):
        from llm_relay.mcp.server import cli_probe
        result = json.loads(cli_probe("gemini-cli"))
        assert result["cli_id"] == "gemini-cli"

    @patch("llm_relay.orch.discovery.discover_all", return_value=_make_statuses())
    def test_probe_not_found(self, mock_discover):
        from llm_relay.mcp.server import cli_probe
        result = json.loads(cli_probe("unknown"))
        assert "error" in result


class TestCliDelegate:
    @patch("llm_relay.orch.discovery.discover_all", return_value=_make_statuses())
    @patch("llm_relay.orch.executor.execute_cli")
    def test_delegate_success(self, mock_exec, mock_discover):
        mock_exec.return_value = DelegationResult(
            cli_id="claude-code", auth_method=AuthMethod.CLI_OAUTH,
            success=True, output="Hello!", duration_ms=150.0, exit_code=0,
        )
        from llm_relay.mcp.server import cli_delegate
        result = json.loads(cli_delegate("claude", "say hello"))
        assert result["success"] is True
        assert result["output"] == "Hello!"
        assert result["cli_id"] == "claude-code"

    @patch("llm_relay.orch.discovery.discover_all", return_value=[])
    def test_delegate_no_cli_available(self, mock_discover):
        from llm_relay.mcp.server import cli_delegate
        result = json.loads(cli_delegate("claude", "say hello"))
        assert result["success"] is False
        assert "not available" in result["error"]

    @patch("llm_relay.orch.discovery.discover_all", return_value=_make_statuses())
    @patch("llm_relay.orch.executor.execute_cli")
    def test_delegate_with_options(self, mock_exec, mock_discover):
        mock_exec.return_value = DelegationResult(
            cli_id="gemini-cli", auth_method=AuthMethod.CLI_OAUTH,
            success=True, output="done", duration_ms=80.0, exit_code=0,
        )
        from llm_relay.mcp.server import cli_delegate
        result = json.loads(cli_delegate("gemini", "analyze", model="gemini-2.5-pro", timeout=60))
        assert result["success"] is True
        mock_exec.assert_called_once()
        call_kwargs = mock_exec.call_args
        assert call_kwargs.kwargs["model"] == "gemini-2.5-pro"
        assert call_kwargs.kwargs["timeout"] == 60


class TestOrchDelegate:
    @patch("llm_relay.orch.router.get_available")
    @patch("llm_relay.orch.router.execute_cli")
    @patch("llm_relay.orch.router.get_orch_conn")
    def test_smart_delegate(self, mock_conn, mock_exec, mock_avail):
        mock_avail.return_value = _make_statuses()
        mock_exec.return_value = DelegationResult(
            cli_id="claude-code", auth_method=AuthMethod.CLI_OAUTH,
            success=True, output="result", duration_ms=200.0, exit_code=0,
        )
        mock_conn.return_value = MagicMock()

        from llm_relay.mcp.server import orch_delegate
        result = json.loads(orch_delegate("test task", strategy="strongest"))
        assert result["success"] is True
        assert result["strategy"] == "strongest"

    @patch("llm_relay.orch.router.get_available", return_value=[])
    def test_no_available_cli(self, mock_avail):
        from llm_relay.mcp.server import orch_delegate
        result = json.loads(orch_delegate("test"))
        assert result["success"] is False
        assert "No authenticated" in result["error"]


class TestOrchHistory:
    @patch("llm_relay.orch.db.get_orch_conn")
    @patch("llm_relay.orch.db.get_delegation_history")
    def test_returns_history(self, mock_history, mock_conn):
        mock_conn.return_value = MagicMock()
        mock_history.return_value = [
            {"id": 1, "cli_id": "claude-code", "success": 1, "duration_ms": 100},
            {"id": 2, "cli_id": "gemini-cli", "success": 0, "duration_ms": 50},
        ]
        from llm_relay.mcp.server import orch_history
        result = json.loads(orch_history(limit=10))
        assert result["count"] == 2
        assert len(result["delegations"]) == 2

    @patch("llm_relay.orch.db.get_orch_conn", side_effect=Exception("DB error"))
    def test_db_error_graceful(self, mock_conn):
        from llm_relay.mcp.server import orch_history
        result = json.loads(orch_history())
        assert "error" in result
        assert result["delegations"] == []


class TestRelayStats:
    @patch("llm_relay.orch.db.get_orch_conn")
    @patch("llm_relay.orch.db.get_delegation_stats")
    def test_returns_stats(self, mock_stats, mock_conn):
        mock_conn.return_value = MagicMock()
        mock_stats.return_value = {
            "window_hours": 8,
            "total_delegations": 5,
            "per_cli": {"claude-code": {"total": 3, "successes": 3}},
        }
        from llm_relay.mcp.server import relay_stats
        result = json.loads(relay_stats(window_hours=8))
        assert result["total_delegations"] == 5

    @patch("llm_relay.orch.db.get_orch_conn", side_effect=Exception("DB error"))
    def test_db_error_graceful(self, mock_conn):
        from llm_relay.mcp.server import relay_stats
        result = json.loads(relay_stats())
        assert "error" in result
