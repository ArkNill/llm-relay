"""Display page helper -- extracts last user prompts from session transcripts.

Lightweight tail-based JSONL parsing for real-time dashboard display.
Supports Claude Code, OpenAI Codex, and Gemini CLI sessions.
Also provides CLI process liveness check via host /proc mount.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from llm_relay.detect.scanner import find_projects_dir

# Known CLI binary names for process detection
_CLI_PROCESS_NAMES = {"claude", "codex", "gemini"}

# Filters for non-user-input messages that live under type=="user"
_WRAPPER_PREFIXES = (
    "<task-notification",
    "<local-command",
    "<command-",
    "Caveat:",
    "<tool_use_error",
    "<user-prompt-submit-hook",
)


def _extract_text(content) -> str:
    """Extract plain text from a message content field (str or list of parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                return part.get("text", "")
    return ""


def _is_real_user_prompt(text: str) -> bool:
    """True if the text looks like a genuine user-typed prompt (not a system wrapper)."""
    if not text:
        return False
    stripped = text.lstrip()
    for prefix in _WRAPPER_PREFIXES:
        if stripped.startswith(prefix):
            return False
    # Pure system-reminder blocks (no other content)
    if stripped.startswith("<system-reminder>") and stripped.rstrip().endswith("</system-reminder>"):
        return False
    return True


def _tail_lines(path: Path, max_bytes: int = 256 * 1024) -> list[str]:
    """Read the last `max_bytes` of a file and return complete lines only.

    Handles large JSONL transcripts efficiently by seeking to the end.
    """
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # discard partial line
            data = f.read()
        return data.decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _get_projects_dirs(projects_dir: Optional[Path] = None) -> list:
    """Return candidate session directories for all supported CLIs.

    Searches:
      - Claude Code: ~/.claude/projects/ + ~/.claude-gt/projects/ + env override
      - Codex: ~/.codex/sessions/
      - Gemini: ~/.gemini/tmp/*/chats/
    """
    if projects_dir is not None:
        return [projects_dir]
    dirs: list[Path] = []
    env_path = os.getenv("CC_CLAUDE_PROJECTS_DIR")
    if env_path:
        dirs.append(Path(env_path))
    # Claude Code -- stock
    stock = find_projects_dir()
    if stock.is_dir() and stock not in dirs:
        dirs.append(stock)
    # Claude Code -- claudeGt isolated
    gt = Path.home() / ".claude-gt" / "projects"
    if gt.is_dir() and gt not in dirs:
        dirs.append(gt)
    # Codex -- sessions dir
    codex_env = os.environ.get("LLM_RELAY_CODEX_HOME")
    codex_base = Path(codex_env) if codex_env else Path.home() / ".codex"
    codex_sessions = codex_base / "sessions"
    if codex_sessions.is_dir() and codex_sessions not in dirs:
        dirs.append(codex_sessions)
    # Gemini -- tmp/*/chats dirs
    gemini_env = os.environ.get("LLM_RELAY_GEMINI_HOME")
    gemini_home = Path(gemini_env) if gemini_env else Path.home() / ".gemini"
    gemini_tmp = gemini_home / "tmp"
    if gemini_tmp.is_dir():
        for pdir in gemini_tmp.iterdir():
            chats = pdir / "chats"
            if chats.is_dir() and chats not in dirs:
                dirs.append(chats)
    return dirs


def _find_session_file(session_id: str, projects_dir: Optional[Path] = None) -> Optional[Path]:
    """Locate a session file by ID across all CLI session directories.

    Searches for:
      - Claude Code: <dir>/<project>/<session_id>.jsonl
      - Codex: <dir>/**/<session_id>.jsonl (rollout-* prefixed)
      - Gemini: <dir>/<session_id>.json or .jsonl
    """
    for pdir in _get_projects_dirs(projects_dir):
        try:
            # Direct match (Claude Code style: project_dir/session_id.jsonl)
            for child in pdir.iterdir():
                if child.is_dir():
                    candidate = child / "{}.jsonl".format(session_id)
                    if candidate.exists():
                        return candidate
                elif child.is_file():
                    stem = child.stem
                    if stem == session_id or stem.endswith(session_id):
                        return child
            # Recursive search for Codex (sessions/YYYY/MM/DD/rollout-*.jsonl)
            for match in pdir.rglob("*{}*".format(session_id)):
                if match.is_file() and match.suffix in (".jsonl", ".json"):
                    return match
        except OSError:
            continue
    return None


def _extract_prompt_from_cc(lines: list) -> dict:
    """Extract last user prompt from Claude Code JSONL lines."""
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if obj.get("type") != "user":
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = _extract_text(msg.get("content"))
        if _is_real_user_prompt(text):
            return {"text": text.strip()[:500], "timestamp": obj.get("timestamp")}
    return {"text": "", "timestamp": None}


def _extract_prompt_from_codex(lines: list) -> dict:
    """Extract last user prompt from Codex JSONL lines."""
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        # Codex format: type="user" or role="user"
        entry_type = obj.get("type", obj.get("role", ""))
        if entry_type != "user":
            continue
        # Codex stores prompt in message.content or directly in content/text
        msg = obj.get("message", obj)
        text = ""
        if isinstance(msg, dict):
            text = _extract_text(msg.get("content", msg.get("text", "")))
        if not text:
            text = _extract_text(obj.get("content", obj.get("text", "")))
        if _is_real_user_prompt(text):
            return {
                "text": text.strip()[:500],
                "timestamp": obj.get("timestamp", obj.get("created_at")),
            }
    return {"text": "", "timestamp": None}


def _extract_prompt_from_gemini(content: str) -> dict:
    """Extract last user prompt from Gemini JSON or JSONL content."""
    records: list = []
    content = content.strip()
    if not content:
        return {"text": "", "timestamp": None}

    if content.startswith("["):
        # JSON array format
        try:
            parsed = json.loads(content)
            if isinstance(parsed, list):
                records = parsed
        except json.JSONDecodeError:
            return {"text": "", "timestamp": None}
    else:
        # JSONL format
        for line in content.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                records.append(json.loads(stripped))
            except json.JSONDecodeError:
                continue

    for obj in reversed(records):
        if not isinstance(obj, dict):
            continue
        entry_type = obj.get("type", obj.get("role", ""))
        if entry_type != "user":
            continue
        text = obj.get("message", obj.get("text", obj.get("content", "")))
        if isinstance(text, dict):
            text = text.get("text", text.get("content", ""))
        if isinstance(text, list):
            text = _extract_text(text)
        if isinstance(text, str) and _is_real_user_prompt(text):
            return {
                "text": text.strip()[:500],
                "timestamp": obj.get("timestamp", obj.get("createdAt")),
            }
    return {"text": "", "timestamp": None}


def get_last_user_prompt(session_id: str, projects_dir: Optional[Path] = None) -> dict:
    """Return the most recent real user prompt for a session.

    Supports Claude Code, Codex, and Gemini session formats.

    Returns:
        {"text": str, "timestamp": str} or {"text": "", "timestamp": None} if not found.
    """
    if not session_id:
        return {"text": "", "timestamp": None}

    session_path = _find_session_file(session_id, projects_dir)
    if session_path is None:
        return {"text": "", "timestamp": None}

    # Determine CLI type from path
    path_str = str(session_path)
    if ".codex" in path_str:
        lines = _tail_lines(session_path)
        return _extract_prompt_from_codex(lines)
    elif ".gemini" in path_str:
        try:
            content = session_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {"text": "", "timestamp": None}
        return _extract_prompt_from_gemini(content)
    else:
        # Default: Claude Code format
        lines = _tail_lines(session_path)
        return _extract_prompt_from_cc(lines)


def _get_proc_dir() -> Path:
    """Return the /proc directory path (host /proc if mounted, else local)."""
    env_path = os.getenv("CC_HOST_PROC")
    if env_path:
        return Path(env_path)
    return Path("/proc")


def _is_cli_process(comm: str, cmdline: str) -> bool:
    """Check if a process name or cmdline matches any known CLI tool."""
    comm_lower = comm.lower()
    cmdline_lower = cmdline.lower()
    for name in _CLI_PROCESS_NAMES:
        if comm_lower == name or name in cmdline_lower:
            return True
    return False


def find_cli_pid_by_tty(tty: Optional[str]) -> Optional[int]:
    """Find a running CLI process (claude/codex/gemini) on a given TTY.

    Args:
        tty: TTY path like "/dev/pts/8" or "pts/8".

    Returns:
        PID of the matching CLI process, or None if not found.
    """
    if not tty:
        return None

    tty_short = tty.replace("/dev/", "")
    if not tty_short:
        return None

    proc_dir = _get_proc_dir()
    try:
        for entry in proc_dir.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline_path = entry / "cmdline"
                if not cmdline_path.exists():
                    continue
                cmdline = cmdline_path.read_bytes().decode("utf-8", errors="replace")
                comm = ""
                comm_path = entry / "comm"
                if comm_path.exists():
                    comm = comm_path.read_text(errors="replace").strip()
                if not _is_cli_process(comm, cmdline):
                    continue
                # Check the stat file for TTY
                stat_path = entry / "stat"
                if not stat_path.exists():
                    continue
                stat_content = stat_path.read_text(errors="replace")
                rparen = stat_content.rfind(")")
                if rparen == -1:
                    continue
                fields = stat_content[rparen + 1:].split()
                if len(fields) < 5:
                    continue
                tty_nr = int(fields[4])
                major = (tty_nr >> 8) & 0xff
                minor = (tty_nr & 0xff) | ((tty_nr >> 12) & 0xfff00)
                if major == 136:
                    candidate = f"pts/{minor}"
                elif major == 4:
                    candidate = f"tty{minor}"
                else:
                    continue
                if candidate == tty_short:
                    return int(entry.name)
            except (OSError, ValueError, PermissionError):
                continue
    except OSError:
        pass
    return None


def is_cli_process_alive(pid: Optional[int]) -> bool:
    """Check if a process is alive and is a known CLI instance.

    Verifies both existence and that the process matches claude/codex/gemini
    to protect against PID reuse.

    Args:
        pid: Process ID to check. Returns False if None or invalid.

    Returns:
        True if the process exists and looks like a CLI tool, False otherwise.
    """
    if not pid or pid <= 0:
        return False

    proc_dir = _get_proc_dir() / str(pid)
    if not proc_dir.exists():
        return False

    try:
        cmdline_path = proc_dir / "cmdline"
        if not cmdline_path.exists():
            return False
        cmdline = cmdline_path.read_bytes().decode("utf-8", errors="replace")
        comm = ""
        comm_path = proc_dir / "comm"
        if comm_path.exists():
            comm = comm_path.read_text(errors="replace").strip()
        if _is_cli_process(comm, cmdline):
            return True
    except (OSError, PermissionError):
        pass

    return False


# Backward-compatible aliases
find_claude_pid_by_tty = find_cli_pid_by_tty
is_cc_process_alive = is_cli_process_alive


# ── CC session liveness helpers (shared by /display and /turns endpoints) ──

def collect_owned_cc_pids(terminals: dict) -> set:
    """Return the set of cc_pids from terminals whose process is currently alive.

    Used as the "claimed PID" set when deciding whether to fall back to TTY
    lookup for a session whose registered cc_pid is dead — a TTY-discovered
    PID is only valid if it isn't already claimed by another live session.
    """
    owned: set = set()
    for term in terminals.values():
        pid = term.get("cc_pid")
        if pid and is_cc_process_alive(pid):
            owned.add(pid)
    return owned


def check_cc_session_alive(
    term: dict,
    last_ts: Optional[float],
    owned_cc_pids: set,
    now_ts: float,
    stale_tty_window_s: int = 600,
) -> bool:
    """Decide whether a CC session is alive given its terminal record.

    Two-tier check:
    1. Registered cc_pid is alive -> alive (source of truth).
    2. cc_pid dead/missing, but the session was active within
       stale_tty_window_s AND a claude process exists on its TTY whose PID
       is not already claimed by another live registered session -> alive
       (handles intermediate shell-process PID drift without resurrecting
       long-dead sessions when a new CC reuses the same /dev/pts/N).
    """
    if not term:
        return False
    cc_pid = term.get("cc_pid")
    tty = term.get("tty")
    if cc_pid and is_cc_process_alive(cc_pid):
        return True
    if tty and last_ts and (now_ts - last_ts) < stale_tty_window_s:
        real_pid = find_claude_pid_by_tty(tty)
        if real_pid and real_pid not in owned_cc_pids:
            return True
    return False


# ── External CLI (Codex/Gemini) liveness via open file descriptors ──

def _collect_open_session_paths(proc_dir: Optional[Path] = None) -> set:
    """Return resolved paths of session JSONL/JSON files held open by any process.

    Codex/Gemini transcripts persist on disk after the CLI exits, so file
    mtime is not a reliable liveness signal. Instead we check whether any
    process currently has the transcript open as a file descriptor — when
    the CLI exits the kernel closes the fd and the path drops out of /proc.
    """
    if proc_dir is None:
        proc_dir = _get_proc_dir()
    open_paths: set = set()
    try:
        entries = list(proc_dir.iterdir())
    except OSError:
        return open_paths
    for entry in entries:
        if not entry.name.isdigit():
            continue
        fd_dir = entry / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except (OSError, PermissionError):
            continue
        for fd in fds:
            try:
                target = os.readlink(str(fd))
            except OSError:
                continue
            if not (target.endswith(".jsonl") or target.endswith(".json")):
                continue
            try:
                resolved = str(Path(target).resolve())
            except OSError:
                resolved = target
            open_paths.add(resolved)
    return open_paths


def _parse_codex_session_raw(path: Path) -> dict:
    """Parse a Codex JSONL session file directly.

    Returns: {"user_turns": int, "first_ts": str|None, "last_ts": str|None}
    """
    user_turns = 0
    first_ts = None
    last_ts = None
    last_user_text = ""
    last_user_ts = None

    try:
        for line in _tail_lines(path, max_bytes=512 * 1024):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            ts = obj.get("timestamp")
            if ts and first_ts is None:
                first_ts = ts
            if ts:
                last_ts = ts

            entry_type = obj.get("type", "")
            if entry_type == "response_item":
                payload = obj.get("payload", {})
                if payload.get("role") == "user":
                    user_turns += 1
                    content = payload.get("content", [])
                    for part in content:
                        if isinstance(part, dict):
                            text = part.get("text", "")
                            if text and _is_real_user_prompt(text):
                                last_user_text = text.strip()[:500]
                                last_user_ts = ts
            elif entry_type == "event_msg":
                payload = obj.get("payload", {})
                if payload.get("type") == "user_message":
                    text = payload.get("text", payload.get("message", ""))
                    if isinstance(text, str) and _is_real_user_prompt(text):
                        last_user_text = text.strip()[:500]
                        last_user_ts = ts
    except OSError:
        pass

    return {
        "user_turns": user_turns,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "last_user_text": last_user_text,
        "last_user_ts": last_user_ts,
    }


def _parse_gemini_session_raw(path: Path) -> dict:
    """Parse a Gemini session JSON file directly.

    Gemini uses {sessionId, messages: [{type, content}]} format.

    Returns: {"user_turns": int, "first_ts": str|None, "last_ts": str|None}
    """
    user_turns = 0
    first_ts = None
    last_ts = None
    last_user_text = ""
    last_user_ts = None

    try:
        content = path.read_text(encoding="utf-8", errors="replace").strip()
        if not content:
            return {"user_turns": 0, "first_ts": None, "last_ts": None,
                    "last_user_text": "", "last_user_ts": None}

        data = json.loads(content)

        # Handle nested messages format: {messages: [{type, content}]}
        if isinstance(data, dict):
            first_ts = data.get("startTime")
            last_ts = data.get("lastUpdated")
            messages = data.get("messages", [])
        elif isinstance(data, list):
            messages = data
        else:
            messages = []

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            msg_type = msg.get("type", msg.get("role", ""))
            ts = msg.get("timestamp", msg.get("createdAt"))
            if ts and first_ts is None:
                first_ts = ts
            if ts:
                last_ts = ts

            if msg_type == "user":
                user_turns += 1
                # Content can be string or list of parts
                msg_content = msg.get("content", msg.get("text", ""))
                if isinstance(msg_content, list):
                    for part in msg_content:
                        if isinstance(part, dict):
                            text = part.get("text", "")
                            if text and _is_real_user_prompt(text):
                                last_user_text = text.strip()[:500]
                                last_user_ts = ts
                elif isinstance(msg_content, str) and _is_real_user_prompt(msg_content):
                    last_user_text = msg_content.strip()[:500]
                    last_user_ts = ts
    except (OSError, json.JSONDecodeError):
        pass

    return {
        "user_turns": user_turns,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "last_user_text": last_user_text,
        "last_user_ts": last_user_ts,
    }


def discover_external_cli_sessions(
    window_hours: float = 4.0,
    include_dead: bool = False,
) -> list:
    """Discover active Codex/Gemini sessions not tracked by the proxy DB.

    Directly parses session files (not using provider parsers, which may not
    match the actual on-disk format for display purposes). A session is
    considered alive only if its transcript file is currently held open by
    some process — after the CLI exits the kernel releases the fd and the
    session is filtered out unless include_dead=True.

    Args:
        window_hours: Only include sessions modified within this time window.
        include_dead: When True, return dead sessions too with alive=False.
    """
    import time

    cutoff = time.time() - (window_hours * 3600)
    results: list = []

    try:
        from llm_relay.providers import get_all_providers
    except ImportError:
        return results

    open_paths = _collect_open_session_paths()

    for provider in get_all_providers():
        if provider.provider_id == "claude-code":
            continue

        try:
            sessions = provider.discover_sessions(limit=20)
        except Exception:
            continue

        for sf in sessions:
            if sf.mtime < cutoff:
                continue

            try:
                resolved = str(sf.path.resolve())
            except OSError:
                resolved = str(sf.path)
            alive = resolved in open_paths
            if not alive and not include_dead:
                continue

            # Parse directly based on provider type
            if provider.provider_id == "openai-codex":
                info = _parse_codex_session_raw(sf.path)
            elif provider.provider_id == "gemini-cli":
                info = _parse_gemini_session_raw(sf.path)
            else:
                continue

            user_turns = info["user_turns"]
            if user_turns == 0:
                continue

            first_ts = _iso_to_epoch(info["first_ts"]) if info["first_ts"] else None
            last_ts = _iso_to_epoch(info["last_ts"]) if info["last_ts"] else None

            duration_s = 0.0
            if first_ts and last_ts:
                duration_s = last_ts - first_ts

            results.append({
                "session_id": sf.session_id,
                "provider": provider.provider_id,
                "provider_name": provider.display_name,
                "turns": user_turns,
                "first_ts": first_ts,
                "last_ts": last_ts,
                "duration_s": round(duration_s, 1),
                "current_ctx": 0,
                "peak_ctx": 0,
                "recent_peak": 0,
                "cumul_unique": 0,
                "ceiling": 0,
                "abs_zone": "unknown",
                "abs_color": "gray",
                "ratio_zone": "unknown",
                "ratio_color": "gray",
                "last_prompt": info["last_user_text"],
                "last_prompt_ts": info["last_user_ts"],
                "tty": None,
                "cc_pid": None,
                "term_pid": None,
                "term_name": None,
                "alive": alive,
            })

    return results


def _iso_to_epoch(ts: str) -> Optional[float]:
    """Best-effort ISO 8601 timestamp to epoch seconds."""
    if not ts:
        return None
    from datetime import datetime, timezone
    try:
        # Handle Z suffix
        ts = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, OSError):
        return None

