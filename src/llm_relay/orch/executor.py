"""Subprocess execution wrapper for CLI tools -- stdlib only."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

from llm_relay.orch import agy_quota
from llm_relay.orch.models import CLIStatus, DelegationResult

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = int(os.environ.get("LLM_RELAY_ORCH_EXEC_TIMEOUT", "120"))

# Codex GitHub App token injection — when llm-relay's cli_delegate spawns
# Codex (cli.cli_id == "openai-codex"), we generate a fresh GitHub App
# installation token via a user-provided script and inject it as GH_TOKEN
# in the subprocess env. This lets Codex use a dedicated bot identity for
# `gh` operations (PR comments, label changes, branch pushes) instead of
# falling back to the operator's personal token.
#
# Tokens are GitHub-installation tokens that expire after 60 minutes; we
# cache for 50 minutes to leave a comfortable margin.
#
# Disable by unsetting LLM_RELAY_CODEX_GH_TOKEN_SCRIPT (or pointing it at a
# non-existent path). Disabled by default — feature only activates when the
# script exists and is executable.
_CODEX_GH_TOKEN_SCRIPT = os.environ.get(
    "LLM_RELAY_CODEX_GH_TOKEN_SCRIPT",
    os.path.expanduser("~/.llm-relay/github-apps/generate-token.sh"),
)
_CODEX_GH_TOKEN_AGENT = os.environ.get("LLM_RELAY_CODEX_GH_AGENT", "codex-reviewer")
_CODEX_GH_TOKEN_TTL_S = 3000  # 50 min cache; tokens themselves expire at 60
_codex_gh_token_cache: Optional[tuple] = None  # (token, expiry_monotonic)

# Antigravity CLI (agy) headless settings.
# --print-timeout is set this many seconds below the subprocess timeout so agy
# can emit its own JSON envelope (with a real error) before we kill it.
_AGY_PRINT_TIMEOUT_MARGIN_S = 10
# Model used when a delegation does not name one (override: LLM_RELAY_AGY_DEFAULT_MODEL).
_AGY_DEFAULT_MODEL = "gemini-4-neon-xhigh"
# Default permission rules passed via --settings. Without explicit rules, a tool
# call that needs approval is auto-denied in headless mode and the whole answer
# is lost (rc=0, status SUCCESS, empty response). With explicit deny rules the
# denied tool returns an error to the model and the answer still comes back.
_AGY_READ_ONLY_SETTINGS = {
    "permissions": {
        "allow": ["read_file(*)", "read_url(*)"],
        "deny": ["command(*)", "write_file(*)", "execute_url(*)", "mcp(*)"],
    },
}


def _get_codex_gh_token() -> Optional[str]:
    """Generate (or return cached) GitHub App installation token for Codex.

    Returns None silently if the script doesn't exist, isn't executable, or
    fails — callers must tolerate that and continue without the env injection.
    """
    global _codex_gh_token_cache
    if _codex_gh_token_cache:
        token, expiry = _codex_gh_token_cache
        if time.monotonic() < expiry:
            return token

    script = _CODEX_GH_TOKEN_SCRIPT
    if not script or not os.path.isfile(script) or not os.access(script, os.X_OK):
        return None

    try:
        proc = subprocess.run(
            [script, _CODEX_GH_TOKEN_AGENT],
            capture_output=True,
            text=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            token = proc.stdout.strip()
            _codex_gh_token_cache = (token, time.monotonic() + _CODEX_GH_TOKEN_TTL_S)
            logger.debug("Codex GH token generated (cached %ds)", _CODEX_GH_TOKEN_TTL_S)
            return token
        logger.debug("Codex GH token script returned %d: %s", proc.returncode, proc.stderr.strip()[:200])
    except Exception:
        logger.debug("Codex GH token generation failed", exc_info=True)
    return None


def _reset_codex_gh_token_cache_for_test() -> None:
    """Test helper — clear the in-memory cache."""
    global _codex_gh_token_cache
    _codex_gh_token_cache = None


def execute_cli(
    cli: CLIStatus,
    prompt: str,
    *,
    model: Optional[str] = None,
    working_dir: Optional[str] = None,
    max_budget_usd: Optional[float] = None,
    timeout: int = _DEFAULT_TIMEOUT,
    quota_check: bool = True,
    quota_block_pct: Optional[float] = None,
) -> DelegationResult:
    """Execute a headless CLI command and return parsed result.

    Builds the command based on cli.cli_id:
      claude:  claude -p "{prompt}" --output-format=json [--model X]
      codex:   codex exec "{prompt}" --json --full-auto [--model X]
      gemini:  gemini -p "{prompt}" --output-format=json -y [--model X]
      agy:     agy -p "{prompt}" --output-format json [--print-timeout Ns]
               (--settings {read-only rules} | --dangerously-skip-permissions) [--model X]

    For antigravity-cli the JSON envelope decides success: agy can exit 0 with
    status SUCCESS and an empty response (tool approval auto-denied), so the
    return code alone is not trusted.

    For antigravity-cli a quota preflight (orch.agy_quota) runs first unless
    quota_check is False or LLM_RELAY_AGY_QUOTA_CHECK=0. When a bucket the model
    draws from is below the floor (quota_block_pct, else
    LLM_RELAY_AGY_QUOTA_BLOCK_PCT, default 10) agy is not started and the result
    fails with an error starting "quota preflight:" and exit_code -1. A failed
    quota query never blocks. The verdict (and, after a call that ran at least
    LLM_RELAY_AGY_QUOTA_AFTER_MIN_S, the quota spent) is attached as
    result.quota; read it with getattr(result, "quota", None).
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
        "antigravity-cli": _build_antigravity_cmd,
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

    cmd = builder(
        cli, prompt, model=model, working_dir=working_dir, max_budget_usd=max_budget_usd, timeout=timeout,
    )

    # agy quota preflight. quota stays None for every other CLI and when the check is off.
    quota: Optional[Dict[str, Any]] = None
    quota_model: Optional[str] = None
    quota_cwd: Optional[str] = None
    if cli.cli_id == "antigravity-cli" and quota_check and agy_quota.check_enabled():
        quota_model = _agy_effective_model(model)
        # The query only needs a directory to start in; a missing one must not fail it.
        quota_cwd = working_dir if working_dir and os.path.isdir(working_dir) else None
        quota, preflight_ms = _agy_quota_preflight(cli, quota_model, quota_cwd, quota_block_pct)
        if quota.get("verdict") == "blocked":
            blocked = DelegationResult(
                cli_id=cli.cli_id,
                auth_method=cli.preferred_auth,
                success=False,
                output="",
                error=agy_quota.block_message(quota),
                duration_ms=preflight_ms,
                exit_code=-1,
            )
            # Attached, not passed to the constructor: a long-lived MCP process may still
            # hold an older DelegationResult without the quota field.
            blocked.quota = quota
            return blocked

    logger.info("Executing %s: %s", cli.cli_id, " ".join(cmd[:4]) + " ...")

    # Inject Codex bot-account GitHub App token when invoking Codex. None
    # = inherit current env (the default subprocess behavior); falling back
    # to operator's gh credentials. See _get_codex_gh_token above for the
    # opt-in / disable contract.
    env = None
    if cli.cli_id == "openai-codex":
        gh_token = _get_codex_gh_token()
        if gh_token:
            env = {**os.environ, "GH_TOKEN": gh_token}

    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=working_dir,
            stdin=subprocess.DEVNULL,
            env=env,
        )
        duration_ms = (time.monotonic() - start) * 1000

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        success = proc.returncode == 0

        # Parse output based on CLI type
        failure_reason: Optional[str] = None
        if cli.cli_id == "antigravity-cli":
            output, failure_reason = _parse_antigravity_output(stdout)
        else:
            output = _extract_output(cli.cli_id, stdout, stderr)
        error = stderr.strip() if not success and stderr.strip() else None
        if failure_reason is not None:
            # The envelope overrides the return code (rc=0 can still mean no answer).
            success = False
            error = failure_reason

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

    except subprocess.TimeoutExpired:
        duration_ms = (time.monotonic() - start) * 1000
        logger.warning("Timeout after %.0fms for %s", duration_ms, cli.cli_id)
        result = DelegationResult(
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
        result = DelegationResult(
            cli_id=cli.cli_id,
            auth_method=cli.preferred_auth,
            success=False,
            output="",
            error=str(e),
            duration_ms=duration_ms,
            exit_code=-1,
        )

    if quota is not None:
        # Outside the try: the follow-up query must not be mistaken for a failure of the call itself.
        _agy_quota_after(quota, cli, quota_model, quota_cwd, result.duration_ms)
        result.quota = quota
    return result


def _agy_quota_preflight(
    cli: CLIStatus,
    model: Optional[str],
    cwd: Optional[str],
    block_pct: Optional[float],
) -> Tuple[Dict[str, Any], float]:
    """Query agy's quota and judge it for ``model``; returns (quota, elapsed ms). Never raises.

    A failed query gives verdict ``unknown`` so the delegation still runs (fail-open):
    agy updates itself often and the /usage shape may change.
    """
    start = time.monotonic()
    buckets, reason = agy_quota.fetch_usage(cli.binary_path, cwd=cwd)
    if buckets is None:
        quota: Dict[str, Any] = {"verdict": "unknown", "note": reason}
    else:
        env_block_pct, warn_pct = agy_quota.thresholds_from_env()
        quota = agy_quota.evaluate(
            buckets,
            model,
            block_pct=block_pct if block_pct is not None else env_block_pct,
            warn_pct=warn_pct,
        )
    elapsed_ms = (time.monotonic() - start) * 1000
    logger.info(
        "agy quota preflight: verdict=%s, lowest=%s, took=%.0fms",
        quota["verdict"], _lowest_bucket(quota), elapsed_ms,
    )
    return quota, elapsed_ms


def _lowest_bucket(quota: Dict[str, Any]) -> str:
    """``id remaining%`` of the tightest bucket in quota["before"], or ``-`` (log text only)."""
    known = [
        (info["remaining_pct"], bucket_id)
        for bucket_id, info in (quota.get("before") or {}).items()
        if isinstance(info, dict) and info.get("remaining_pct") is not None
    ]
    if not known:
        return "-"
    pct, bucket_id = min(known)
    return "{} {:.1f}%".format(bucket_id, pct)


def _agy_quota_after(
    quota: Dict[str, Any],
    cli: CLIStatus,
    model: Optional[str],
    cwd: Optional[str],
    run_ms: float,
) -> None:
    """Re-query after a long enough call and record what it spent (in place). Never raises.

    Runs whatever the call's outcome: a call that failed because the quota ran out
    should show that. Skipped when the preflight had no usable reading.
    """
    if quota.get("verdict") not in ("ok", "warn"):
        return
    if run_ms / 1000.0 < agy_quota.after_min_seconds():
        return
    buckets, reason = agy_quota.fetch_usage(cli.binary_path, cwd=cwd)
    if buckets is None:
        quota["after_note"] = reason
        return
    after = agy_quota.summarize(agy_quota.relevant_buckets(buckets, quota.get("group") or "", model))
    before = quota.get("before") or {}
    quota["after"] = after
    quota["used_pct"] = {
        bucket_id: round(before[bucket_id]["remaining_pct"] - info["remaining_pct"], 1)
        for bucket_id, info in after.items()
        if bucket_id in before
        and before[bucket_id].get("remaining_pct") is not None
        and info.get("remaining_pct") is not None
    }


def _build_claude_cmd(
    cli: CLIStatus,
    prompt: str,
    *,
    model: Optional[str] = None,
    working_dir: Optional[str] = None,
    max_budget_usd: Optional[float] = None,
    timeout: Optional[int] = None,
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
    timeout: Optional[int] = None,
) -> List[str]:
    """Build Codex CLI headless command.

    Sandbox mode is controlled by LLM_RELAY_CODEX_SANDBOX env var, read on
    every invocation so operators can change the setting without restarting
    the orchestrator process (e.g. when cli_delegate runs inside a long-lived
    MCP subprocess and the env is updated mid-session):
      - "workspace-write" (default): sandboxed, no shell access beyond workspace
      - "danger-full-access": full filesystem access, shell commands work
      - "none": --dangerously-bypass-approvals-and-sandbox (no sandbox at all)

    Users who need Codex to run gh, git, or read files outside the workspace
    should set LLM_RELAY_CODEX_SANDBOX=none.
    """
    sandbox = os.environ.get("LLM_RELAY_CODEX_SANDBOX", "workspace-write")
    cmd = [cli.binary_path, "exec", prompt, "--json", "--skip-git-repo-check"]
    if sandbox == "none":
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        cmd.extend(["--full-auto", "--sandbox", sandbox])
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
    timeout: Optional[int] = None,
) -> List[str]:
    """Build Gemini CLI headless command."""
    cmd = [cli.binary_path, "-p", prompt, "--output-format", "json", "-y"]
    if model:
        cmd.extend(["-m", model])
    return cmd


