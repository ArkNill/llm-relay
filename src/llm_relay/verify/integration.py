"""verify integration -- confirm a target LLM CLI is wired to the relay.

For each CLI we check (a) the binary is on PATH, (b) its config file is
present and parseable, and (c) the relay-specific settings are in place
(ANTHROPIC_BASE_URL, MCP server registration, etc.).

CLIs that aren't installed produce `skipped` checks rather than `fail`,
since the user may legitimately use only one CLI.

Known limitations preserved as `warn`s:
  - Gemini CLI oauth-personal hits an upstream 403 (#25425); we surface
    this as a known-issue warning rather than a failure.
  - Codex CLI does not have a stable proxy-routing knob yet; the
    proxy_route check is `skipped` for it.

Each CLI runs in its own sub-report; `verify_integration("all")` aggregates
them via `verify.aggregate()`, so check IDs collide-free.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from llm_relay.setup_init import _read_json
from llm_relay.verify import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIPPED,
    STATUS_WARN,
    VerifyCheck,
    VerifyReport,
    aggregate,
    run_check,
)

# (cli_id, binary_name, config_path_relative_to_home)
_CLI_REGISTRY: List[Tuple[str, str, str]] = [
    ("claude-code", "claude", ".claude/settings.json"),
    ("openai-codex", "codex", ".codex/config.toml"),
    ("gemini-cli", "gemini", ".gemini"),
]

_REGISTRY_BY_ID = {row[0]: row for row in _CLI_REGISTRY}

ALL_CLIS = "all"


def _check_binary(binary_name: str) -> VerifyCheck:
    path = shutil.which(binary_name)
    if path:
        return VerifyCheck(
            id="", label="",
            status=STATUS_PASS,
            detail="{} on PATH at {}".format(binary_name, path),
            data={"path": path},
        )
    return VerifyCheck(
        id="", label="",
        status=STATUS_FAIL,
        detail="{} binary not found on PATH".format(binary_name),
        remediation="Install {} (see vendor docs).".format(binary_name),
    )


def _check_config_present(label_path: str, abs_path: Path) -> VerifyCheck:
    if abs_path.exists():
        return VerifyCheck(
            id="", label="",
            status=STATUS_PASS,
            detail="{} present at {}".format(label_path, abs_path),
            data={"path": str(abs_path)},
        )
    return VerifyCheck(
        id="", label="",
        status=STATUS_FAIL,
        detail="{} not found at {}".format(label_path, abs_path),
        remediation="Launch the CLI once to let it create its config, then re-run.",
        data={"path": str(abs_path)},
    )


# ── claude-code ─────────────────────────────────────────────────────────────


def _claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _claude_check_settings_present() -> VerifyCheck:
    settings_path = _claude_settings_path()
    check = _check_config_present("Claude settings.json", settings_path)
    if check.status != STATUS_PASS:
        return check
    # Additionally confirm parseability
    try:
        _read_json(settings_path)
    except Exception as exc:  # noqa: BLE001
        return VerifyCheck(
            id="", label="",
            status=STATUS_FAIL,
            detail="settings.json present but unparseable: {}".format(exc),
            remediation=(
                "Repair the JSON manually, or delete settings.json and let "
                "Claude Code regenerate it (your hooks/permissions will reset)."
            ),
            data={"path": str(settings_path)},
        )
    return check


def _claude_check_proxy_route() -> VerifyCheck:
    settings_path = _claude_settings_path()
    if not settings_path.is_file():
        return VerifyCheck(
            id="", label="",
            status=STATUS_SKIPPED,
            detail="settings.json missing (see claude_settings_present)",
        )
    settings = _read_json(settings_path)
    env = settings.get("env", {}) if isinstance(settings, dict) else {}
    base_url = env.get("ANTHROPIC_BASE_URL") if isinstance(env, dict) else None
    env_base_url = os.environ.get("ANTHROPIC_BASE_URL")
    effective = env_base_url or base_url
    if effective and ("localhost" in effective or "127.0.0.1" in effective):
        return VerifyCheck(
            id="", label="",
            status=STATUS_PASS,
            detail="ANTHROPIC_BASE_URL routes to local relay: {}".format(effective),
            data={"source": "env" if env_base_url else "settings.json", "value": effective},
        )
    if effective:
        return VerifyCheck(
            id="", label="",
            status=STATUS_WARN,
            detail="ANTHROPIC_BASE_URL set but not local: {}".format(effective),
            remediation=(
                "Run `llm-relay init` or set ANTHROPIC_BASE_URL=http://localhost:8083 "
                "if you want Claude Code to route through llm-relay."
            ),
            data={"source": "env" if env_base_url else "settings.json", "value": effective},
        )
    return VerifyCheck(
        id="", label="",
        status=STATUS_FAIL,
        detail="ANTHROPIC_BASE_URL not configured",
        remediation="Run `llm-relay init` to wire Claude Code through the relay.",
    )


def _claude_check_mcp_registered() -> VerifyCheck:
    settings_path = _claude_settings_path()
    if not settings_path.is_file():
        return VerifyCheck(
            id="", label="",
            status=STATUS_SKIPPED,
            detail="settings.json missing (see claude_settings_present)",
        )
    settings = _read_json(settings_path)
    mcp = settings.get("mcpServers", {}) if isinstance(settings, dict) else {}
    if isinstance(mcp, dict) and "llm-relay" in mcp:
        entry = mcp["llm-relay"]
        return VerifyCheck(
            id="", label="",
            status=STATUS_PASS,
            detail="llm-relay MCP server registered",
            data={"entry": entry},
        )
    return VerifyCheck(
        id="", label="",
        status=STATUS_FAIL,
        detail="llm-relay MCP server not registered in settings.json",
        remediation="Run `llm-relay init` (registers llm-relay-mcp as a stdio server).",
    )


def _claude_checks() -> List[Tuple[str, str, Callable[[], VerifyCheck]]]:
    return [
        ("binary", "claude binary on PATH", lambda: _check_binary("claude")),
        ("settings_present", "~/.claude/settings.json present and parseable",
         _claude_check_settings_present),
        ("proxy_route", "ANTHROPIC_BASE_URL routes to local relay",
         _claude_check_proxy_route),
        ("mcp_server", "llm-relay MCP server registered in settings.json",
         _claude_check_mcp_registered),
    ]


# ── openai-codex ────────────────────────────────────────────────────────────


def _codex_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def _codex_check_config_present() -> VerifyCheck:
    return _check_config_present("Codex config.toml", _codex_config_path())


def _codex_check_proxy_route() -> VerifyCheck:
    """Codex doesn't yet have a stable proxy-routing knob exposed via config,
    so we surface this as `skipped` with a note so agents don't try to wire
    Codex into the relay until upstream support lands.
    """
    return VerifyCheck(
        id="", label="",
        status=STATUS_SKIPPED,
        detail=(
            "Codex CLI does not currently expose a stable proxy-routing setting; "
            "relay-routed Codex calls are out of scope for this check."
        ),
        data={"reason": "upstream"},
    )


def _codex_checks() -> List[Tuple[str, str, Callable[[], VerifyCheck]]]:
    return [
        ("binary", "codex binary on PATH", lambda: _check_binary("codex")),
        ("config_present", "~/.codex/config.toml present", _codex_check_config_present),
        ("proxy_route", "Codex proxy routing (upstream limitation)", _codex_check_proxy_route),
    ]


# ── gemini-cli ──────────────────────────────────────────────────────────────


def _gemini_dir() -> Path:
    return Path.home() / ".gemini"


def _gemini_check_dir_present() -> VerifyCheck:
    return _check_config_present("~/.gemini directory", _gemini_dir())


def _gemini_check_oauth_known_issue() -> VerifyCheck:
    """We always surface the known oauth-personal 403 bug as a `warn` so the
    operator/agent knows to fall back to GEMINI_API_KEY if they hit it.
    """
    return VerifyCheck(
        id="", label="",
        status=STATUS_WARN,
        detail=(
            "Gemini CLI oauth-personal has a known 403 server-side bug "
            "(google-gemini/gemini-cli#25425). Use GEMINI_API_KEY to bypass."
        ),
        remediation="export GEMINI_API_KEY=<your key>  # or use a service-account flow",
        data={"upstream_issue": "google-gemini/gemini-cli#25425"},
    )


def _gemini_checks() -> List[Tuple[str, str, Callable[[], VerifyCheck]]]:
    return [
        ("binary", "gemini binary on PATH", lambda: _check_binary("gemini")),
        ("config_dir_present", "~/.gemini directory present", _gemini_check_dir_present),
        ("oauth_known_issue", "oauth-personal upstream 403 known-issue note",
         _gemini_check_oauth_known_issue),
    ]


# ── proxy_reachable (optional, --live) ──────────────────────────────────────


def _live_check_proxy_reachable(port: int) -> VerifyCheck:
    """Hit the relay's /_health endpoint to confirm the proxy is actually
    answering on `port`. Optional because it requires httpx and a running
    server.
    """
    try:
        import httpx  # type: ignore[import-not-found]
    except ImportError:
        return VerifyCheck(
            id="", label="",
            status=STATUS_SKIPPED,
            detail="httpx not installed; pass [proxy] extra to enable --live check",
        )
    url = "http://127.0.0.1:{}/_health".format(port)
    try:
        resp = httpx.get(url, timeout=2.0)
    except httpx.HTTPError as exc:
        return VerifyCheck(
            id="", label="",
            status=STATUS_FAIL,
            detail="proxy /_health unreachable at {}: {}".format(url, exc),
            remediation="Start the relay (`llm-relay serve --port {}`).".format(port),
            data={"url": url},
        )
    if resp.status_code == 200:
        return VerifyCheck(
            id="", label="",
            status=STATUS_PASS,
            detail="proxy /_health responded 200 at {}".format(url),
            data={"url": url, "status_code": 200},
        )
    return VerifyCheck(
        id="", label="",
        status=STATUS_FAIL,
        detail="proxy /_health returned status {} at {}".format(resp.status_code, url),
        remediation="Inspect server logs (`journalctl --user -u llm-relay-api`).",
        data={"url": url, "status_code": resp.status_code},
    )


# ── Dispatch ────────────────────────────────────────────────────────────────


_CHECKS_BY_CLI: Dict[str, Callable[[], List[Tuple[str, str, Callable[[], VerifyCheck]]]]] = {
    "claude-code": _claude_checks,
    "openai-codex": _codex_checks,
    "gemini-cli": _gemini_checks,
}


def _verify_one_cli(cli_id: str, *, live: bool, port: int) -> VerifyReport:
    if cli_id not in _CHECKS_BY_CLI:
        raise ValueError("Unknown cli_id {!r}; expected one of {}".format(
            cli_id, sorted(list(_CHECKS_BY_CLI.keys()) + [ALL_CLIS]),
        ))
    binary_name = _REGISTRY_BY_ID[cli_id][1]
    binary_present = shutil.which(binary_name) is not None
    report = VerifyReport(target=cli_id)

    if not binary_present:
        # When the binary isn't installed, every check for this CLI is
        # `skipped` -- the user may not use this CLI at all.
        for check_id, label, _ in _CHECKS_BY_CLI[cli_id]():
            report.checks.append(VerifyCheck(
                id=check_id,
                label=label,
                status=STATUS_SKIPPED,
                detail="{} not installed".format(binary_name),
            ))
        return report

    for check_id, label, fn in _CHECKS_BY_CLI[cli_id]():
        report.checks.append(run_check(check_id, label, fn))

    if live:
        report.checks.append(
            run_check(
                "proxy_reachable_live",
                "proxy /_health responds on configured port",
                lambda: _live_check_proxy_reachable(port),
            )
        )
    return report


def verify_integration(
    cli_id: Optional[str] = ALL_CLIS,
    *,
    live: bool = False,
    port: int = 8083,
) -> VerifyReport:
    """Verify a single CLI or all of them.

    `cli_id="all"` aggregates per-CLI sub-reports into one combined report;
    each check ID is namespaced as `{cli_id}.{original_id}`.
    """
    if cli_id is None:
        cli_id = ALL_CLIS
    if cli_id == ALL_CLIS:
        sub_reports = [
            _verify_one_cli(c, live=live, port=port)
            for c in _CHECKS_BY_CLI
        ]
        return aggregate("integration", sub_reports)
    return _verify_one_cli(cli_id, live=live, port=port)


__all__ = ["verify_integration", "ALL_CLIS"]
