"""Tests for orch/router.py — request routing."""

from unittest.mock import patch, MagicMock

import pytest

from llm_relay.orch.models import (
    AuthMethod,
    CLIStatus,
    DelegationRequest,
    DelegationResult,
    DelegationStrategy,
)
from llm_relay.orch.router import _select_cli, route


def _cli(cli_id: str, auth: AuthMethod = AuthMethod.CLI_OAUTH) -> CLIStatus:
    return CLIStatus(
        cli_id=cli_id,
        binary_name=cli_id.split("-")[0],
        binary_path="/usr/bin/" + cli_id.split("-")[0],
        installed=True,
        cli_authenticated=auth == AuthMethod.CLI_OAUTH,
        api_key_name="TEST",
        api_key_available=auth == AuthMethod.API_KEY,
        preferred_auth=auth,
    )


class TestSelectCli:
    def test_preferred_found(self):
        available = [_cli("claude-code"), _cli("gemini-cli")]
        result = _select_cli(available, DelegationStrategy.AUTO, preferred="gemini-cli")
        assert result.cli_id == "gemini-cli"

    def test_preferred_by_binary_name(self):
        available = [_cli("claude-code"), _cli("gemini-cli")]
        result = _select_cli(available, DelegationStrategy.AUTO, preferred="gemini")
        assert result.cli_id == "gemini-cli"

    def test_preferred_not_found(self):
        available = [_cli("claude-code")]
        result = _select_cli(available, DelegationStrategy.AUTO, preferred="codex")
        assert result is None

    def test_strongest_prefers_claude(self):
        available = [_cli("gemini-cli"), _cli("claude-code"), _cli("openai-codex")]
        result = _select_cli(available, DelegationStrategy.STRONGEST)
        assert result.cli_id == "claude-code"

    def test_fastest_prefers_gemini(self):
        available = [_cli("claude-code"), _cli("gemini-cli"), _cli("openai-codex")]
        result = _select_cli(available, DelegationStrategy.FASTEST)
        assert result.cli_id == "gemini-cli"

    def test_cheapest_prefers_gemini(self):
        available = [_cli("claude-code"), _cli("openai-codex"), _cli("gemini-cli")]
        result = _select_cli(available, DelegationStrategy.CHEAPEST)
        assert result.cli_id == "gemini-cli"

    def test_round_robin(self):
        import llm_relay.orch.router as r
        r._rr_index = 0
        available = [_cli("claude-code"), _cli("gemini-cli")]
        r1 = _select_cli(available, DelegationStrategy.ROUND_ROBIN)
        r2 = _select_cli(available, DelegationStrategy.ROUND_ROBIN)
        r3 = _select_cli(available, DelegationStrategy.ROUND_ROBIN)
        assert r1.cli_id == "claude-code"
        assert r2.cli_id == "gemini-cli"
        assert r3.cli_id == "claude-code"

    def test_empty_available(self):
        result = _select_cli([], DelegationStrategy.AUTO)
        assert result is None

    def test_fallback_to_first(self):
        # CLI not in any strategy order
        available = [CLIStatus(
            cli_id="custom-cli", binary_name="custom", binary_path="/usr/bin/custom",
            installed=True, cli_authenticated=True, api_key_name="X",
            api_key_available=False, preferred_auth=AuthMethod.CLI_OAUTH,
        )]
        result = _select_cli(available, DelegationStrategy.STRONGEST)
        assert result.cli_id == "custom-cli"


class TestRoute:
    @patch("llm_relay.orch.router.get_available")
    @patch("llm_relay.orch.router.execute_cli")
    @patch("llm_relay.orch.router.get_orch_conn")
    def test_success(self, mock_conn, mock_exec, mock_avail):
        mock_avail.return_value = [_cli("claude-code")]
        mock_exec.return_value = DelegationResult(
            cli_id="claude-code", auth_method=AuthMethod.CLI_OAUTH,
            success=True, output="done", duration_ms=100,
        )
        mock_conn.return_value = MagicMock()

        req = DelegationRequest(prompt="hello")
        result = route(req)
        assert result.success is True
        assert result.cli_id == "claude-code"

    @patch("llm_relay.orch.router.get_available")
    def test_no_available_cli(self, mock_avail):
        mock_avail.return_value = []
        req = DelegationRequest(prompt="hello")
        result = route(req)
        assert result.success is False
        assert "No authenticated CLI" in result.error

    @patch("llm_relay.orch.router.get_available")
    def test_preferred_not_available(self, mock_avail):
        mock_avail.return_value = [_cli("claude-code")]
        req = DelegationRequest(prompt="hello", preferred_cli="gemini-cli")
        result = route(req)
        assert result.success is False
        assert "not available" in result.error

    @patch("llm_relay.orch.router.get_available")
    @patch("llm_relay.orch.router.execute_cli")
    @patch("llm_relay.orch.router.get_orch_conn")
    def test_db_logging_failure_is_silent(self, mock_conn, mock_exec, mock_avail):
        mock_avail.return_value = [_cli("claude-code")]
        mock_exec.return_value = DelegationResult(
            cli_id="claude-code", auth_method=AuthMethod.CLI_OAUTH,
            success=True, output="done",
        )
        mock_conn.side_effect = Exception("DB error")

        req = DelegationRequest(prompt="hello")
        result = route(req)
        # Should still succeed even if DB logging fails
        assert result.success is True
