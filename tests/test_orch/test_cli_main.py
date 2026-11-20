"""Tests for the shell entry point ``python -m llm_relay.orch``."""

from __future__ import annotations

import json
from unittest.mock import patch

from llm_relay.orch import __main__ as orch_cli
from llm_relay.orch.models import AuthMethod, CLIStatus, DelegationResult


def _agy(usable: bool = True) -> CLIStatus:
    return CLIStatus(
        cli_id="antigravity-cli", binary_name="agy", binary_path="/home/u/bin/agyGt", installed=True,
        cli_authenticated=usable, api_key_name=None, api_key_available=False,
        preferred_auth=AuthMethod.CLI_OAUTH if usable else AuthMethod.NONE, version="1.5.13",
    )


def _result(success: bool = True, output: str = "pong\n", error=None, exit_code: int = 0) -> DelegationResult:
    return DelegationResult(
        cli_id="antigravity-cli", auth_method=AuthMethod.CLI_OAUTH, success=success, output=output, error=error,
        duration_ms=1234.56, exit_code=exit_code,
    )


class TestDelegate:
    @patch("llm_relay.orch.__main__._log")
    @patch("llm_relay.orch.executor.execute_cli")
    @patch("llm_relay.orch.discovery.discover_all")
    def test_success_prints_json_and_exits_zero(self, mock_discover, mock_exec, mock_log, capsys):
        mock_discover.return_value = [_agy()]
        mock_exec.return_value = _result()
        rc = orch_cli.main(["delegate", "--prompt", "ping", "--working-dir", "/repo", "--timeout", "90"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload == {
            "success": True, "cli_id": "antigravity-cli", "model": None, "output": "pong\n", "error": None,
            "duration_ms": 1234.6, "exit_code": 0,
        }
        kwargs = mock_exec.call_args.kwargs
        assert kwargs == {"model": None, "working_dir": "/repo", "timeout": 90}
        assert mock_exec.call_args.args[1] == "ping"
        assert mock_log.called

    @patch("llm_relay.orch.__main__._log")
    @patch("llm_relay.orch.executor.execute_cli")
    @patch("llm_relay.orch.discovery.discover_all")
    def test_failure_exits_one_and_writes_out_file(self, mock_discover, mock_exec, mock_log, tmp_path, capsys):
        mock_discover.return_value = [_agy()]
        mock_exec.return_value = _result(success=False, output="", error="print timeout after 110s", exit_code=3)
        out = tmp_path / "result.json"
        prompt_file = tmp_path / "task.md"
        prompt_file.write_text("한글 과제\n", encoding="utf-8")
        rc = orch_cli.main(["delegate", "--cli", "antigravity", "--prompt-file", str(prompt_file),
                            "--model", "gemini-4-neon-high", "--out", str(out)])
        assert rc == 1
        assert capsys.readouterr().out == ""
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["success"] is False and payload["error"] == "print timeout after 110s"
        assert payload["model"] == "gemini-4-neon-high" and payload["exit_code"] == 3
        assert mock_exec.call_args.args[1] == "한글 과제\n"
        assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".delegate-")] == []

    @patch("llm_relay.orch.__main__._log")
    @patch("llm_relay.orch.executor.execute_cli")
    @patch("llm_relay.orch.discovery.discover_all")
    def test_unusable_cli_is_reported_without_running(self, mock_discover, mock_exec, mock_log, capsys):
        mock_discover.return_value = [_agy(usable=False)]
        rc = orch_cli.main(["delegate", "--prompt", "ping"])
        assert rc == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["success"] is False and "not available" in payload["error"]
        assert not mock_exec.called and not mock_log.called

    @patch("llm_relay.orch.executor.execute_cli")
    @patch("llm_relay.orch.discovery.discover_all")
    def test_empty_prompt_is_a_usage_error(self, mock_discover, mock_exec, capsys):
        assert orch_cli.main(["delegate", "--prompt", "   "]) == 2
        assert orch_cli.main(["delegate"]) == 2
        assert orch_cli.main(["delegate", "--prompt-file", "/no/such/file"]) == 2
        assert not mock_exec.called

    @patch("llm_relay.orch.__main__._log")
    @patch("llm_relay.orch.executor.execute_cli")
    @patch("llm_relay.orch.discovery.discover_all")
    def test_no_log_flag(self, mock_discover, mock_exec, mock_log, capsys):
        mock_discover.return_value = [_agy()]
        mock_exec.return_value = _result()
        assert orch_cli.main(["delegate", "--prompt", "ping", "--no-log"]) == 0
        assert not mock_log.called

    @patch("llm_relay.orch.executor.execute_cli")
    @patch("llm_relay.orch.discovery.discover_all")
    def test_delegation_is_logged_with_strategy_cli(self, mock_discover, mock_exec, capsys):
        mock_discover.return_value = [_agy()]
        mock_exec.return_value = _result()
        with patch("llm_relay.orch.db.log_delegation") as mock_log_delegation, \
                patch("llm_relay.orch.db.get_orch_conn") as mock_conn:
            assert orch_cli.main(["delegate", "--prompt", "ping"]) == 0
        assert mock_conn.called
        kwargs = mock_log_delegation.call_args.kwargs
        assert kwargs["cli_id"] == "antigravity-cli" and kwargs["strategy"] == "cli" and kwargs["success"] is True


class TestStatusAndUsage:
    @patch("llm_relay.orch.discovery.discover_all")
    def test_status_lists_clis(self, mock_discover, capsys):
        mock_discover.return_value = [_agy()]
        assert orch_cli.main(["status"]) == 0
        rows = json.loads(capsys.readouterr().out)
        assert rows[0]["cli_id"] == "antigravity-cli" and rows[0]["usable"] is True
        assert rows[0]["binary_path"] == "/home/u/bin/agyGt"

    def test_no_command_is_a_usage_error(self, capsys):
        assert orch_cli.main([]) == 2
