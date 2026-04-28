"""Doctor -- 7 health checks for Claude Code configuration and session integrity.

Absorbs health check patterns from cozempic's doctor module, adapted for
llm-relay's zero-dep core philosophy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class HealthResult:
    name: str
    status: str  # "ok", "warning", "issue"
    detail: str
    recommendation: str = ""
    fixable: bool = False


@dataclass
class DoctorReport:
    results: list[HealthResult] = field(default_factory=list)

    @property
    def issues(self) -> list[HealthResult]:
        return [r for r in self.results if r.status == "issue"]

    @property
    def warnings(self) -> list[HealthResult]:
        return [r for r in self.results if r.status == "warning"]


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _claude_json_path() -> Path:
    return Path.home() / ".claude.json"


def _claude_dir() -> Path:
    return Path.home() / ".claude"


def check_trust_dialog_hang() -> HealthResult:
    """Check for hasTrustDialogAccepted stuck state (Windows-origin bug)."""
    path = _claude_json_path()
    if not path.exists():
        return HealthResult("trust-dialog-hang", "ok", "No .claude.json found")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return HealthResult(
            "trust-dialog-hang", "issue",
            ".claude.json is corrupted -- cannot check trust dialog",
            fixable=False,
        )

    if data.get("hasTrustDialogAccepted") is True:
        return HealthResult(
            "trust-dialog-hang", "warning",
            "hasTrustDialogAccepted=true -- can cause resume hangs on some setups",
            recommendation=(
                "Set hasTrustDialogAccepted to false in ~/.claude.json"
                " (or %USERPROFILE%\\.claude.json on Windows)"
            ),
            fixable=True,
        )
    return HealthResult("trust-dialog-hang", "ok", "Trust dialog state is clean")


def check_hooks_trust_flag() -> HealthResult:
    """Check for missing hasTrustDialogHooksAccepted (v2.1.51+ bug)."""
    path = _claude_json_path()
    if not path.exists():
        return HealthResult("hooks-trust-flag", "ok", "No .claude.json found")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return HealthResult("hooks-trust-flag", "issue", ".claude.json corrupted", fixable=False)

    if "hasTrustDialogHooksAccepted" not in data:
        return HealthResult(
            "hooks-trust-flag", "warning",
            "hasTrustDialogHooksAccepted missing -- hooks may be silently blocked",
            recommendation=(
                "Add hasTrustDialogHooksAccepted: true to ~/.claude.json"
                " (or %USERPROFILE%\\.claude.json on Windows)"
            ),
            fixable=True,
        )
    return HealthResult("hooks-trust-flag", "ok", "Hooks trust flag present")


def check_claude_json_corruption() -> HealthResult:
    """Check .claude.json for corruption: truncation, null bytes, invalid JSON."""
    path = _claude_json_path()
    if not path.exists():
        return HealthResult("claude-json-corruption", "ok", "No .claude.json found")

    try:
        raw = path.read_bytes()
    except OSError as e:
        return HealthResult("claude-json-corruption", "issue", f"Cannot read: {e}")

    if b"\x00" in raw:
        return HealthResult(
            "claude-json-corruption", "issue",
            "Null bytes found in .claude.json ({} occurrences)".format(raw.count(b'\x00')),
            recommendation="Restore from backup: ~/.claude.json.bak",
            fixable=True,
        )

    try:
        json.loads(raw)
    except json.JSONDecodeError as e:
        return HealthResult(
            "claude-json-corruption", "issue",
            f"Invalid JSON: {e}",
            recommendation="Restore from backup: ~/.claude.json.bak",
            fixable=True,
        )

    return HealthResult("claude-json-corruption", "ok", "JSON structure valid")


def check_corrupted_tool_use() -> HealthResult:
    """Check latest sessions for corrupted tool_use blocks (name >200 chars)."""
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return HealthResult("corrupted-tool-use", "ok", "No sessions found")

    # Check 3 most recent sessions
    candidates = sorted(base.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]
    corrupted_count = 0
    checked = 0

    for path in candidates:
        checked += 1
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    inner = msg.get("message", {})
                    content = inner.get("content", []) if isinstance(inner, dict) else []
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            name = block.get("name", "")
                            if len(name) > 200:
                                corrupted_count += 1
        except OSError:
            continue

    if corrupted_count > 0:
        return HealthResult(
            "corrupted-tool-use", "issue",
            f"{corrupted_count} corrupted tool_use blocks found (name >200 chars) "
            f"in {checked} recent sessions. Likely a serialization bug.",
            recommendation="Run pruner to fix: llm-relay prune --execute",
            fixable=True,
        )
    return HealthResult("corrupted-tool-use", "ok", f"No corrupted tool_use in {checked} sessions")


def check_orphaned_tool_results() -> HealthResult:
    """Quick orphan check on the latest session."""
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return HealthResult("orphaned-tool-results", "ok", "No sessions found")

    candidates = sorted(base.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return HealthResult("orphaned-tool-results", "ok", "No sessions found")

    path = candidates[0]
    uses: set[str] = set()
    results: set[str] = set()

    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                inner = msg.get("message", {})
                content = inner.get("content", []) if isinstance(inner, dict) else []
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        tid = block.get("id", "")
                        if tid:
                            uses.add(tid)
                    elif block.get("type") == "tool_result":
                        tid = block.get("tool_use_id", "")
                        if tid:
                            results.add(tid)
    except OSError:
        return HealthResult("orphaned-tool-results", "ok", "Cannot read latest session")

    orphan_results = results - uses
    orphan_uses = uses - results

    if orphan_results:
        return HealthResult(
            "orphaned-tool-results", "warning",
            f"{len(orphan_results)} orphaned tool_result blocks in latest session. "
            f"May cause 400 errors on --resume.",
            recommendation="Run llm-relay doctor --fix or prune the session.",
            fixable=True,
        )

    if len(orphan_uses) > 5:
        return HealthResult(
            "orphaned-tool-results", "warning",
            f"{len(orphan_uses)} tool_use blocks without results in latest session.",
            recommendation="These may be from interrupted or timed-out tool calls.",
        )

    return HealthResult("orphaned-tool-results", "ok", "Tool call chains intact")


def check_zombie_sessions() -> HealthResult:
    """Check for stale session JSONL files (>7 days old, >10 MB)."""
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return HealthResult("zombie-sessions", "ok", "No sessions directory")

    import time
    now = time.time()
    week_ago = now - 7 * 86400
    zombies: list[tuple[str, float]] = []

    for path in base.rglob("*.jsonl"):
        try:
            st = path.stat()
            if st.st_mtime < week_ago and st.st_size > 10 * 1024 * 1024:
                zombies.append((str(path), st.st_size / 1024 / 1024))
        except OSError:
            continue

    if zombies:
        return HealthResult(
            "zombie-sessions", "warning",
            f"{len(zombies)} old session files >10 MB found (>7 days). "
            f"Total: {sum(s for _, s in zombies):.0f} MB.",
            recommendation="Consider archiving or deleting old sessions.",
        )
    return HealthResult("zombie-sessions", "ok", "No zombie sessions")


def check_relay_health() -> HealthResult:
    """Check if llm-relay proxy is running and DB is accessible."""
    import subprocess
    import sys

    # Check if proxy process is running (cross-platform)
    proxy_running = False
    if sys.platform == "win32":
        # Windows: use tasklist to find uvicorn/python running llm_relay
        try:
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV"],
                capture_output=True, text=True, timeout=5,
                stdin=subprocess.DEVNULL,
            )
            if result.returncode == 0 and "python" in result.stdout.lower():
                wmic = subprocess.run(
                    ["wmic", "process", "where", "name='python.exe'", "get", "commandline"],
                    capture_output=True, text=True, timeout=5,
                    stdin=subprocess.DEVNULL,
                )
                proxy_running = "llm_relay" in wmic.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            proxy_running = False
    else:
        try:
            result = subprocess.run(
                ["pgrep", "-f", "llm_relay"],
                capture_output=True, timeout=5,
                stdin=subprocess.DEVNULL,
            )
            proxy_running = result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            proxy_running = False

    db_path = Path.home() / ".llm-relay" / "usage.db"
    db_exists = db_path.exists()

    if proxy_running and db_exists:
        return HealthResult("relay-health", "ok", "llm-relay proxy running, DB accessible")
    elif not proxy_running and db_exists:
        return HealthResult(
            "relay-health", "warning",
            "llm-relay proxy not running but DB exists",
            recommendation="Start with: llm-relay serve",
        )
    elif not db_exists:
        return HealthResult(
            "relay-health", "warning",
            "llm-relay DB not found -- proxy may not have been set up",
            recommendation="Set up with: pip install llm-relay && llm-relay serve",
        )
    return HealthResult("relay-health", "ok", "No issues")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

ALL_CHECKS = [
    check_trust_dialog_hang,
    check_hooks_trust_flag,
    check_claude_json_corruption,
    check_corrupted_tool_use,
    check_orphaned_tool_results,
    check_zombie_sessions,
    check_relay_health,
]


def run_doctor(fix: bool = False) -> DoctorReport:
    """Run all health checks and return a report."""
    report = DoctorReport()

    for check_fn in ALL_CHECKS:
        result = check_fn()
        report.results.append(result)

    # TODO: implement --fix for fixable issues
    return report
