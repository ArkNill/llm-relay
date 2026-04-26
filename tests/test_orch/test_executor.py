"""Tests for orch/executor.py — subprocess wrapper."""

import json
import os
import subprocess
from unittest.mock import MagicMock, patch

from llm_relay.orch.executor import (
    _build_claude_cmd,
    _build_codex_cmd,
    _build_gemini_cmd,
    _parse_codex_jsonl,
    _parse_json_output,
    execute_cli,
    prompt_hash,
    prompt_preview,
)
from llm_relay.orch.models import AuthMethod, CLIStatus


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
        assert cmd == [
            "/usr/bin/codex", "exec", "fix bug", "--json", "--skip-git-repo-check",
            "--full-auto", "--sandbox", "workspace-write",
        ]

    def test_codex_with_dir(self):
        cli = _make_cli("openai-codex", "codex", "/usr/bin/codex")
        cmd = _build_codex_cmd(cli, "test", working_dir="/tmp/project")
        assert "-C" in cmd
        assert "/tmp/project" in cmd

    def test_codex_sandbox_none(self):
        cli = _make_cli("openai-codex", "codex", "/usr/bin/codex")
        with patch.dict(os.environ, {"LLM_RELAY_CODEX_SANDBOX": "none"}):
            cmd = _build_codex_cmd(cli, "fix bug")
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert "--full-auto" not in cmd
        assert "--sandbox" not in cmd

    def test_codex_sandbox_danger_full_access(self):
        cli = _make_cli("openai-codex", "codex", "/usr/bin/codex")
        with patch.dict(os.environ, {"LLM_RELAY_CODEX_SANDBOX": "danger-full-access"}):
            cmd = _build_codex_cmd(cli, "fix bug")
        assert "--full-auto" in cmd
        assert "--sandbox" in cmd
        assert "danger-full-access" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" not in cmd

    def test_codex_sandbox_read_per_invocation(self):
        """Sandbox env is re-read on each call, not cached at module load."""
        cli = _make_cli("openai-codex", "codex", "/usr/bin/codex")
        with patch.dict(os.environ, {"LLM_RELAY_CODEX_SANDBOX": "workspace-write"}):
            cmd1 = _build_codex_cmd(cli, "first")
        with patch.dict(os.environ, {"LLM_RELAY_CODEX_SANDBOX": "none"}):
            cmd2 = _build_codex_cmd(cli, "second")
        assert "workspace-write" in cmd1
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd2

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
        assert result.duration_ms >= 0

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


