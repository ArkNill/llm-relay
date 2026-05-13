"""Display page helper -- extracts last user prompts from session transcripts.

Lightweight tail-based JSONL parsing for real-time dashboard display.
Supports Claude Code, OpenAI Codex, and Gemini CLI sessions.
Also provides cross-platform CLI process liveness check.
"""

from __future__ import annotations

import json
import os
import time as _time
from pathlib import Path
from typing import Optional

from llm_relay.api._compat import (
    _is_cli_process_name as _is_cli_process,  # noqa: F401 (re-exported for tests)
)
from llm_relay.api._compat import (
    collect_open_session_path_pids as _collect_open_session_path_pids,
)
from llm_relay.api._compat import (
    find_cli_pid_by_tty,
    is_cli_process_alive,
    is_cli_running_cached,
)
from llm_relay.api._compat import (
    get_parent_comm_chain as _get_parent_comm_chain,
)
from llm_relay.api._compat import (
    get_process_terminal_name as _get_process_terminal_name,
)
from llm_relay.api._compat import (
    get_process_tty as _get_process_tty,
)
from llm_relay.api._compat import (
    read_proc_environ as _read_proc_environ,
)

# Re-exports from _zones.py for backward compatibility (tests import these from display)
from llm_relay.api._zones import (  # noqa: F401
    _OPENAI_CODEX_OFFICIAL_CONTEXT_WINDOW,
    _OPENAI_CODEX_OFFICIAL_MAX_OUTPUT,
    _codex_classify_absolute,
    _codex_classify_ratio,
    _codex_compute_zone_bundle,
    _codex_display_ceiling,
    _context_tokens_from_openai_usage,
    _extract_codex_token_metrics,
    _to_int,
    _usage_total,
)
from llm_relay.detect.scanner import find_projects_dir

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
      - Claude Code: ~/.claude/projects/ + alternate config dirs + env override
      - Codex: ~/.codex/sessions/
      - Gemini: ~/.gemini/tmp/*/chats/
    """
    if projects_dir is not None:
        return [projects_dir]
    dirs: list[Path] = []
    env_path = os.getenv("LLM_RELAY_CLAUDE_PROJECTS_DIR")
    if env_path:
        dirs.append(Path(env_path))
    # Claude Code -- stock
    stock = find_projects_dir()
    if stock.is_dir() and stock not in dirs:
        dirs.append(stock)
    # Claude Code -- alternate config dir
    gt = Path.home() / ".claude-gt" / "projects"
    if gt.is_dir() and gt not in dirs:
        dirs.append(gt)
    # Codex -- sessions dir
    codex_env = os.environ.get("LLM_RELAY_CODEX_HOME") or os.environ.get("CCPULSE_CODEX_HOME")
    codex_base = Path(codex_env) if codex_env else Path.home() / ".codex"
    codex_sessions = codex_base / "sessions"
    if codex_sessions.is_dir() and codex_sessions not in dirs:
        dirs.append(codex_sessions)
    # Gemini -- tmp/*/chats dirs
    gemini_env = os.environ.get("LLM_RELAY_GEMINI_HOME") or os.environ.get("CCPULSE_GEMINI_HOME")
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


