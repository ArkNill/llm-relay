"""llm-relay init — one-command setup for new users.

Detects installed CLIs, configures Claude Code proxy + MCP, initializes DB,
starts the proxy, and verifies everything works.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

# ── Detection helpers ──


def _detect_clis() -> List[Dict[str, Any]]:
    """Detect installed AI CLI tools."""
    clis = []

    # Claude Code
    cc_bin = shutil.which("claude")
    cc_dir = Path.home() / ".claude"
    if cc_bin or cc_dir.exists():
        version = None
        if cc_bin:
            try:
                out = subprocess.run(
                    [cc_bin, "--version"],
                    capture_output=True, text=True, timeout=10,
                    stdin=subprocess.DEVNULL,
                )
                version = out.stdout.strip().split("\n")[0] if out.returncode == 0 else None
            except Exception:
                pass
        clis.append({
            "id": "claude-code",
            "name": "Claude Code",
            "installed": True,
            "binary": cc_bin,
            "config_dir": str(cc_dir),
            "version": version,
        })

    # Codex CLI
    codex_bin = shutil.which("codex")
    codex_dir = Path.home() / ".codex"
    if codex_bin or codex_dir.exists():
        clis.append({
            "id": "openai-codex",
            "name": "Codex CLI",
            "installed": True,
            "binary": codex_bin,
            "config_dir": str(codex_dir),
        })

    # Gemini CLI
    gemini_bin = shutil.which("gemini")
    gemini_dir = Path.home() / ".gemini"
    if gemini_bin or gemini_dir.exists():
        clis.append({
            "id": "gemini-cli",
            "name": "Gemini CLI",
            "installed": True,
            "binary": gemini_bin,
            "config_dir": str(gemini_dir),
        })

    return clis


def _is_port_in_use(port: int) -> bool:
    """Check if a TCP port is already bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _find_available_port(start: int = 8083) -> int:
    """Find the first available port starting from `start`."""
    for port in range(start, start + 20):
        if not _is_port_in_use(port):
            return port
    return start


# ── Configuration helpers ──


def _read_json(path: Path) -> Dict[str, Any]:
    """Read a JSON file, returning empty dict if missing or malformed."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    """Write JSON with 2-space indent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _configure_claude_code(port: int, dry_run: bool = False) -> List[str]:
    """Configure Claude Code to use llm-relay proxy + MCP.

    Merges into existing settings.json without overwriting other keys.
    Returns a list of actions taken (for display).
    """
    settings_path = Path.home() / ".claude" / "settings.json"
    actions = []

    if not settings_path.parent.exists():
        if dry_run:
            actions.append("[dry-run] Would create ~/.claude/")
        else:
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            actions.append("Created ~/.claude/")

    settings = _read_json(settings_path)
    changed = False

    # 1. Set ANTHROPIC_BASE_URL in env
    env = settings.get("env", {})
    if not isinstance(env, dict):
        env = {}
    base_url = "http://localhost:{}".format(port)
    if env.get("ANTHROPIC_BASE_URL") != base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
        settings["env"] = env
        changed = True
        actions.append("Set ANTHROPIC_BASE_URL={}".format(base_url))
    else:
        actions.append("ANTHROPIC_BASE_URL already set (skipped)")

    # 2. Register MCP server
    mcp_servers = settings.get("mcpServers", {})
    if not isinstance(mcp_servers, dict):
        mcp_servers = {}
    mcp_entry = {"command": "llm-relay-mcp", "type": "stdio"}
    if "llm-relay" not in mcp_servers:
        mcp_servers["llm-relay"] = mcp_entry
        settings["mcpServers"] = mcp_servers
        changed = True
        actions.append("Registered llm-relay MCP server (8 tools)")
    else:
        actions.append("MCP server already registered (skipped)")

    if changed and not dry_run:
        _write_json(settings_path, settings)

    return actions


# ── DB initialization ──


def _init_db(db_dir: Path) -> str:
    """Initialize the SQLite database."""
    db_path = db_dir / "usage.db"
    if db_path.exists():
        size = db_path.stat().st_size
        return "DB exists ({} bytes)".format(size)

    db_dir.mkdir(parents=True, exist_ok=True)
    try:
        from llm_relay.proxy.db import get_conn
        conn = get_conn(db_path)
        conn.close()
        return "DB created at {}".format(db_path)
    except ImportError:
        return "DB directory created (proxy module not installed for schema init)"


# ── Config file ──