class TestCodexGhTokenInjection:
    """Verify that Codex invocations get GH_TOKEN injected from a user-supplied
    GitHub App token script when configured, and skip cleanly otherwise."""

    def setup_method(self):
        from llm_relay.orch.executor import _reset_codex_gh_token_cache_for_test
        _reset_codex_gh_token_cache_for_test()

    @patch("llm_relay.orch.executor.subprocess.run")
    def test_codex_no_script_configured_no_env_injection(self, mock_run, tmp_path):
        """When the script path doesn't exist, env stays None (subprocess
        inherits the operator's environment) — no failure."""
        mock_run.return_value = MagicMock(returncode=0, stdout='{"items":[]}', stderr="")
        cli = _make_cli(cli_id="openai-codex", binary_name="codex", path="/usr/bin/codex")

        with patch("llm_relay.orch.executor._CODEX_GH_TOKEN_SCRIPT", str(tmp_path / "missing.sh")):
            execute_cli(cli, "hello")

        # subprocess.run was called with env=None (inherit current env)
        assert mock_run.call_args.kwargs.get("env") is None

    @patch("llm_relay.orch.executor.subprocess.run")
    def test_codex_with_token_script_injects_gh_token(self, mock_run, tmp_path):
        """When the script exists, token is generated and merged into subprocess env."""
        # Script that prints a fake token
        token_script = tmp_path / "gen.sh"
        token_script.write_text("#!/bin/bash\necho 'fake-token-abc123'\n")
        token_script.chmod(0o755)

        # First call to subprocess.run returns the token (script invocation),
        # second returns the codex result.
        def run_side_effect(*args, **kwargs):
            cmd = args[0]
            if cmd[0] == str(token_script):
                return MagicMock(returncode=0, stdout="fake-token-abc123\n", stderr="")
            return MagicMock(returncode=0, stdout='{"items":[]}', stderr="")
        mock_run.side_effect = run_side_effect

        cli = _make_cli(cli_id="openai-codex", binary_name="codex", path="/usr/bin/codex")
        with patch("llm_relay.orch.executor._CODEX_GH_TOKEN_SCRIPT", str(token_script)):
            execute_cli(cli, "hello")

        # Find the codex execution call (not the token-generation call)
        codex_call = next(
            c for c in mock_run.call_args_list
            if c.args[0][0] != str(token_script)
        )
        env = codex_call.kwargs.get("env")
        assert env is not None
        assert env.get("GH_TOKEN") == "fake-token-abc123"

    @patch("llm_relay.orch.executor.subprocess.run")
    def test_non_codex_cli_never_calls_token_script(self, mock_run, tmp_path):
        """A claude-code or gemini-cli invocation must not trigger the token script."""
        token_script = tmp_path / "gen.sh"
        token_script.write_text("#!/bin/bash\necho 'should-not-be-called'\n")
        token_script.chmod(0o755)

        mock_run.return_value = MagicMock(returncode=0, stdout='{"result":"x"}', stderr="")
        cli = _make_cli(cli_id="claude-code", binary_name="claude", path="/usr/bin/claude")

        with patch("llm_relay.orch.executor._CODEX_GH_TOKEN_SCRIPT", str(token_script)):
            execute_cli(cli, "hello")

        # Token script must not appear in any subprocess.run invocation
        for call in mock_run.call_args_list:
            assert call.args[0][0] != str(token_script), \
                f"non-Codex CLI should not invoke token script, but did: {call.args[0]}"
        # And env must be None (no injection)
        assert mock_run.call_args.kwargs.get("env") is None

    @patch("llm_relay.orch.executor.subprocess.run")
    def test_token_script_failure_is_silent_no_injection(self, mock_run, tmp_path):
        """When the token script fails, env stays None — Codex still runs."""
        token_script = tmp_path / "gen.sh"
        token_script.write_text("#!/bin/bash\necho 'boom' >&2\nexit 1\n")
        token_script.chmod(0o755)

        def run_side_effect(*args, **kwargs):
            cmd = args[0]
            if cmd[0] == str(token_script):
                return MagicMock(returncode=1, stdout="", stderr="boom\n")
            return MagicMock(returncode=0, stdout='{"items":[]}', stderr="")
        mock_run.side_effect = run_side_effect

        cli = _make_cli(cli_id="openai-codex", binary_name="codex", path="/usr/bin/codex")
        with patch("llm_relay.orch.executor._CODEX_GH_TOKEN_SCRIPT", str(token_script)):
            result = execute_cli(cli, "hello")

        # Codex still ran (success), but with no GH_TOKEN injected
        codex_call = next(
            c for c in mock_run.call_args_list
            if c.args[0][0] != str(token_script)
        )
        assert codex_call.kwargs.get("env") is None
        assert result.success is True

    @patch("llm_relay.orch.executor.subprocess.run")
    def test_token_is_cached_between_calls(self, mock_run, tmp_path):
        """Two Codex invocations within TTL only call the token script once."""
        token_script = tmp_path / "gen.sh"
        token_script.write_text("#!/bin/bash\necho 'cached-token'\n")
        token_script.chmod(0o755)

        script_calls = {"count": 0}
        def run_side_effect(*args, **kwargs):
            cmd = args[0]
            if cmd[0] == str(token_script):
                script_calls["count"] += 1
                return MagicMock(returncode=0, stdout="cached-token\n", stderr="")
            return MagicMock(returncode=0, stdout='{"items":[]}', stderr="")
        mock_run.side_effect = run_side_effect

        cli = _make_cli(cli_id="openai-codex", binary_name="codex", path="/usr/bin/codex")
        with patch("llm_relay.orch.executor._CODEX_GH_TOKEN_SCRIPT", str(token_script)):
            execute_cli(cli, "first")
            execute_cli(cli, "second")
            execute_cli(cli, "third")

        assert script_calls["count"] == 1, \
            f"token script should only be called once due to caching, was called {script_calls['count']} times"


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