def _parse_codex_session_history(path: Path, include_thinking: bool = False) -> list[dict]:
    """Build history-detail turns from a Codex session JSONL file.

    Codex sessions are not stored in the proxy DB, so the History page needs a
    lightweight session-file fallback that roughly matches the proxy DB shape.
    """
    turns: list[dict] = []
    current_turn: Optional[dict] = None
    turn_number = 0

    def _finalize_turn() -> None:
        nonlocal current_turn
        if current_turn is None:
            return
        response_blocks = current_turn.pop("_response_blocks")
        thinking_blocks = current_turn.pop("_thinking_blocks")
        current_turn["response_message"] = (
            json.dumps(response_blocks, ensure_ascii=False) if response_blocks else None
        )
        current_turn["thinking_blocks"] = (
            json.dumps(thinking_blocks, ensure_ascii=False) if thinking_blocks else None
        )
        current_turn["response_size_bytes"] = (
            len(current_turn["response_message"].encode("utf-8"))
            if current_turn["response_message"]
            else 0
        )
        turns.append(current_turn)
        current_turn = None

    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                continue

            if obj.get("type") != "response_item":
                continue

            payload = obj.get("payload", {})
            if not isinstance(payload, dict):
                continue

            item_type = payload.get("type")
            role = payload.get("role")
            ts_epoch = _iso_to_epoch(obj.get("timestamp", ""))

            if role == "user":
                _finalize_turn()
                turn_number += 1
                request_messages = json.dumps(
                    [{"role": "user", "content": payload.get("content")}],
                    ensure_ascii=False,
                )
                current_turn = {
                    "id": None,
                    "ts": ts_epoch,
                    "session_id": path.stem,
                    "request_id": None,
                    "turn_number": turn_number,
                    "storage_mode": "full",
                    "request_messages": request_messages,
                    "response_message": None,
                    "thinking_blocks": None,
                    "model": None,
                    "temperature": None,
                    "max_tokens": None,
                    "total_message_count": 1,
                    "previous_message_count": max(0, turn_number - 1),
                    "request_size_bytes": len(request_messages.encode("utf-8")),
                    "response_size_bytes": 0,
                    "provider": "openai-codex",
                    "_response_blocks": [],
                    "_thinking_blocks": [],
                }
                continue

            if current_turn is None:
                continue

            if role == "assistant" and item_type == "message":
                content = payload.get("content")
                if isinstance(content, list):
                    current_turn["_response_blocks"].extend(content)
                elif content is not None:
                    current_turn["_response_blocks"].append({"type": "output_text", "text": str(content)})
                continue

            if item_type == "function_call":
                current_turn["_response_blocks"].append({
                    "type": "tool_use",
                    "name": payload.get("name", ""),
                    "input": payload.get("arguments", ""),
                })
                continue

            if item_type == "function_call_output":
                current_turn["_response_blocks"].append({
                    "type": "tool_result",
                    "content": payload.get("output", ""),
                })
                continue

            if item_type == "reasoning" and include_thinking:
                summary = payload.get("summary")
                if isinstance(summary, list) and summary:
                    thinking_text = "\n".join(
                        part.get("text", "") for part in summary if isinstance(part, dict)
                    ).strip()
                else:
                    thinking_text = ""
                if not thinking_text and payload.get("encrypted_content"):
                    thinking_text = "[encrypted reasoning]"
                if thinking_text:
                    current_turn["_thinking_blocks"].append({
                        "type": "thinking",
                        "thinking": thinking_text,
                    })
    except OSError:
        return []

    _finalize_turn()
    return turns


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


# Backward-compatible aliases
find_claude_pid_by_tty = find_cli_pid_by_tty
is_cc_process_alive = is_cli_process_alive


# ── Connection type detection ──

# Cache: pid -> (timestamp, result)
_conn_type_cache: dict = {}
_CONN_CACHE_TTL = 60.0  # seconds


def detect_connection_type(pid: int) -> str:
    """Detect connection method for a process.

    Checks /proc/PID/environ for env vars and walks parent process tree.
    Returns combined transport+multiplexer label:
      'native', 'ssh', 'tmux', 'ssh+tmux', 'tailscale+tmux',
      'screen', 'ssh+screen', 'mosh', 'mosh+tmux', 'unknown'
    """
    if not pid or pid <= 0:
        return "unknown"

    # Cache check
    now = _time.time()
    cached = _conn_type_cache.get(pid)
    if cached and (now - cached[0]) < _CONN_CACHE_TTL:
        return cached[1]

    env = _read_proc_environ(pid)
    chain = _get_parent_comm_chain(pid)
    parent_comms = {comm.lower() for _, comm in chain}

    # Detect multiplexer
    multiplexer = ""
    if env.get("TMUX") or "tmux: server" in parent_comms or "tmux" in parent_comms:
        multiplexer = "tmux"
    elif env.get("STY") or "screen" in parent_comms:
        multiplexer = "screen"

    # Detect transport
    transport = "native"
    ssh_conn = env.get("SSH_CONNECTION", "")
    if env.get("MOSH_SESSION_ID") or "mosh-server" in parent_comms:
        transport = "mosh"
    elif ssh_conn or "sshd" in parent_comms:
        # Check for Tailscale (100.x.x.x CGNAT range)
        if ssh_conn:
            client_ip = ssh_conn.split()[0] if ssh_conn.split() else ""
            if client_ip.startswith("100."):
                transport = "tailscale"
            else:
                transport = "ssh"
        else:
            transport = "ssh"

    # Combine
    if multiplexer:
        if transport == "native":
            result = multiplexer
        else:
            result = transport + "+" + multiplexer
    else:
        result = transport

    _conn_type_cache[pid] = (now, result)
    return result


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


