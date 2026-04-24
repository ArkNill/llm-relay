"""Context composition analyzer — classifies what fills the context window.

Breaks down session context into categories:
  system, user_text, assistant_text, tool_use, tool_result, thinking_overhead

Used by /api/v1/display to provide real-time composition visibility.
Includes in-memory caching keyed by (session_id, max_turn_number).
"""

from __future__ import annotations

import json
import logging
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


# ── Reconstruction ──


def _reconstruct_and_classify(
    turns_data: List[dict],
) -> Tuple[Dict[str, int], int, Dict[str, int]]:
    """Reconstruct full context from delta/full turns, classify final state.

    Returns (category_bytes, total_bytes, duplicate_reads).
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

        # Track reads
        for msg in messages:
            for target in _extract_read_targets(msg):
                read_counts[target] += 1

    # Classify accumulated context
    totals: Dict[str, int] = {cat: 0 for cat in CATEGORIES}
    for msg in accumulated:
        sizes = _classify_message(msg)
        for cat in CATEGORIES:
            totals[cat] += sizes.get(cat, 0)

    total_bytes = sum(totals.values())
    dupes = {k: v for k, v in read_counts.items() if v > 1}

    return totals, total_bytes, dupes


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
    category_bytes, total_bytes, dupes = _reconstruct_and_classify(turns_data)

    # Build result
    categories = {}
    for cat in CATEGORIES:
        b = category_bytes[cat]
        pct = round(b / total_bytes * 100, 1) if total_bytes > 0 else 0.0
        categories[cat] = {"bytes": b, "pct": pct}

    signal = category_bytes["user_text"] + category_bytes["assistant_text"]
    noise = category_bytes["tool_result"] + category_bytes["thinking_overhead"]
    snr = round(signal / max(noise, 1), 2)

    result = {
        "categories": categories,
        "total_bytes": total_bytes,
        "est_tokens": total_bytes // 4,
        "snr": snr,
        "duplicate_read_count": len(dupes),
    }

    # Cache
    _cache[session_id] = (max_turn, result)

    return result


def clear_cache(session_id: Optional[str] = None) -> None:
    """Clear composition cache for a session or all sessions."""
    if session_id:
        _cache.pop(session_id, None)
    else:
        _cache.clear()
