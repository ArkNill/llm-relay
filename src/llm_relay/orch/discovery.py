"""CLI binary discovery and authentication probing — stdlib only."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from typing import List, Optional, Tuple

from llm_relay.orch.models import AuthMethod, CLIStatus

logger = logging.getLogger(__name__)

# (cli_id, binary_name, api_key_env_var)
_CLI_REGISTRY: List[Tuple[str, str, str]] = [
    ("claude-code", "claude", "ANTHROPIC_API_KEY"),
    ("openai-codex", "codex", "OPENAI_API_KEY"),
    ("gemini-cli", "gemini", "GEMINI_API_KEY"),
]

_PROBE_TIMEOUT = int(os.environ.get("LLM_RELAY_ORCH_PROBE_TIMEOUT", "10"))

# Process-level cache
_cache: Optional[List[CLIStatus]] = None


def discover_all() -> List[CLIStatus]:
    """Discover all registered CLIs. Cached per-process."""
    global _cache
    if _cache is not None:
        return list(_cache)
    _cache = [_discover_one(cli_id, binary, api_key_env) for cli_id, binary, api_key_env in _CLI_REGISTRY]
    return list(_cache)


def refresh() -> List[CLIStatus]:
    """Clear cache and re-discover all CLIs."""
    global _cache
    _cache = None
    return discover_all()


def get_available(require_auth: bool = True) -> List[CLIStatus]:
    """Return CLIs that are installed and optionally authenticated."""
    results = discover_all()
    if require_auth:
        return [s for s in results if s.is_usable()]
    return [s for s in results if s.installed]


def _discover_one(cli_id: str, binary_name: str, api_key_env: str) -> CLIStatus:
    """Probe a single CLI for installation and auth status."""
    binary_path = shutil.which(binary_name)
    installed = binary_path is not None
    api_key_available = bool(os.environ.get(api_key_env, ""))
    cli_authenticated = False
    version: Optional[str] = None

    if installed:
        version = _get_version(binary_path, binary_name)
        cli_authenticated = _probe_auth(cli_id, binary_path)

    # Determine preferred auth method
    if cli_authenticated:
        preferred_auth = AuthMethod.CLI_OAUTH
    elif api_key_available:
        preferred_auth = AuthMethod.API_KEY
    else:
        preferred_auth = AuthMethod.NONE

    status = CLIStatus(
        cli_id=cli_id,
        binary_name=binary_name,
        binary_path=binary_path,
        installed=installed,
        cli_authenticated=cli_authenticated,
        api_key_name=api_key_env,
        api_key_available=api_key_available,
        preferred_auth=preferred_auth,
        version=version,
    )
    logger.debug(
        "Discovered %s: installed=%s, auth=%s, preferred=%s",
        cli_id, installed, cli_authenticated, preferred_auth.value,
    )
    return status


def _get_version(binary_path: str, binary_name: str) -> Optional[str]:
    """Get CLI version string."""
    try:
        result = subprocess.run(
            [binary_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        output = result.stdout.strip() or result.stderr.strip()
        if output:
            return output.splitlines()[0]
    except (subprocess.TimeoutExpired, OSError, ValueError):
        logger.debug("Failed to get version for %s", binary_name)
    return None


def _probe_auth(cli_id: str, binary_path: str) -> bool:
    """Probe whether a CLI is authenticated by attempting a minimal headless call."""
    probers = {
        "claude-code": _probe_claude,
        "openai-codex": _probe_codex,
        "gemini-cli": _probe_gemini,
    }
    prober = probers.get(cli_id)
    if prober is None:
        return False
    try:
        return prober(binary_path)
    except Exception:
        logger.debug("Auth probe failed for %s", cli_id, exc_info=True)
        return False


def _probe_claude(binary_path: str) -> bool:
    """Check Claude Code auth by looking for OAuth credentials."""
    # Instead of running a headless call (expensive), check for auth state
    claude_dir = os.path.expanduser("~/.claude")
    if not os.path.isdir(claude_dir):
        return False
    # Claude Code stores OAuth state; presence of projects dir indicates active auth
    projects_dir = os.path.join(claude_dir, "projects")
    return os.path.isdir(projects_dir)


def _probe_codex(binary_path: str) -> bool:
    """Check Codex auth by looking for auth.json."""
    auth_file = os.path.expanduser("~/.codex/auth.json")
    if not os.path.isfile(auth_file):
        return False
    try:
        with open(auth_file) as f:
            content = f.read().strip()
            return len(content) > 10  # Non-empty auth file
    except OSError:
        return False


def _probe_gemini(binary_path: str) -> bool:
    """Check Gemini CLI auth by looking for OAuth credentials."""
    oauth_file = os.path.expanduser("~/.gemini/oauth_creds.json")
    return os.path.isfile(oauth_file)