def _parse_cc_session_raw(path: Path) -> dict:
    """Parse a Claude Code JSONL session file for display (Windows file-based fallback).

    Extracts user turns, timestamps, last prompt, token metrics, and cache stats
    from the CC JSONL format where assistant entries contain full usage data.
    """
    user_turns = 0
    first_ts = None  # type: Optional[str]
    last_ts = None  # type: Optional[str]
    last_user_text = ""
    last_user_ts = None  # type: Optional[str]
    session_id = None  # type: Optional[str]
    current_ctx = 0
    peak_ctx = 0
    recent_contexts = []  # type: list
    cumul_input = 0
    cumul_output = 0
    total_input_tokens = 0
    total_cache_read = 0
    total_cache_creation = 0

    try:
        for line in _tail_lines(path, max_bytes=4 * 1024 * 1024):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            ts = obj.get("timestamp")
            entry_type = obj.get("type", "")

            if not session_id and obj.get("sessionId"):
                session_id = obj["sessionId"]
            if ts and first_ts is None:
                first_ts = ts
            if ts:
                last_ts = ts

            if entry_type == "user":
                user_turns += 1
                msg = obj.get("message", {})
                if isinstance(msg, dict):
                    content = msg.get("content", "")
                    if isinstance(content, str) and _is_real_user_prompt(content):
                        last_user_text = content.strip()[:500]
                        last_user_ts = ts
                    elif isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                text = part.get("text", "")
                                if text and _is_real_user_prompt(text):
                                    last_user_text = text.strip()[:500]
                                    last_user_ts = ts

            elif entry_type == "assistant":
                msg = obj.get("message", {})
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage", {})
                if not isinstance(usage, dict):
                    continue
                inp = usage.get("input_tokens", 0) or 0
                out = usage.get("output_tokens", 0) or 0
                cache_read = usage.get("cache_read_input_tokens", 0) or 0
                cache_create = usage.get("cache_creation_input_tokens", 0) or 0

                # current_ctx = input + cache_read + cache_create (total context sent)
                ctx = inp + cache_read + cache_create
                if ctx > 0:
                    current_ctx = ctx
                    peak_ctx = max(peak_ctx, ctx)
                    recent_contexts.append(ctx)

                cumul_input += ctx
                cumul_output += out
                total_input_tokens += inp + cache_read + cache_create
                total_cache_read += cache_read
                total_cache_creation += cache_create
    except OSError:
        pass

    recent_peak = max(recent_contexts[-5:], default=0)
    cumul_unique = cumul_input + cumul_output

    cache_hit_rate = None  # type: Optional[float]
    if total_input_tokens > 0:
        cache_hit_rate = round(total_cache_read / total_input_tokens * 100, 2)

    return {
        "user_turns": user_turns,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "last_user_text": last_user_text,
        "last_user_ts": last_user_ts,
        "session_id": session_id,
        "current_ctx": current_ctx,
        "peak_ctx": peak_ctx,
        "recent_peak": recent_peak,
        "cumul_unique": cumul_unique,
        "model_window": 0,
        "cache_hit_rate": cache_hit_rate,
    }


