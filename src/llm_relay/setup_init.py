"""llm-relay init — diagnostic check and Docker setup guide.

Detects installed CLIs, checks Docker status, and prints instructions
for running llm-relay via Docker. Does NOT modify CC settings or start
native servers.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

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


def _check_docker() -> Dict[str, Any]:
    """Check if Docker is installed and running."""
    result = {"installed": False, "running": False, "compose": False, "version": None}

    docker_bin = shutil.which("docker")
    if not docker_bin:
        return result
    result["installed"] = True

    try:
        out = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=5,
            stdin=subprocess.DEVNULL,
        )
        if out.returncode == 0 and out.stdout.strip():
            result["running"] = True
            result["version"] = out.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass

    try:
        out = subprocess.run(
            ["docker", "compose", "version", "--short"],
            capture_output=True, text=True, timeout=5,
            stdin=subprocess.DEVNULL,
        )
        if out.returncode == 0:
            result["compose"] = True
    except (subprocess.TimeoutExpired, OSError):
        pass

    return result


def _check_container() -> Dict[str, Any]:
    """Check if llm-relay Docker container is running."""
    result = {"running": False, "healthy": False, "port": None}

    try:
        out = subprocess.run(
            ["docker", "ps", "--filter", "name=llm-relay", "--format",
             "{{.Status}}\t{{.Ports}}"],
            capture_output=True, text=True, timeout=5,
            stdin=subprocess.DEVNULL,
        )
        if out.returncode == 0 and out.stdout.strip():
            result["running"] = True
            status_line = out.stdout.strip().split("\n")[0]
            if "healthy" in status_line.lower():
                result["healthy"] = True
            # Extract port from "127.0.0.1:8080->8080/tcp"
            ports_part = status_line.split("\t")[-1] if "\t" in status_line else ""
            if ":" in ports_part and "->" in ports_part:
                try:
                    result["port"] = int(ports_part.split(":")[1].split("->")[0])
                except (ValueError, IndexError):
                    pass
    except (subprocess.TimeoutExpired, OSError):
        pass

    # Fallback: try health endpoint on common ports
    if result["running"] and not result["port"]:
        for port in [8080, 8083]:
            try:
                import urllib.request
                resp = urllib.request.urlopen(
                    "http://localhost:{}/_health".format(port), timeout=3,
                )
                data = json.loads(resp.read())
                if data.get("status") == "ok":
                    result["port"] = port
                    result["healthy"] = True
                    break
            except Exception:
                continue

    return result


def db_dir_for_env() -> Path:
    """Return the DB directory from env or default."""
    env_db = os.getenv("LLM_RELAY_DB")
    if env_db:
        return Path(env_db).parent
    return Path.home() / ".llm-relay"


# ── Main init function ──


def run_init(
    port: int = 8080,
    skip_server: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
) -> Dict[str, Any]:
    """Run diagnostic checks and print Docker setup instructions.

    Returns a summary dict with all results.
    """
    summary = {
        "version": None,
        "clis": [],
        "docker": None,
        "container": None,
        "port": port,
        "urls": {},
        "next_steps": [],
    }

    try:
        from llm_relay import __version__
        summary["version"] = __version__
    except ImportError:
        summary["version"] = "unknown"

    # Step 1: Detect CLIs
    summary["clis"] = _detect_clis()

    # Step 2: Check Docker
    summary["docker"] = _check_docker()

    # Step 3: Check running container
    summary["container"] = _check_container()

    # Step 4: Determine next steps
    next_steps = []

    if not summary["docker"]["installed"]:
        next_steps.append("Install Docker: https://docs.docker.com/get-docker/")
    elif not summary["docker"]["running"]:
        next_steps.append("Start Docker Desktop or Docker Engine")
    elif not summary["docker"]["compose"]:
        next_steps.append("Install Docker Compose: https://docs.docker.com/compose/install/")

    if summary["docker"]["installed"] and summary["docker"]["running"]:
        if not summary["container"]["running"]:
            next_steps.append(
                "Download docker-compose.yml and start:"
                "\n    curl -sL https://raw.githubusercontent.com/"
                "ArkNill/llm-relay/main/docker-compose.yml -o docker-compose.yml"
                "\n    docker compose up -d"
            )
        elif not summary["container"]["healthy"]:
            next_steps.append("Container running but not healthy. Check: docker logs llm-relay")

    if summary["container"]["running"] and summary["container"]["healthy"]:
        p = summary["container"]["port"] or port
        summary["port"] = p
        summary["urls"] = {
            "dashboard": "http://localhost:{}/dashboard/".format(p),
            "display": "http://localhost:{}/display/".format(p),
            "history": "http://localhost:{}/history/".format(p),
        }
        # Check if CC is configured to use the proxy
        cc_settings = Path.home() / ".claude" / "settings.json"
        cc_configured = False
        if cc_settings.exists():
            try:
                data = json.loads(cc_settings.read_text(encoding="utf-8"))
                env = data.get("env", {})
                base_url = env.get("ANTHROPIC_BASE_URL", "")
                if "localhost:{}".format(p) in base_url:
                    cc_configured = True
            except (json.JSONDecodeError, OSError):
                pass

        if not cc_configured:
            settings_path = "~/.claude/settings.json"
            if sys.platform == "win32":
                settings_path = "%USERPROFILE%\\.claude\\settings.json"
            next_steps.append(
                "To route Claude Code through the proxy, add to {}:"
                '\n    "env": {{ "ANTHROPIC_BASE_URL": "http://localhost:{}" }}'.format(
                    settings_path, p
                )
            )
    else:
        next_steps.append(
            "After starting, open: http://localhost:{}/dashboard/".format(port)
        )

    summary["next_steps"] = next_steps

    return summary
