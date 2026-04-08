"""Tests for orch/executor.py — subprocess wrapper."""

import json
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from llm_relay.orch.executor import (
    _build_claude_cmd,
    _build_codex_cmd,
    _build_gemini_cmd,
    _extract_output,
    _parse_codex_jsonl,
    _parse_json_output,
    execute_cli,
    prompt_hash,
    prompt_preview,
)
from llm_relay.orch.models import AuthMethod, CLIStatus, DelegationResult


def _make_cli(cli_id: str = "claude-code", binary_name: str = "claude", path: str = "/usr/bin/claude") -> CLIStatus:
    return CLIStatus(
        cli_id=cli_id,
        binary_name=binary_name,
        binary_path=path,
        installed=True,
        cli_authenticated=True,
        api_key_name="TEST_KEY",
        api_key_available=False,
        preferred_auth=AuthMethod.CLI_OAUTH,
    )


class TestBuildCommands:
    def test_claude_basic(self):
        cli = _make_cli()
        cmd = _build_claude_cmd(cli, "hello world")
        assert cmd == ["/usr/bin/claude", "-p", "hello world", "--output-format", "json"]

    def test_claude_with_model(self):
        cli = _make_cli()
        cmd = _build_claude_cmd(cli, "test", model="sonnet")
        assert "--model" in cmd
        assert "sonnet" in cmd

    def test_claude_with_budget(self):
        cli = _make_cli()
        cmd = _build_claude_cmd(cli, "test", max_budget_usd=0.5)
        assert "--max-budget-usd" in cmd
        assert "0.5" in cmd

    def test_codex_basic(self):
        cli = _make_cli("openai-codex", "codex", "/usr/bin/codex")
        cmd = _build_codex_cmd(cli, "fix bug")
        assert cmd == ["/usr/bin/codex", "exec", "fix bug", "--json", "--full-auto", "--skip-git-repo-check"]

    def test_codex_with_dir(self):
        cli = _make_cli("openai-codex", "codex", "/usr/bin/codex")
        cmd = _build_codex_cmd(cli, "test", working_dir="/tmp/project")
        assert "-C" in cmd
        assert "/tmp/project" in cmd

    def test_gemini_basic(self):
        cli = _make_cli("gemini-cli", "gemini", "/usr/bin/gemini")
        cmd = _build_gemini_cmd(cli, "analyze")
        assert cmd == ["/usr/bin/gemini", "-p", "analyze", "--output-format", "json", "-y"]

    def test_gemini_with_model(self):
        cli = _make_cli("gemini-cli", "gemini", "/usr/bin/gemini")
        cmd = _build_gemini_cmd(cli, "test", model="gemini-2.5-pro")
        assert "-m" in cmd
        assert "gemini-2.5-pro" in cmd


class TestParseOutput:
    def test_json_with_result(self):
        stdout = json.dumps({"result": "Hello, world!"})
        assert _parse_json_output(stdout) == "Hello, world!"

    def test_json_with_content(self):
        stdout = json.dumps({"content": "Some content"})
        assert _parse_json_output(stdout) == "Some content"

    def test_json_with_text(self):
        stdout = json.dumps({"text": "text output"})
        assert _parse_json_output(stdout) == "text output"

    def test_json_fallback_raw(self):
        stdout = json.dumps({"unknown_field": "value"})
        assert "unknown_field" in _parse_json_output(stdout)

    def test_non_json_passthrough(self):
        assert _parse_json_output("plain text output") == "plain text output"

    def test_empty_string(self):
        assert _parse_json_output("") == ""

    def test_codex_jsonl_message(self):
        lines = [
            json.dumps({"type": "system", "content": "init"}),
            json.dumps({"type": "message", "content": "Final answer"}),
        ]
        stdout = "\n".join(lines)
        assert _parse_codex_jsonl(stdout) == "Final answer"

    def test_codex_jsonl_empty(self):
        assert _parse_codex_jsonl("") == ""

    def test_codex_jsonl_fallback(self):
        stdout = "not json at all"
        assert _parse_codex_jsonl(stdout) == "not json at all"

    def test_codex_jsonl_response_type(self):
        lines = [
            json.dumps({"type": "response", "content": "answer"}),
        ]
        assert _parse_codex_jsonl("\n".join(lines)) == "answer"


class TestExecuteCli:
    @patch("llm_relay.orch.executor.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"result": "done"}),
            stderr="",
        )
        cli = _make_cli()
        result = execute_cli(cli, "hello")
        assert result.success is True
        assert result.output == "done"
        assert result.exit_code == 0
        assert result.duration_ms > 0

    @patch("llm_relay.orch.executor.subprocess.run")
    def test_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Error: authentication failed",
        )
        cli = _make_cli()
        result = execute_cli(cli, "hello")
        assert result.success is False
        assert result.error == "Error: authentication failed"
        assert result.exit_code == 1

    @patch("llm_relay.orch.executor.subprocess.run")
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["claude"], timeout=5)
        cli = _make_cli()
        result = execute_cli(cli, "hello", timeout=5)
        assert result.success is False
        assert "timed out" in result.error
        assert result.exit_code == -1

    @patch("llm_relay.orch.executor.subprocess.run")
    def test_os_error(self, mock_run):
        mock_run.side_effect = OSError("No such file")
        cli = _make_cli()
        result = execute_cli(cli, "hello")
        assert result.success is False
        assert "No such file" in result.error

    def test_no_binary_path(self):
        cli = CLIStatus(
            cli_id="claude-code", binary_name="claude", binary_path=None,
            installed=False, cli_authenticated=False,
            api_key_name="TEST", api_key_available=False,
            preferred_auth=AuthMethod.NONE,
        )
        result = execute_cli(cli, "hello")
        assert result.success is False
        assert "not found" in result.error

    def test_unknown_cli(self):
        cli = CLIStatus(
            cli_id="unknown-cli", binary_name="unknown", binary_path="/usr/bin/unknown",
            installed=True, cli_authenticated=True,
            api_key_name="TEST", api_key_available=False,
            preferred_auth=AuthMethod.CLI_OAUTH,
        )
        result = execute_cli(cli, "hello")
        assert result.success is False
        assert "Unknown CLI" in result.error


class TestPromptUtils:
    def test_hash_deterministic(self):
        h1 = prompt_hash("hello")
        h2 = prompt_hash("hello")
        assert h1 == h2
        assert len(h1) == 16

    def test_hash_different(self):
        assert prompt_hash("hello") != prompt_hash("world")

    def test_preview_short(self):
        assert prompt_preview("short") == "short"

    def test_preview_truncated(self):
        long = "x" * 300
        p = prompt_preview(long, max_len=200)
        assert len(p) == 203  # 200 + "..."
        assert p.endswith("...")