def _parse_codex_session_raw(path: Path) -> dict:
    """Parse a Codex JSONL session file directly.

    Returns user turn, prompt, timestamp, and token display metrics.
    """
    user_turns = 0
    first_ts = None
    last_ts = None
    last_user_text = ""
    last_user_ts = None
    current_ctx = 0
    peak_ctx = 0
    recent_contexts: list[int] = []
    cumul_unique = 0
    model_window = 0
    total_input_tokens = 0
    total_cached_tokens = 0

    try:
        for line in _tail_lines(path, max_bytes=4 * 1024 * 1024):
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
            # Codex reuses the same JSONL file across exit/restart cycles.
            # Each new run emits a fresh task_started event — reset accumulators
            # so metrics reflect the current run, not the entire file history.
            if entry_type == "event_msg":
                payload_peek = obj.get("payload", {})
                if payload_peek.get("type") == "task_started":
                    user_turns = 0
                    current_ctx = 0
                    peak_ctx = 0
                    recent_contexts.clear()
                    cumul_unique = 0
                    total_input_tokens = 0
                    total_cached_tokens = 0
                    first_ts = ts
                    last_user_text = ""
                    last_user_ts = None

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
                elif payload.get("type") == "token_count":
                    metrics = _extract_codex_token_metrics(payload, recent_contexts)
                    if metrics.get("current_ctx"):
                        current_ctx = metrics["current_ctx"]
                        peak_ctx = max(peak_ctx, current_ctx)
                        # Accumulate cumul from per-request last_token_usage
                        # instead of total_token_usage (which doesn't reset
                        # across Codex exit/restart within the same file).
                        last_usage = payload.get("info", {}).get("last_token_usage", {})
                        last_out = last_usage.get("output_tokens", 0)
                        if isinstance(last_out, (int, float)):
                            cumul_unique += current_ctx + int(last_out)
                    if metrics.get("model_window"):
                        model_window = metrics["model_window"]
                    # Cache stats from total_token_usage
                    info = payload.get("info")
                    if isinstance(info, dict):
                        usage = info.get("total_token_usage", {})
                        if isinstance(usage, dict):
                            inp = usage.get("input_tokens", 0)
                            cached = usage.get("cached_input_tokens", 0)
                            if isinstance(inp, (int, float)) and inp > 0:
                                total_input_tokens = int(inp)
                            if isinstance(cached, (int, float)) and cached > 0:
                                total_cached_tokens = int(cached)
    except OSError:
        pass

    recent_peak = max(recent_contexts[-5:], default=0)

    cache_hit_rate = None  # type: Optional[float]
    if total_input_tokens > 0:
        cache_hit_rate = round(total_cached_tokens / total_input_tokens * 100, 2)

    return {
        "user_turns": user_turns,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "last_user_text": last_user_text,
        "last_user_ts": last_user_ts,
        "current_ctx": current_ctx,
        "peak_ctx": peak_ctx,
        "recent_peak": recent_peak,
        "cumul_unique": cumul_unique,
        "model_window": model_window,
        "cache_hit_rate": cache_hit_rate,
    }