def _agy_effective_model(model: Optional[str]) -> Optional[str]:
    """Model an agy delegation runs with; shared by the command builder and the quota preflight.

    No model given: pin one instead of inheriting whatever the interactive agy session last
    selected (its /model choice is saved to settings.json and would silently change
    delegated calls). An empty LLM_RELAY_AGY_DEFAULT_MODEL leaves the choice to agy.
    """
    return model or os.environ.get("LLM_RELAY_AGY_DEFAULT_MODEL", _AGY_DEFAULT_MODEL)


def _build_antigravity_cmd(
    cli: CLIStatus,
    prompt: str,
    *,
    model: Optional[str] = None,
    working_dir: Optional[str] = None,
    max_budget_usd: Optional[float] = None,
    timeout: Optional[int] = None,
) -> List[str]:
    """Build Antigravity CLI (agy) headless command.

    When a positive timeout is given, --print-timeout is set to
    timeout - _AGY_PRINT_TIMEOUT_MARGIN_S (at least 1s) so agy returns its own
    error envelope before the subprocess is killed. agy start-up time is not
    counted by --print-timeout, which the margin also absorbs.

    Permission mode is controlled by LLM_RELAY_AGY_PERMISSIONS env var, read on
    every invocation so operators can change the setting without restarting
    the orchestrator process:
      - "read-only" (default, and any value other than "full"): pass
        _AGY_READ_ONLY_SETTINGS via --settings -- file/URL reads allowed,
        shell commands, file writes, URL execution and MCP tools denied
      - "full": --dangerously-skip-permissions (every tool call auto-approved)

    Without an explicit model the call uses LLM_RELAY_AGY_DEFAULT_MODEL (default
    "gemini-4-neon-xhigh"), read on every invocation; set it to an empty string to
    let agy use its own configured default.

    Read-only rules are the default because headless agy without explicit rules
    auto-denies any tool call that needs approval and then returns an empty
    response with status SUCCESS and exit code 0, losing the whole answer. With
    explicit deny rules the denied tool only returns an error to the model and
    the answer still comes back.
    """
    cmd = [cli.binary_path, "-p", prompt, "--output-format", "json"]
    if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > 0:
        print_timeout = max(timeout - _AGY_PRINT_TIMEOUT_MARGIN_S, 1)
        cmd.extend(["--print-timeout", "{}s".format(print_timeout)])
    permissions = os.environ.get("LLM_RELAY_AGY_PERMISSIONS", "read-only")
    if permissions == "full":
        cmd.append("--dangerously-skip-permissions")
    else:
        cmd.extend(["--settings", json.dumps(_AGY_READ_ONLY_SETTINGS, separators=(",", ":"))])
    model = _agy_effective_model(model)
    if model:
        cmd.extend(["--model", model])
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


def _parse_antigravity_output(stdout: str) -> Tuple[str, Optional[str]]:
    """Parse the agy JSON envelope into (output, failure_reason).

    failure_reason is None when the envelope is acceptable. A non-None reason
    marks the delegation as failed even when agy exited 0: a tool approval that
    is auto-denied in headless mode yields status SUCCESS with an empty
    response and a denied_actions list.
    """
    if not stdout.strip():
        return "", "empty output"
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return stdout.strip(), None
    if not isinstance(data, dict):
        return stdout.strip(), None

    response = data.get("response")
    response = "" if response is None else str(response)

    status = data.get("status")
    if status is not None and status != "SUCCESS":
        return response, str(data.get("error") or "status {}".format(status))

    if not response.strip():
        denied = data.get("denied_actions")
        if isinstance(denied, list) and denied:
            names = []
            for entry in denied:
                if isinstance(entry, dict):
                    name = entry.get("display_name") or entry.get("action")
                    if name:
                        names.append(str(name))
            reason = "tool permission auto-denied in headless mode ({}); no output produced".format(
                ", ".join(names),
            )
            return "", reason
        return "", "empty response"

    # A non-empty response is a success even if some tool calls were denied.
    return response, None


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
