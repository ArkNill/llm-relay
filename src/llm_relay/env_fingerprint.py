"""Environment fingerprint -- single-shot JSON snapshot of the user's LLM CLI environment.

Used by both onboarding paths:

  Path A (human-driven): `llm-relay env-fingerprint` shows the user what their
    environment looks like before running `init`.

  Path B (LLM-driven, primary): an agent (Claude Code / Codex / Gemini) runs
    `llm-relay env-fingerprint --json` and parses the structured output to
    decide which install / configure steps to take, without having to scrape
    the human-friendly `init` output.

This module is a pure collector -- it composes the existing probes
(setup_init._detect_clis, orch.discovery, recover.doctor, detect.scanner)
into one stable JSON schema. It does NOT change anything in the user's
environment; calling it is safe to run repeatedly.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Dict, List, Optional

# Schema version is bumped when the output shape changes in a way that breaks
# agents parsing earlier outputs. Patch-level additions (new fields) do not
# require a bump; agents should ignore unknown fields.
SCHEMA_VERSION = "1"

# Environment variables we surface (read-only) so agents can see what the
# user already configured without inspecting their shell rc files.
_RELEVANT_ENV_VARS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "GEMINI_API_KEY",
    "LLM_RELAY_DB",
    "LLM_RELAY_HISTORY",
    "LLM_RELAY_LANG",
    "LLM_TOKEN_CEILING",
)

# Ports the relay typically wants. Agents use this to spot conflicts before
# they try to `init`.
_DEFAULT_PROXY_PORTS = (8080, 8083)


def _safe_call(fn, default):
    """Run a probe function and swallow any exception, returning `default`.

    Fingerprint must never crash because of a sub-probe failure -- partial
    data with an error marker is more useful to a calling agent than no data.
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 -- the whole point is to capture any failure
        return {"_error": "{}: {}".format(type(exc).__name__, exc), "_value": default}


def _llm_relay_version() -> Optional[str]:
    try:
        return version("llm-relay")
    except PackageNotFoundError:
        return None


def _llm_relay_section() -> Dict[str, Any]:
    """Versions + on-disk paths the relay itself uses."""
    from llm_relay.setup_init import db_dir_for_env

    db_dir = db_dir_for_env()
    return {
        "version": _llm_relay_version(),
        "db_dir": str(db_dir),
        "db_path": str(db_dir / "usage.db"),
        "config_path": str(db_dir / "config.json"),
        "knowledge_dir": str(db_dir / "knowledge"),
    }


def _clis_section() -> List[Dict[str, Any]]:
    """Per-CLI install + auth + version + redacted proxy/config hints.

    Combines orch.discovery (which probes auth) with setup_init._detect_clis
    (which surfaces config_dir). Output is keyed by cli_id so agents can match
    against their own identity.
    """
    from llm_relay.orch.discovery import discover_all
    from llm_relay.setup_init import _detect_clis

    detect_by_id = {c["id"]: c for c in _detect_clis()}
    out: List[Dict[str, Any]] = []
    for status in discover_all():
        detect = detect_by_id.get(status.cli_id, {})
        config_dir = detect.get("config_dir")
        out.append({
            "id": status.cli_id,
            "binary_name": status.binary_name,
            "binary_path": status.binary_path,
            "installed": status.installed,
            "version": status.version,
            "auth": {
                "cli_authenticated": status.cli_authenticated,
                "api_key_env": status.api_key_name,
                "api_key_set": status.api_key_available,
                "preferred": status.preferred_auth.value,
            },
            "config_dir": config_dir,
            "config_dir_exists": Path(config_dir).is_dir() if config_dir else False,
        })
    return out


def _ports_section(ports: List[int]) -> Dict[str, str]:
    from llm_relay.setup_init import _is_port_in_use
    return {str(p): "in_use" if _is_port_in_use(p) else "free" for p in ports}


def _filesystem_section() -> Dict[str, Any]:
    """Project / session locations the relay knows about.

    Counts are bounded and cheap to compute; an agent should follow up with
    `llm-relay scan` for detail rather than trying to derive everything here.
    """
    from llm_relay.detect.scanner import discover_sessions, find_claude_home, find_projects_dir
    from llm_relay.setup_init import db_dir_for_env

    claude_home = find_claude_home()
    projects_dir = find_projects_dir()
    sessions = discover_sessions(projects_dir) if projects_dir.is_dir() else []
    db_dir = db_dir_for_env()
    return {
        "home": str(Path.home()),
        "claude_home": str(claude_home) if claude_home.exists() else None,
        "projects_dir": str(projects_dir) if projects_dir.is_dir() else None,
        "session_count": len(sessions),
        "knowledge_dir": str(db_dir / "knowledge"),
        "knowledge_dir_exists": (db_dir / "knowledge").is_dir(),
        "db_dir_exists": db_dir.is_dir(),
    }


def _env_section() -> Dict[str, Any]:
    """Surface relevant env vars. API keys are reported as set/unset only --
    never the actual value -- so the fingerprint can be safely pasted into a
    bug report.
    """
    out: Dict[str, Any] = {}
    for name in _RELEVANT_ENV_VARS:
        val = os.environ.get(name)
        if val is None:
            out[name] = None
            continue
        # Redact secret-shaped vars
        if name.endswith("_API_KEY"):
            out[name] = "set" if val else "empty"
        else:
            out[name] = val
    return out


def _doctor_section() -> Dict[str, Any]:
    """Run the existing read-only doctor checks and summarise.

    Agents typically don't need every check's full report inline; we return a
    summary plus per-check status. Agents wanting detail call `llm-relay
    doctor` separately.
    """
    from llm_relay.recover.doctor import run_doctor

    report = run_doctor(fix=False)
    checks_by_status: Dict[str, int] = {}
    items = []
    for result in report.results:
        checks_by_status[result.status] = checks_by_status.get(result.status, 0) + 1
        items.append({
            "name": result.name,
            "status": result.status,
            "detail": result.detail,
            "recommendation": result.recommendation or None,
        })
    return {
        "totals": checks_by_status,
        "checks": items,
    }


def collect_fingerprint(
    *,
    include_doctor: bool = True,
    ports: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Return the full environment fingerprint as a dict.

    Set `include_doctor=False` to skip the doctor checks when the caller only
    wants a fast install/version probe (~10x faster on a cold cache).
    """
    if ports is None:
        ports = list(_DEFAULT_PROXY_PORTS)

    snapshot: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "llm_relay": _safe_call(_llm_relay_section, {}),
        "clis": _safe_call(_clis_section, []),
        "ports": _safe_call(lambda: _ports_section(ports), {}),
        "filesystem": _safe_call(_filesystem_section, {}),
        "env": _safe_call(_env_section, {}),
    }
    if include_doctor:
        snapshot["doctor"] = _safe_call(_doctor_section, {"totals": {}, "checks": []})
    return snapshot