def _parse_gemini_session_raw(path: Path) -> dict:
    """Parse a Gemini session file (JSON or JSONL) directly.

    Supports:
      - Legacy JSON: [{type, message}] or {messages: [{type, content}]}
      - New JSONL: one JSON object per line (session header + messages + $set updates)

    Returns: {"user_turns": int, "first_ts": str|None, "last_ts": str|None, ...}
    """
    user_turns = 0
    first_ts = None
    last_ts = None
    last_user_text = ""
    last_user_ts = None
    total_input_tokens = 0
    total_cached_tokens = 0
    current_ctx = 0
    peak_ctx = 0
    cumul_total = 0
    recent_contexts = []  # type: list[int]

    def _process_msg(msg: dict) -> None:
        nonlocal user_turns, first_ts, last_ts, last_user_text, last_user_ts
        nonlocal total_input_tokens, total_cached_tokens
        nonlocal current_ctx, peak_ctx, cumul_total
        if not isinstance(msg, dict):
            return
        # Skip $set metadata entries
        if "$set" in msg:
            return
        msg_type = msg.get("type", msg.get("role", ""))
        ts = msg.get("timestamp", msg.get("createdAt"))
        if ts and first_ts is None:
            first_ts = ts
        if ts:
            last_ts = ts

        # Token usage (new JSONL format)
        tokens = msg.get("tokens")
        if isinstance(tokens, dict):
            inp = tokens.get("input", 0)
            cached = tokens.get("cached", 0)
            total = tokens.get("total", 0)
            if isinstance(inp, (int, float)) and inp > 0:
                total_input_tokens += int(inp)
                current_ctx = int(inp)
                peak_ctx = max(peak_ctx, current_ctx)
                recent_contexts.append(current_ctx)
            if isinstance(cached, (int, float)) and cached > 0:
                total_cached_tokens += int(cached)
            if isinstance(total, (int, float)) and total > 0:
                cumul_total += int(total)

        if msg_type == "user":
            user_turns += 1
            msg_content = msg.get("content", msg.get("text", msg.get("message", "")))
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

    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
        if not raw:
            return {"user_turns": 0, "first_ts": None, "last_ts": None,
                    "last_user_text": "", "last_user_ts": None}

        # Detect format: JSONL (multiple lines, first line is session header)
        # vs JSON (single object/array)
        is_jsonl = str(path).endswith(".jsonl")
        if not is_jsonl:
            # Also detect JSONL by checking if first line is a valid JSON object
            # and there are more lines
            first_newline = raw.find("\n")
            if first_newline > 0:
                try:
                    first_obj = json.loads(raw[:first_newline])
                    if isinstance(first_obj, dict) and "sessionId" in first_obj:
                        is_jsonl = True
                except (json.JSONDecodeError, ValueError):
                    pass

        if is_jsonl:
            for line in raw.split("\n"):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(obj, dict):
                    continue
                # Session header
                if "sessionId" in obj and "type" not in obj:
                    if obj.get("startTime"):
                        first_ts = obj["startTime"]
                    if obj.get("lastUpdated"):
                        last_ts = obj["lastUpdated"]
                    continue
                _process_msg(obj)
        else:
            data = json.loads(raw)
            if isinstance(data, dict):
                first_ts = data.get("startTime")
                last_ts = data.get("lastUpdated")
                messages = data.get("messages", [])
            elif isinstance(data, list):
                messages = data
            else:
                messages = []
            for msg in messages:
                _process_msg(msg)
    except (OSError, json.JSONDecodeError):
        pass

    cache_hit_rate = None  # type: Optional[float]
    if total_input_tokens > 0:
        cache_hit_rate = round(total_cached_tokens / total_input_tokens * 100, 2)

    return {
        "user_turns": user_turns,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "last_user_text": last_user_text,
        "last_user_ts": last_user_ts,
        "current_ctx": current_ctx,
        "peak_ctx": peak_ctx,
        "recent_peak": max(recent_contexts[-5:], default=0),
        "cumul_unique": cumul_total,
        "model_window": 0,
        "cache_hit_rate": cache_hit_rate,
    }


def _find_cli_pid(name: str) -> int:
    """Find a running CLI process PID by binary name (e.g., 'gemini', 'codex').

    Searches /proc or psutil for processes whose cmdline contains the CLI binary.
    Returns the PID or 0 if not found.
    """
    import subprocess
    import sys

    if sys.platform == "win32":
        # Windows: use psutil to find by process name
        try:
            from llm_relay.api._compat import _get_psutil
            psutil = _get_psutil()
            if psutil:
                for proc in psutil.process_iter(["pid", "name"]):
                    try:
                        pname = (proc.info.get("name") or "").lower()
                        if pname.startswith(name):
                            return proc.info["pid"]
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
        except Exception:
            pass
        return 0

    try:
        out = subprocess.run(
            ["pgrep", "-f", f"bin/{name}"],
            capture_output=True, text=True, timeout=5,
            stdin=subprocess.DEVNULL,
        )
        for line in out.stdout.strip().split("\n"):
            line = line.strip()
            if line.isdigit():
                return int(line)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return 0


