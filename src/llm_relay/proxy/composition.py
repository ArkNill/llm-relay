"""Context composition analyzer — classifies what fills the context window.

Breaks down session context into categories:
  system, user_text, assistant_text, tool_use, tool_result, thinking_overhead

Used by /api/v1/display to provide real-time composition visibility.
Includes in-memory caching keyed by (session_id, max_turn_number).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("llm-relay.composition")

CATEGORIES = [
    "system",
    "user_text",
    "assistant_text",
    "tool_use",
    "tool_result",
    "thinking_overhead",
]

# In-memory cache: session_id -> (max_turn_number, result_dict)
_cache: Dict[str, Tuple[int, dict]] = {}


# ── Classification ──


def _block_size(block: Any) -> int:
    """Estimate serialized size of a content block in bytes."""
    if isinstance(block, str):
        return len(block.encode("utf-8"))
    return len(json.dumps(block, ensure_ascii=False).encode("utf-8"))


def _classify_content_blocks(content: Any) -> Dict[str, int]:
    """Classify content (str or list of blocks) into type -> byte size."""
    sizes: Dict[str, int] = defaultdict(int)

    if isinstance(content, str):
        sizes["text"] += len(content.encode("utf-8"))
        return dict(sizes)

    if not isinstance(content, list):
        sizes["text"] += _block_size(content)
        return dict(sizes)

    for block in content:
        if not isinstance(block, dict):
            sizes["text"] += _block_size(block)
            continue

        btype = block.get("type", "text")
        if btype == "thinking":
            sizes["thinking"] += _block_size(block)
        elif btype == "tool_use":
            sizes["tool_use"] += _block_size(block)
        elif btype == "tool_result":
            sizes["tool_result"] += _block_size(block)
        else:
            sizes["text"] += _block_size(block)

    return dict(sizes)


def _classify_message(msg: dict) -> Dict[str, int]:
    """Classify a single message into composition categories."""
    role = msg.get("role", "")
    content = msg.get("content", "")
    block_sizes = _classify_content_blocks(content)

    result: Dict[str, int] = defaultdict(int)

    if role == "system":
        for v in block_sizes.values():
            result["system"] += v
    elif role == "user":
        result["user_text"] += block_sizes.get("text", 0)
        result["tool_result"] += block_sizes.get("tool_result", 0)
        result["tool_use"] += block_sizes.get("tool_use", 0)
    elif role == "assistant":
        result["assistant_text"] += block_sizes.get("text", 0)
        result["tool_use"] += block_sizes.get("tool_use", 0)
        result["thinking_overhead"] += block_sizes.get("thinking", 0)
    else:
        result["user_text"] += sum(block_sizes.values())

    return dict(result)


def _extract_read_targets(msg: dict) -> List[str]:
    """Extract file paths from Read tool_use blocks for duplicate detection."""
    targets: List[str] = []
    content = msg.get("content", "")
    if not isinstance(content, list):
        return targets

    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name", "")
        inp = block.get("input", {})
        if name == "Read" and isinstance(inp, dict):
            fp = inp.get("file_path", "")
            if fp:
                targets.append(fp)

    return targets


def _extract_tool_names(msg: dict) -> List[str]:
    """Extract tool names from tool_use blocks in a CC message."""
    names: List[str] = []
    content = msg.get("content", "")
    if not isinstance(content, list):
        return names
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name", "")
        if name:
            names.append(name)
    return names


def _extract_tool_use_bytes(msg: dict) -> Dict[str, Dict[str, int]]:
    """Extract per-tool byte breakdown from a CC message.

    Returns {tool_name: {"use": bytes}} for tool_use blocks,
    and {"__result__": {"result": bytes}} for tool_result blocks.
    """
    result: Dict[str, Dict[str, int]] = defaultdict(lambda: {"use": 0, "result": 0})
    content = msg.get("content", "")
    if not isinstance(content, list):
        return dict(result)
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype == "tool_use":
            name = block.get("name", "unknown")
            result[name]["use"] += _block_size(block)
        elif btype == "tool_result":
            # tool_result doesn't have tool name directly, track as aggregate
            result["__result__"]["result"] += _block_size(block)
    return dict(result)


def _count_thinking_blocks(msg: dict) -> int:
    """Count thinking blocks in a CC assistant message."""
    content = msg.get("content", "")
    if not isinstance(content, list):
        return 0
    return sum(1 for b in content if isinstance(b, dict) and b.get("type") == "thinking")


# ── Reconstruction ──


def _reconstruct_and_classify(
    turns_data: List[dict],
) -> Tuple[Dict[str, int], int, Dict[str, int], Dict[str, int], Dict[str, Dict[str, int]], int]:
    """Reconstruct full context from delta/full turns, classify final state.

    Returns (category_bytes, total_bytes, duplicate_reads, tool_calls, tool_bytes, thinking_count).
    """
    accumulated: List[dict] = []
    read_counts: Dict[str, int] = defaultdict(int)

    for row in turns_data:
        storage_mode = row["storage_mode"]
        req_json = row["request_messages"]

        if not req_json:
            continue

        try:
            messages = json.loads(req_json)
        except (json.JSONDecodeError, TypeError):
            continue

        if not isinstance(messages, list):
            continue

        if storage_mode == "full":
            accumulated = list(messages)
        else:
            accumulated.extend(messages)

        # Track reads and tool calls
        for msg in messages:
            for target in _extract_read_targets(msg):
                read_counts[target] += 1

    # Classify accumulated context + extract tool details
    totals: Dict[str, int] = {cat: 0 for cat in CATEGORIES}
    tool_call_counts: Dict[str, int] = defaultdict(int)
    tool_byte_totals: Dict[str, Dict[str, int]] = defaultdict(lambda: {"use": 0, "result": 0})
    thinking_count = 0

    for msg in accumulated:
        sizes = _classify_message(msg)
        for cat in CATEGORIES:
            totals[cat] += sizes.get(cat, 0)
        # Per-tool counting
        for name in _extract_tool_names(msg):
            tool_call_counts[name] += 1
        for name, bytes_info in _extract_tool_use_bytes(msg).items():
            if name == "__result__":
                continue
            tool_byte_totals[name]["use"] += bytes_info["use"]
            tool_byte_totals[name]["result"] += bytes_info["result"]
        thinking_count += _count_thinking_blocks(msg)

    total_bytes = sum(totals.values())
    dupes = {k: v for k, v in read_counts.items() if v > 1}

    return totals, total_bytes, dupes, dict(tool_call_counts), dict(tool_byte_totals), thinking_count


# ── Public API ──


def analyze_session_composition(
    conn: sqlite3.Connection,
    session_id: str,
) -> Optional[dict]:
    """Analyze context composition for a session. Returns cached result if available.

    Returns dict with: categories, total_bytes, est_tokens, snr, duplicate_read_count
    Returns None if no history data exists.
    """
    # Check current max turn
    row = conn.execute(
        "SELECT MAX(turn_number) FROM conversation_turns WHERE session_id = ?",
        (session_id,),
    ).fetchone()

    if not row or row[0] is None:
        return None

    max_turn = row[0]

    # Cache check
    cached = _cache.get(session_id)
    if cached and cached[0] == max_turn:
        return cached[1]

    # Fetch turns
    turns = conn.execute(
        """SELECT turn_number, storage_mode, request_messages
           FROM conversation_turns
           WHERE session_id = ?
           ORDER BY turn_number ASC""",
        (session_id,),
    ).fetchall()

    if not turns:
        return None

    turns_data = [dict(t) for t in turns]
    category_bytes, total_bytes, dupes, tool_calls, tool_bytes, thinking_count = (
        _reconstruct_and_classify(turns_data)
    )

    result = _build_composition_result(
        category_bytes, dupes, tool_calls, tool_bytes,
        thinking_count=thinking_count,
    )

    # Cache
    _cache[session_id] = (max_turn, result)

    return result


# ── Per-turn analysis ──

_per_turn_cache: Dict[str, Tuple[int, dict]] = {}


def _reconstruct_per_turn(
    turns_data: List[dict],
) -> List[dict]:
    """Reconstruct full context at each turn and return per-turn composition.

    Similar to _reconstruct_and_classify but returns composition at every turn
    rather than just the final state.
    """
    accumulated: List[dict] = []
    results: List[dict] = []

    for row in turns_data:
        turn_number = row["turn_number"]
        storage_mode = row["storage_mode"]
        req_json = row["request_messages"]

        if not req_json:
            continue

        try:
            messages = json.loads(req_json)
        except (json.JSONDecodeError, TypeError):
            continue

        if not isinstance(messages, list):
            continue

        compacted = False
        if storage_mode == "full":
            if accumulated and len(messages) < len(accumulated):
                compacted = True
            accumulated = list(messages)
        else:
            accumulated.extend(messages)

        # Classify full accumulated context at this turn
        totals: Dict[str, int] = {cat: 0 for cat in CATEGORIES}
        for msg in accumulated:
            sizes = _classify_message(msg)
            for cat in CATEGORIES:
                totals[cat] += sizes.get(cat, 0)

        total_bytes = sum(totals.values())
        categories = {}
        for cat in CATEGORIES:
            b = totals[cat]
            pct = round(b / total_bytes * 100, 1) if total_bytes > 0 else 0.0
            categories[cat] = {"bytes": b, "pct": pct}

        results.append({
            "turn": turn_number,
            "msgs": len(accumulated),
            "compacted": compacted,
            "composition": {
                "total_bytes": total_bytes,
                "est_tokens": total_bytes // 4,
                "categories": categories,
            },
        })

    return results


def _sample_turns(turns: List[dict]) -> List[dict]:
    """Sample turns for large sessions: first 5 + every 10th + last 5."""
    n = len(turns)
    if n <= 50:
        return turns
    indices = set(range(min(5, n)))
    indices.update(range(0, n, 10))
    indices.update(range(max(0, n - 5), n))
    return [turns[i] for i in sorted(indices)]


def analyze_session_composition_per_turn(
    conn: sqlite3.Connection,
    session_id: str,
) -> Optional[dict]:
    """Analyze per-turn composition for a session. Returns sampled data for large sessions.

    Returns dict with: session_id, total_turns, sampled, turns[].
    Returns None if no history data exists.
    """
    row = conn.execute(
        "SELECT MAX(turn_number) FROM conversation_turns WHERE session_id = ?",
        (session_id,),
    ).fetchone()

    if not row or row[0] is None:
        return None

    max_turn = row[0]

    # Cache check
    cached = _per_turn_cache.get(session_id)
    if cached and cached[0] == max_turn:
        return cached[1]

    turns = conn.execute(
        """SELECT turn_number, storage_mode, request_messages
           FROM conversation_turns
           WHERE session_id = ?
           ORDER BY turn_number ASC""",
        (session_id,),
    ).fetchall()

    if not turns:
        return None

    turns_data = [dict(t) for t in turns]
    all_turns = _reconstruct_per_turn(turns_data)

    sampled = len(all_turns) > 50
    sampled_turns = _sample_turns(all_turns) if sampled else all_turns

    result = {
        "session_id": session_id,
        "total_turns": len(all_turns),
        "sampled": sampled,
        "turns": sampled_turns,
    }

    _per_turn_cache[session_id] = (max_turn, result)
    return result


def clear_cache(session_id: Optional[str] = None) -> None:
    """Clear composition cache for a session or all sessions."""
    if session_id:
        _cache.pop(session_id, None)
        _per_turn_cache.pop(session_id, None)
        _file_cache.pop(session_id, None)
    else:
        _cache.clear()
        _per_turn_cache.clear()
        _file_cache.clear()


# ── File-based composition (Codex / Gemini) ──

# path_str -> (mtime, result_dict)
_file_cache: Dict[str, Tuple[float, dict]] = {}

_MAX_FILE_READ = 2 * 1024 * 1024  # 2 MB cap for session file reads


# Regex for extracting file paths from shell commands used for reading
# Matches: sed -n '...' FILE, nl -ba FILE, cat FILE, head/tail FILE
_RE_READ_CMD = re.compile(
    r"(?:sed\s+-n\s+'[^']*'\s+|nl\s+(?:-\w+\s+)*|cat\s+|head\s+(?:-\w+\s+)*|tail\s+(?:-\w+\s+)*)"
    r"([^\s|;&>]+\.\w+)"
)


def _extract_codex_read_targets(obj: dict) -> List[str]:
    """Extract file paths from Codex exec_command function_call entries."""
    targets: List[str] = []
    if obj.get("type") != "response_item":
        return targets
    payload = obj.get("payload", {})
    if payload.get("type") != "function_call" or payload.get("name") != "exec_command":
        return targets
    args_str = payload.get("arguments", "")
    if not isinstance(args_str, str):
        return targets
    try:
        args = json.loads(args_str)
    except (json.JSONDecodeError, ValueError):
        return targets
    cmd = args.get("cmd", "")
    if not cmd:
        return targets
    # Split piped commands and check each segment
    for segment in re.split(r"[|&;]", cmd):
        segment = segment.strip()
        for match in _RE_READ_CMD.finditer(segment):
            fp = match.group(1)
            if fp and not fp.startswith("-"):
                targets.append(fp)
    return targets


def _classify_codex_entry(obj: dict) -> Dict[str, int]:
    """Classify a single Codex JSONL entry into composition categories."""
    sizes: Dict[str, int] = defaultdict(int)
    entry_type = obj.get("type", "")

    if entry_type == "response_item":
        payload = obj.get("payload", {})
        role = payload.get("role", "")
        ptype = payload.get("type", "")

        content = payload.get("content", [])
        content_bytes = 0
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text", "")
                    if text:
                        content_bytes += len(text.encode("utf-8"))
                elif isinstance(part, str):
                    content_bytes += len(part.encode("utf-8"))
        elif isinstance(content, str):
            content_bytes = len(content.encode("utf-8"))

        if ptype == "function_call":
            args = payload.get("arguments", "")
            sizes["tool_use"] += len(args.encode("utf-8")) if isinstance(args, str) else 0
            sizes["tool_use"] += content_bytes
        elif ptype == "function_call_output":
            output = payload.get("output", "")
            sizes["tool_result"] += len(output.encode("utf-8")) if isinstance(output, str) else 0
            sizes["tool_result"] += content_bytes
        elif ptype == "reasoning":
            # Reasoning content is encrypted — estimate from summary or use minimal size
            summary = payload.get("summary", [])
            if isinstance(summary, list):
                for s in summary:
                    if isinstance(s, dict):
                        t = s.get("text", "")
                        sizes["thinking_overhead"] += len(t.encode("utf-8")) if t else 0
            encrypted = payload.get("encrypted_content", "")
            if encrypted:
                sizes["thinking_overhead"] += len(encrypted) // 2  # rough estimate
        elif ptype in ("custom_tool_call",):
            sizes["tool_use"] += content_bytes
        elif ptype in ("custom_tool_call_output",):
            sizes["tool_result"] += content_bytes
        elif role == "developer":
            sizes["system"] += content_bytes
        elif role == "user":
            sizes["user_text"] += content_bytes
        elif role == "assistant":
            sizes["assistant_text"] += content_bytes

    elif entry_type == "session_meta":
        payload = obj.get("payload", {})
        instructions = payload.get("base_instructions", {})
        if isinstance(instructions, dict):
            text = instructions.get("text", "")
            sizes["system"] += len(text.encode("utf-8")) if text else 0

    return dict(sizes)


def _extract_codex_tool_info(obj: dict) -> Optional[dict]:
    """Extract tool detail from a Codex JSONL entry.

    Returns dict with: name, use_bytes, result_bytes, status, duration_ms
    or None if entry is not a tool-related entry.
    """
    entry_type = obj.get("type", "")

    if entry_type == "response_item":
        payload = obj.get("payload", {})
        ptype = payload.get("type", "")
        if ptype == "function_call":
            name = payload.get("name", "exec_command")
            args = payload.get("arguments", "")
            use_bytes = len(args.encode("utf-8")) if isinstance(args, str) else 0
            return {"name": name, "use_bytes": use_bytes, "result_bytes": 0}
        if ptype == "function_call_output":
            output = payload.get("output", "")
            result_bytes = len(output.encode("utf-8")) if isinstance(output, str) else 0
            return {"name": "__output__", "result_bytes": result_bytes, "use_bytes": 0}
        if ptype == "reasoning":
            return {"name": "__reasoning__", "use_bytes": 0, "result_bytes": 0}
        if ptype in ("custom_tool_call",):
            return {"name": "custom_tool", "use_bytes": 0, "result_bytes": 0}

    if entry_type == "event_msg":
        payload = obj.get("payload", {})
        if payload.get("type") == "exec_command_end":
            duration = payload.get("duration", {})
            duration_ms = 0
            if isinstance(duration, dict):
                secs = duration.get("secs", 0)
                nanos = duration.get("nanos", 0)
                duration_ms = int(secs * 1000 + nanos / 1_000_000)
            exit_code = payload.get("exit_code", -1)
            return {
                "name": "__exec_end__",
                "status": "success" if exit_code == 0 else "failed",
                "duration_ms": duration_ms,
                "use_bytes": 0, "result_bytes": 0,
            }

    return None


def _classify_gemini_entry(msg: dict) -> Dict[str, int]:
    """Classify a single Gemini message into composition categories."""
    sizes: Dict[str, int] = defaultdict(int)
    # Skip $set metadata and session headers
    if "$set" in msg or ("sessionId" in msg and "type" not in msg):
        return dict(sizes)
    msg_type = msg.get("type", msg.get("role", ""))

    content = msg.get("message", msg.get("content", msg.get("text", "")))
    content_bytes = 0
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                t = part.get("text", "")
                content_bytes += len(t.encode("utf-8")) if t else 0
            elif isinstance(part, str):
                content_bytes += len(part.encode("utf-8"))
    elif isinstance(content, str):
        content_bytes = len(content.encode("utf-8"))

    if msg_type == "user":
        sizes["user_text"] += content_bytes
    elif msg_type in ("gemini", "assistant", "model"):
        sizes["assistant_text"] += content_bytes
        # toolCalls: [{name, args, result}] embedded in gemini entries
        tool_calls = msg.get("toolCalls", [])
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                args = tc.get("args", {})
                args_bytes = len(json.dumps(args, ensure_ascii=False).encode("utf-8")) if args else 0
                sizes["tool_use"] += args_bytes
                result = tc.get("result", [])
                result_bytes = len(json.dumps(result, ensure_ascii=False).encode("utf-8")) if result else 0
                sizes["tool_result"] += result_bytes
        # thoughts as thinking_overhead
        thoughts = msg.get("thoughts", [])
        if isinstance(thoughts, list):
            for th in thoughts:
                if isinstance(th, dict):
                    desc = th.get("description", "")
                    sizes["thinking_overhead"] += len(desc.encode("utf-8")) if desc else 0
    elif msg_type == "system":
        sizes["system"] += content_bytes
    elif msg_type == "tool":
        sizes["tool_result"] += content_bytes
    elif msg_type == "function_call":
        sizes["tool_use"] += content_bytes
    else:
        sizes["user_text"] += content_bytes

    return dict(sizes)


def _extract_gemini_tool_info(msg: dict) -> List[dict]:
    """Extract per-tool detail from a Gemini message.

    Returns list of dicts with: name, use_bytes, result_bytes, status.
    Also returns thinking entries as {"name": "__thinking__", ...}.
    """
    results: List[dict] = []
    if not isinstance(msg, dict):
        return results

    tool_calls = msg.get("toolCalls", [])
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            name = tc.get("name", "unknown")
            args = tc.get("args", {})
            use_bytes = len(json.dumps(args, ensure_ascii=False).encode("utf-8")) if args else 0
            result = tc.get("result", [])
            result_bytes = len(json.dumps(result, ensure_ascii=False).encode("utf-8")) if result else 0
            status = tc.get("status", "")
            results.append({
                "name": name,
                "use_bytes": use_bytes,
                "result_bytes": result_bytes,
                "status": status,
            })

    thoughts = msg.get("thoughts", [])
    if isinstance(thoughts, list) and thoughts:
        total_thought_bytes = 0
        for th in thoughts:
            if isinstance(th, dict):
                desc = th.get("description", "")
                total_thought_bytes += len(desc.encode("utf-8")) if desc else 0
        if total_thought_bytes > 0:
            results.append({
                "name": "__thinking__",
                "use_bytes": total_thought_bytes,
                "result_bytes": 0,
                "status": "",
                "count": len(thoughts),
            })

    return results


def _build_composition_result(
    totals: Dict[str, int],
    dupes: Optional[Dict[str, int]] = None,
    tool_calls: Optional[Dict[str, int]] = None,
    tool_bytes: Optional[Dict[str, Dict[str, int]]] = None,
    exec_stats: Optional[Dict[str, Any]] = None,
    thinking_count: int = 0,
) -> dict:
    """Build a composition result dict from category byte totals."""
    if dupes is None:
        dupes = {}
    if tool_calls is None:
        tool_calls = {}
    if tool_bytes is None:
        tool_bytes = {}

    total_bytes = sum(totals.get(cat, 0) for cat in CATEGORIES)

    categories = {}
    for cat in CATEGORIES:
        b = totals.get(cat, 0)
        pct = round(b / total_bytes * 100, 1) if total_bytes > 0 else 0.0
        categories[cat] = {"bytes": b, "pct": pct}

    signal = totals.get("user_text", 0) + totals.get("assistant_text", 0)
    noise = totals.get("tool_result", 0) + totals.get("thinking_overhead", 0)
    snr = round(signal / max(noise, 1), 2)

    snr_warning = float(os.getenv("LLM_SNR_WARNING", "0.3"))
    snr_recommendation = None  # type: Optional[str]
    if total_bytes > 0 and snr < snr_warning:
        snr_recommendation = (
            "SNR {snr:.2f} — tool results dominate context. "
            "Consider starting a new session to restore signal quality."
        ).format(snr=snr)

    dupe_warn_threshold = int(os.getenv("DUPLICATE_READ_WARN_THRESHOLD", "5"))
    dupe_warning = any(v >= dupe_warn_threshold for v in dupes.values())

    result = {
        "categories": categories,
        "total_bytes": total_bytes,
        "est_tokens": total_bytes // 4,
        "snr": snr,
        "snr_recommendation": snr_recommendation,
        "duplicate_read_count": len(dupes),
        "duplicate_reads": dupes,
        "duplicate_read_warning": dupe_warning,
        "tool_calls": tool_calls,
        "tool_bytes": tool_bytes,
        "thinking_count": thinking_count,
    }
    if exec_stats:
        result["exec_stats"] = exec_stats
    return result


def analyze_file_composition(
    path: str,
    provider_id: str,
) -> Optional[dict]:
    """Analyze composition from a session file (Codex JSONL or Gemini JSON).

    Returns the same dict format as analyze_session_composition():
      categories, total_bytes, est_tokens, snr, duplicate_read_count, etc.
    Returns None if the file cannot be read or has no data.
    """
    from pathlib import Path

    fpath = Path(path)
    try:
        stat = fpath.stat()
    except OSError:
        return None

    mtime = stat.st_mtime
    cached = _file_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    totals: Dict[str, int] = {cat: 0 for cat in CATEGORIES}
    read_counts: Dict[str, int] = defaultdict(int)
    tool_call_counts: Dict[str, int] = defaultdict(int)
    tool_byte_totals: Dict[str, Dict[str, int]] = defaultdict(lambda: {"use": 0, "result": 0})
    thinking_count = 0
    exec_total = 0
    exec_success = 0
    exec_failed = 0
    exec_durations: List[int] = []

    try:
        if provider_id == "openai-codex":
            # JSONL — read up to _MAX_FILE_READ bytes
            size = stat.st_size
            # Track last function_call name for correlating with output
            last_call_name = "exec_command"
            with open(fpath, encoding="utf-8", errors="replace") as f:
                if size > _MAX_FILE_READ:
                    f.seek(size - _MAX_FILE_READ)
                    f.readline()  # skip partial line
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        obj = json.loads(stripped)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if not isinstance(obj, dict):
                        continue
                    entry_sizes = _classify_codex_entry(obj)
                    for cat in CATEGORIES:
                        totals[cat] += entry_sizes.get(cat, 0)
                    for target in _extract_codex_read_targets(obj):
                        read_counts[target] += 1

                    # Tool detail extraction
                    info = _extract_codex_tool_info(obj)
                    if info:
                        name = info["name"]
                        if name == "__exec_end__":
                            exec_total += 1
                            if info.get("status") == "success":
                                exec_success += 1
                            else:
                                exec_failed += 1
                            if info.get("duration_ms", 0) > 0:
                                exec_durations.append(info["duration_ms"])
                        elif name == "__output__":
                            tool_byte_totals[last_call_name]["result"] += info["result_bytes"]
                        elif name == "__reasoning__":
                            thinking_count += 1
                        elif name != "custom_tool":
                            tool_call_counts[name] += 1
                            tool_byte_totals[name]["use"] += info["use_bytes"]
                            last_call_name = name
                        else:
                            tool_call_counts[name] += 1

        elif provider_id == "gemini-cli":
            raw = fpath.read_text(encoding="utf-8", errors="replace").strip()
            if not raw:
                return None

            # Detect JSONL vs JSON
            is_jsonl = str(fpath).endswith(".jsonl")
            if not is_jsonl:
                first_nl = raw.find("\n")
                if first_nl > 0:
                    try:
                        first_obj = json.loads(raw[:first_nl])
                        if isinstance(first_obj, dict) and "sessionId" in first_obj:
                            is_jsonl = True
                    except (json.JSONDecodeError, ValueError):
                        pass

            messages = []  # type: List[dict]
            if is_jsonl:
                for line in raw.split("\n"):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        obj = json.loads(stripped)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if isinstance(obj, dict):
                        messages.append(obj)
            else:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    return None
                if isinstance(data, dict):
                    messages = data.get("messages", [])
                elif isinstance(data, list):
                    messages = data
                else:
                    return None

            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                entry_sizes = _classify_gemini_entry(msg)
                for cat in CATEGORIES:
                    totals[cat] += entry_sizes.get(cat, 0)
                # Tool detail extraction
                for ti in _extract_gemini_tool_info(msg):
                    name = ti["name"]
                    if name == "__thinking__":
                        thinking_count += ti.get("count", 1)
                    else:
                        tool_call_counts[name] += 1
                        tool_byte_totals[name]["use"] += ti["use_bytes"]
                        tool_byte_totals[name]["result"] += ti["result_bytes"]
                        if ti.get("status") == "success":
                            exec_success += 1
                            exec_total += 1
                        elif ti.get("status") == "failed":
                            exec_failed += 1
                            exec_total += 1
        else:
            return None
    except OSError:
        return None

    total_bytes = sum(totals.get(cat, 0) for cat in CATEGORIES)
    if total_bytes == 0:
        return None

    dupes = {k: v for k, v in read_counts.items() if v > 1}
    exec_stats = None  # type: Optional[Dict[str, Any]]
    if exec_total > 0:
        avg_ms = int(sum(exec_durations) / len(exec_durations)) if exec_durations else 0
        exec_stats = {
            "total": exec_total,
            "success": exec_success,
            "failed": exec_failed,
        }
        if avg_ms > 0:
            exec_stats["avg_duration_ms"] = avg_ms
    result = _build_composition_result(
        totals, dupes, dict(tool_call_counts), dict(tool_byte_totals),
        exec_stats, thinking_count,
    )
    _file_cache[path] = (mtime, result)
    return result
