"""Subprocess execution wrapper for CLI tools — stdlib only."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from typing import List, Optional

from llm_relay.orch.models import CLIStatus, DelegationResult

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = int(os.environ.get("LLM_RELAY_ORCH_EXEC_TIMEOUT", "120"))


def execute_cli(
    cli: CLIStatus,
    prompt: str,
    *,
    model: Optional[str] = None,
    working_dir: Optional[str] = None,
    max_budget_usd: Optional[float] = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> DelegationResult:
    """Execute a headless CLI command and return parsed result.

    Builds the command based on cli.cli_id:
      claude:  claude -p "{prompt}" --output-format=json [--model X]
      codex:   codex exec "{prompt}" --json --full-auto [--model X]
      gemini:  gemini -p "{prompt}" --output-format=json -y [--model X]
    """
    if not cli.binary_path:
        return DelegationResult(
            cli_id=cli.cli_id,
            auth_method=cli.preferred_auth,
            success=False,
            output="",
            error="CLI binary not found",
            exit_code=-1,
        )

    builders = {
        "claude-code": _build_claude_cmd,
        "openai-codex": _build_codex_cmd,
        "gemini-cli": _build_gemini_cmd,
    }

    builder = builders.get(cli.cli_id)
    if builder is None:
        return DelegationResult(
            cli_id=cli.cli_id,
            auth_method=cli.preferred_auth,
            success=False,
            output="",
            error="Unknown CLI: {}".format(cli.cli_id),
            exit_code=-1,
        )

    cmd = builder(cli, prompt, model=model, working_dir=working_dir, max_budget_usd=max_budget_usd)
    logger.info("Executing %s: %s", cli.cli_id, " ".join(cmd[:4]) + " ...")

    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=working_dir,
            stdin=subprocess.DEVNULL,
        )
        duration_ms = (time.monotonic() - start) * 1000

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        success = proc.returncode == 0

        # Parse output based on CLI type
        output = _extract_output(cli.cli_id, stdout, stderr)
        error = stderr.strip() if not success and stderr.strip() else None

        result = DelegationResult(
            cli_id=cli.cli_id,
            auth_method=cli.preferred_auth,
            success=success,
            output=output,
            error=error,
            duration_ms=duration_ms,
            exit_code=proc.returncode,
        )
        logger.info(
            "Completed %s: success=%s, duration=%.0fms, output=%d chars",
            cli.cli_id, success, duration_ms, len(output),
        )
        return result

    except subprocess.TimeoutExpired:
        duration_ms = (time.monotonic() - start) * 1000
        logger.warning("Timeout after %.0fms for %s", duration_ms, cli.cli_id)
        return DelegationResult(
            cli_id=cli.cli_id,
            auth_method=cli.preferred_auth,
            success=False,
            output="",
            error="Execution timed out after {}s".format(timeout),
            duration_ms=duration_ms,
            exit_code=-1,
        )
    except OSError as e:
        duration_ms = (time.monotonic() - start) * 1000
        logger.error("OS error executing %s: %s", cli.cli_id, e)
        return DelegationResult(
            cli_id=cli.cli_id,
            auth_method=cli.preferred_auth,
            success=False,
            output="",
            error=str(e),
            duration_ms=duration_ms,
            exit_code=-1,
        )


def _build_claude_cmd(
    cli: CLIStatus,
    prompt: str,
    *,
    model: Optional[str] = None,
    working_dir: Optional[str] = None,
    max_budget_usd: Optional[float] = None,
) -> List[str]:
    """Build Claude Code headless command."""
    cmd = [cli.binary_path, "-p", prompt, "--output-format", "json"]
    if model:
        cmd.extend(["--model", model])
    if max_budget_usd is not None and max_budget_usd > 0:
        cmd.extend(["--max-budget-usd", str(max_budget_usd)])
    return cmd


def _build_codex_cmd(
    cli: CLIStatus,
    prompt: str,
    *,
    model: Optional[str] = None,
    working_dir: Optional[str] = None,
    max_budget_usd: Optional[float] = None,
) -> List[str]:
    """Build Codex CLI headless command."""
    cmd = [cli.binary_path, "exec", prompt, "--json", "--full-auto", "--skip-git-repo-check"]
    if model:
        cmd.extend(["--model", model])
    if working_dir:
        cmd.extend(["-C", working_dir])
    return cmd


def _build_gemini_cmd(
    cli: CLIStatus,
    prompt: str,
    *,
    model: Optional[str] = None,
    working_dir: Optional[str] = None,
    max_budget_usd: Optional[float] = None,
) -> List[str]:
    """Build Gemini CLI headless command."""
    cmd = [cli.binary_path, "-p", prompt, "--output-format", "json", "-y"]
    if model:
        cmd.extend(["-m", model])
    return cmd


def _extract_output(cli_id: str, stdout: str, stderr: str) -> str:
    """Extract meaningful output from CLI response."""
    if cli_id == "openai-codex":
        return _parse_codex_jsonl(stdout)
    # For claude and gemini, try to parse JSON and extract the result text
    return _parse_json_output(stdout)


def _parse_json_output(stdout: str) -> str:
    """Parse JSON output and extract the result text."""
    if not stdout.strip():
        return ""
    try:
        data = json.loads(stdout)
        # Claude Code JSON output has a "result" field
        if isinstance(data, dict):
            if "result" in data:
                return str(data["result"])
            if "content" in data:
                return str(data["content"])
            if "text" in data:
                return str(data["text"])
        return stdout.strip()
    except (json.JSONDecodeError, ValueError):
        return stdout.strip()


def _parse_codex_jsonl(stdout: str) -> str:
    """Parse Codex JSONL event stream and extract the final message."""
    if not stdout.strip():
        return ""
    last_message = ""
    for line in stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            if isinstance(event, dict):
                # Codex events have "type" field; look for message/response events
                event_type = event.get("type", "")
                if event_type in ("message", "response", "assistant"):
                    content = event.get("content", event.get("text", event.get("message", "")))
                    if content:
                        last_message = str(content)
                elif "content" in event or "text" in event or "message" in event:
                    content = event.get("content", event.get("text", event.get("message", "")))
                    if content:
                        last_message = str(content)
        except (json.JSONDecodeError, ValueError):
            continue
    return last_message or stdout.strip()


def prompt_hash(prompt: str) -> str:
    """SHA-256 hash of a prompt for dedup/tracking (no full prompt stored)."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def prompt_preview(prompt: str, max_len: int = 200) -> str:
    """Truncated preview of a prompt for logging."""
    if len(prompt) <= max_len:
        return prompt
    return prompt[:max_len] + "..."