def discover_external_cli_sessions(
    window_hours: float = 4.0,
    include_dead: bool = False,
    check_open_fds: bool = True,
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
        check_open_fds: When False, skip /proc fd scanning and mark sessions dead.
    """
    import time

    cutoff = time.time() - (window_hours * 3600)
    results: list = []

    try:
        from llm_relay.providers import get_all_providers
    except ImportError:
        return results

    import sys

    # On Windows, open_files() requires admin privileges and usually fails.
    # Fall back to mtime-based liveness: if a CLI process is running AND the
    # session file was modified recently (within 10 min), treat it as alive.
    _is_windows = sys.platform == "win32"
    _mtime_alive_window = 600  # 10 minutes

    if _is_windows:
        path_pids = {}  # type: dict
        open_paths = set()  # type: set
    else:
        path_pids = _collect_open_session_path_pids() if check_open_fds else {}
        open_paths = set(path_pids.keys())

    # Detect running CLI processes
    gemini_pid = _find_cli_pid("gemini") if check_open_fds else 0
    codex_running = is_cli_running_cached("codex") if _is_windows else False
    claude_running = is_cli_running_cached("claude") if _is_windows else False

    for provider in get_all_providers():
        # On Linux, CC sessions come from the proxy DB — skip here.
        # On Windows (no proxy), discover CC sessions from files too.
        if provider.provider_id == "claude-code" and not _is_windows:
            continue

        try:
            sessions = provider.discover_sessions(limit=20)
        except Exception:
            continue

        # For Gemini: mark the most recent session as alive if gemini process exists
        gemini_newest_assigned = False

        for sf in sessions:
            if sf.mtime < cutoff:
                continue

            try:
                resolved = str(sf.path.resolve())
            except OSError:
                resolved = str(sf.path)

            if _is_windows:
                # Windows fallback: mtime-based liveness
                recently_modified = (time.time() - sf.mtime) < _mtime_alive_window
                if provider.provider_id == "claude-code":
                    alive = recently_modified and claude_running
                elif provider.provider_id == "openai-codex":
                    alive = recently_modified and codex_running
                elif provider.provider_id == "gemini-cli":
                    alive = recently_modified and bool(gemini_pid)
                else:
                    alive = False
                cli_pid = 0
            else:
                alive = resolved in open_paths if check_open_fds else False
                cli_pid = path_pids.get(resolved, 0) if alive else 0

            # Gemini fallback: assign running gemini pid to newest session
            if not alive and provider.provider_id == "gemini-cli" and gemini_pid and not gemini_newest_assigned:
                alive = True
                cli_pid = gemini_pid
                gemini_newest_assigned = True

            if not alive and not include_dead:
                continue

            # Parse directly based on provider type
            if provider.provider_id == "claude-code":
                info = _parse_cc_session_raw(sf.path)
            elif provider.provider_id == "openai-codex":
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

            current_ctx = info.get("current_ctx", 0)
            peak_ctx = info.get("peak_ctx", 0)
            cumul_unique = info.get("cumul_unique", 0)
            model_window = info.get("model_window", 0)

            if provider.provider_id == "openai-codex":
                display_ceiling = _codex_display_ceiling()
                zones = _codex_compute_zone_bundle(current_ctx, peak_ctx)
                official_context_window = _OPENAI_CODEX_OFFICIAL_CONTEXT_WINDOW
                official_max_output = _OPENAI_CODEX_OFFICIAL_MAX_OUTPUT
            else:
                display_ceiling = int(os.getenv("LLM_TOKEN_CEILING", "665000"))
                official_context_window = 0
                official_max_output = 0
                try:
                    from llm_relay.api._zones import _compute_zone_bundle

                    zones = _compute_zone_bundle(current_ctx, peak_ctx, ceiling=display_ceiling)
                except Exception:
                    zones = {
                        "zone": "green",
                        "zone_a": "green",
                        "zone_b": "green",
                        "zone_a_peak": "green",
                        "zone_b_peak": "green",
                        "message": None,
                        "next_threshold": None,
                    }

            # Composition analysis from session file
            composition = None  # type: Optional[dict]
            try:
                from llm_relay.proxy.composition import analyze_file_composition

                composition = analyze_file_composition(str(sf.path), provider.provider_id)
            except Exception:
                pass

            results.append({
                "session_id": sf.session_id,
                "provider": provider.provider_id,
                "provider_name": provider.display_name,
                "turns": user_turns,
                "first_ts": first_ts,
                "last_ts": last_ts,
                "duration_s": round(duration_s, 1),
                "current_ctx": current_ctx,
                "peak_ctx": peak_ctx,
                "recent_peak": info.get("recent_peak", 0),
                "cumul_unique": cumul_unique,
                "ceiling": display_ceiling,
                "model_window": model_window,
                "official_context_window": official_context_window,
                "official_max_output": official_max_output,
                **zones,
                "last_prompt": info["last_user_text"],
                "last_prompt_ts": info["last_user_ts"],
                "cache_hit_rate": info.get("cache_hit_rate"),
                "tty": _get_process_tty(cli_pid) if cli_pid else None,
                "cc_pid": cli_pid,
                "term_pid": None,
                "term_name": _get_process_terminal_name(cli_pid) if cli_pid else None,
                "connection_type": detect_connection_type(cli_pid) if cli_pid else "unknown",
                "composition": composition,
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