def _write_config(db_dir: Path, port: int) -> str:
    """Write a minimal config file for reference."""
    config_path = db_dir / "config.json"
    if config_path.exists():
        return "Config exists (skipped)"

    config = {
        "port": port,
        "history": True,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _write_json(config_path, config)
    return "Config written to {}".format(config_path)


# ── Server management ──


def _start_server(port: int) -> Tuple[bool, str]:
    """Start the proxy server in background."""
    if _is_port_in_use(port):
        # Verify it's llm-relay
        try:
            import urllib.request
            resp = urllib.request.urlopen(
                "http://localhost:{}/_health".format(port), timeout=3,
            )
            data = json.loads(resp.read())
            if data.get("status") == "ok":
                return True, "Already running on port {}".format(port)
        except Exception:
            pass
        return False, "Port {} is in use by another process".format(port)

    # Start uvicorn in background
    try:
        env = os.environ.copy()
        env["LLM_RELAY_HISTORY"] = "1"

        log_path = db_dir_for_env() / "server.log"
        log_file = open(str(log_path), "a")  # noqa: SIM115

        # Detach the server process so it survives parent exit.
        # Windows: CREATE_NEW_PROCESS_GROUP; POSIX: start_new_session (setsid).
        detach_kwargs = {}  # type: dict
        if sys.platform == "win32":
            detach_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            detach_kwargs["start_new_session"] = True

        proc = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn",
                "llm_relay.proxy.proxy:app",
                "--host", "0.0.0.0",
                "--port", str(port),
                "--log-level", "info",
            ],
            env=env,
            stdout=log_file,
            stderr=log_file,
            stdin=subprocess.DEVNULL,
            **detach_kwargs,
        )

        # Wait for startup
        for _ in range(20):
            time.sleep(0.5)
            if _is_port_in_use(port):
                return True, "Started on port {} (PID {})".format(port, proc.pid)

        return False, "Server started but not responding (check {})".format(log_path)
    except FileNotFoundError:
        return False, "uvicorn not found. Run: pip install llm-relay[proxy]"
    except Exception as e:
        return False, "Failed to start: {}".format(e)


def db_dir_for_env() -> Path:
    """Return the DB directory from env or default."""
    env_db = os.getenv("LLM_RELAY_DB")
    if env_db:
        return Path(env_db).parent
    return Path.home() / ".llm-relay"


# ── Health check ──


def _health_check(port: int) -> Tuple[bool, Dict[str, Any]]:
    """Run a comprehensive health check against the running server."""
    results = {}
    base = "http://localhost:{}".format(port)

    endpoints = [
        ("/_health", "proxy"),
        ("/api/v1/health", "api"),
        ("/api/v1/quota", "quota"),
        ("/api/v1/errors", "errors"),
        ("/api/v1/cache", "cache"),
        ("/api/v1/ttl", "ttl"),
    ]

    all_ok = True
    for path, name in endpoints:
        try:
            import urllib.request
            resp = urllib.request.urlopen(base + path, timeout=5)
            json.loads(resp.read())  # validate JSON response
            results[name] = {"ok": True, "status": resp.status}
        except Exception as e:
            results[name] = {"ok": False, "error": str(e)}
            all_ok = False

    return all_ok, results


# ── Main init function ──


def run_init(
    port: int = 8083,
    skip_server: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
) -> Dict[str, Any]:
    """Run the full initialization sequence.

    Returns a summary dict with all results.
    """
    summary = {
        "version": None,
        "clis": [],
        "db": None,
        "config": None,
        "claude_code": [],
        "server": None,
        "health": None,
        "port": port,
        "urls": {},
    }

    try:
        from llm_relay import __version__
        summary["version"] = __version__
    except ImportError:
        summary["version"] = "unknown"

    # Step 1: Detect CLIs
    summary["clis"] = _detect_clis()

    # Step 2: Find port
    if _is_port_in_use(port):
        # Check if it's already llm-relay
        try:
            import urllib.request
            resp = urllib.request.urlopen(
                "http://localhost:{}/_health".format(port), timeout=3,
            )
            data = json.loads(resp.read())
            if data.get("status") == "ok":
                summary["server"] = "Already running on port {}".format(port)
            else:
                port = _find_available_port(port + 1)
                summary["port"] = port
        except Exception:
            port = _find_available_port(port + 1)
            summary["port"] = port

    # Step 3: Initialize DB
    db_dir = db_dir_for_env()
    if not dry_run:
        summary["db"] = _init_db(db_dir)
    else:
        summary["db"] = "[dry-run] Would init DB at {}".format(db_dir / "usage.db")

    # Step 4: Write config
    if not dry_run:
        summary["config"] = _write_config(db_dir, port)
    else:
        summary["config"] = "[dry-run] Would write config"

    # Step 5: Configure Claude Code
    has_cc = any(c["id"] == "claude-code" for c in summary["clis"])
    if has_cc:
        summary["claude_code"] = _configure_claude_code(port, dry_run=dry_run)
    else:
        summary["claude_code"] = ["Claude Code not detected (skipped)"]

    # Step 6: Start server
    if not skip_server and not dry_run:
        ok, msg = _start_server(port)
        summary["server"] = msg
        if not ok:
            summary["health"] = "Skipped (server not running)"
            summary["urls"] = {}
            return summary
    elif dry_run:
        summary["server"] = "[dry-run] Would start server on port {}".format(port)

    # Step 7: Health check
    if not skip_server and not dry_run:
        all_ok, results = _health_check(port)
        summary["health"] = results
    else:
        summary["health"] = "Skipped"

    # Step 8: URLs
    summary["urls"] = {
        "dashboard": "http://localhost:{}/dashboard/".format(port),
        "display": "http://localhost:{}/display/".format(port),
        "history": "http://localhost:{}/history/".format(port),
        "proxy": "http://localhost:{}".format(port),
    }

    return summary
