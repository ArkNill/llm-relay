"""Context composition analyzer — classifies what fills the context window.

Breaks down session context into categories:
  system, user_text, assistant_text, tool_use, tool_result, thinking_overhead

Used by /api/v1/display to provide real-time composition visibility.
Results are cached in memory per session (incremental, bounded by entry count
and idle time -- see _BoundedCache).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
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


@dataclass
class _CompositionState:
    """Cumulative state for incremental composition analysis.

    Built by folding turns one at a time. On storage_mode="full" (compaction),
    totals/tool counts/thinking_count/read_counts all reset and then absorb
    the snapshot. On "delta", everything is added to. Only these aggregates
    are kept, not the messages: every category is a per-message sum, and the
    message list this state used to carry was never read yet made up nearly
    all of the cache's memory (49 sessions: 45 MB with it, 0.2 MB without,
    2026-09-29).
    """
    max_turn_processed: int = 0
    read_counts: Dict[str, int] = field(default_factory=dict)
    totals: Dict[str, int] = field(default_factory=lambda: {cat: 0 for cat in CATEGORIES})
    tool_call_counts: Dict[str, int] = field(default_factory=dict)
    tool_byte_totals: Dict[str, Dict[str, int]] = field(default_factory=dict)
    thinking_count: int = 0


@dataclass
class _PerTurnState:
    """Cumulative state for incremental per-turn composition analysis.

    Keeps running category totals and the context's message count instead of
    the messages. Adding each turn's delta gives the same numbers as
    re-classifying the whole context at every turn, without that
    O(turns x messages) work (3.75 s on the event loop for one 692-turn
    session on 2026-09-29) or a retained copy of the context.
    """
    max_turn_processed: int = 0
    msg_count: int = 0
    totals: Dict[str, int] = field(default_factory=lambda: {cat: 0 for cat in CATEGORIES})
    turn_results: List[dict] = field(default_factory=list)


# ── Cache bounds ──

# Nothing else ever empties the caches below (clear_cache() has no production
# caller), so without a bound every session and session file seen during the
# process lifetime would stay resident. Eviction only costs a cold reload on
# the next access, which yields the same result. 0 disables a bound.
_CACHE_MAX = int(os.getenv("LLM_RELAY_COMPOSITION_CACHE_MAX", "64"))
_CACHE_TTL_S = float(os.getenv("LLM_RELAY_COMPOSITION_CACHE_TTL_S", "28800"))
_clock = time.monotonic


class _BoundedCache:
    """Key -> value map bounded by entry count (least recently used first) and idle time.

    Only touched from the event loop thread (the API handlers call the
    analyzers synchronously), so it needs no lock.
    """

    def __init__(self) -> None:
        self._data: OrderedDict[str, Tuple[float, Any]] = OrderedDict()
        self.evicted = 0

    def get(self, key: str) -> Any:
        now = _clock()
        self._expire(now)
        item = self._data.get(key)
        if item is None:
            return None
        self._data[key] = (now, item[1])
        self._data.move_to_end(key)
        return item[1]

    def put(self, key: str, value: Any) -> None:
        now = _clock()
        self._data[key] = (now, value)
        self._data.move_to_end(key)
        self._expire(now)
        while _CACHE_MAX > 0 and len(self._data) > _CACHE_MAX:
            self._data.popitem(last=False)
            self.evicted += 1

    def pop(self, key: str, default: Any = None) -> Any:
        item = self._data.pop(key, None)
        return default if item is None else item[1]

    def sweep(self) -> None:
        """Drop idle entries now instead of on the next get/put."""
        self._expire(_clock())

    def clear(self) -> None:
        self._data.clear()

    def values(self) -> List[Any]:
        return [value for _ts, value in self._data.values()]

    def __getitem__(self, key: str) -> Any:
        return self._data[key][1]

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)

    def _expire(self, now: float) -> None:
        # Entries are kept in access order, so the first one is the idlest.
        while self._data and _CACHE_TTL_S > 0:
            key, (ts, _value) = next(iter(self._data.items()))
            if now - ts <= _CACHE_TTL_S:
                break
            del self._data[key]
            self.evicted += 1


# In-memory cache: session_id -> _CompositionState
_cache = _BoundedCache()


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


def _classify_messages_delta(
    messages: List[dict],
) -> Tuple[Dict[str, int], Dict[str, int], Dict[str, Dict[str, int]], int]:
    """Classify a list of messages into category byte totals + tool details.

    Returns (totals, tool_call_counts, tool_byte_totals, thinking_count) where each
    value is the contribution from `messages` alone. Callers either use the result
    directly (final-state classification) or fold it into a running state
    (incremental classification).
    """
    totals: Dict[str, int] = {cat: 0 for cat in CATEGORIES}
    tool_call_counts: Dict[str, int] = defaultdict(int)
    tool_byte_totals: Dict[str, Dict[str, int]] = defaultdict(lambda: {"use": 0, "result": 0})
    thinking_count = 0

    for msg in messages:
        sizes = _classify_message(msg)
        for cat in CATEGORIES:
            totals[cat] += sizes.get(cat, 0)
        for name in _extract_tool_names(msg):
            tool_call_counts[name] += 1
        for name, bytes_info in _extract_tool_use_bytes(msg).items():
            if name == "__result__":
                continue
            tool_byte_totals[name]["use"] += bytes_info["use"]
            tool_byte_totals[name]["result"] += bytes_info["result"]
        thinking_count += _count_thinking_blocks(msg)

    return totals, dict(tool_call_counts), dict(tool_byte_totals), thinking_count


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
            messages = req_json if isinstance(req_json, list) else json.loads(req_json)
        except (json.JSONDecodeError, TypeError):
            continue

        if not isinstance(messages, list):
            continue

        if storage_mode == "full":
            accumulated = list(messages)
            # Compaction replaces the accumulated context, so prior reads
            # are no longer present. Reset read_counts so duplicate_reads
            # reflects only files still in the current accumulated state.
            read_counts = defaultdict(int)
        else:
            accumulated.extend(messages)

        for msg in messages:
            for target in _extract_read_targets(msg):
                read_counts[target] += 1

    totals, tool_call_counts, tool_byte_totals, thinking_count = _classify_messages_delta(accumulated)

    total_bytes = sum(totals.values())
    dupes = {k: v for k, v in read_counts.items() if v > 1}

    return totals, total_bytes, dupes, tool_call_counts, tool_byte_totals, thinking_count


# ── Incremental folding ──


def _parse_turn_messages(row: dict) -> Optional[List[dict]]:
    """Parse request_messages from a turn row. Returns None if unusable."""
    req_json = row["request_messages"]
    if not req_json:
        return None
    try:
        messages = req_json if isinstance(req_json, list) else json.loads(req_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(messages, list):
        return None
    return messages


def _fold_delta_into_composition_state(
    state: _CompositionState,
    new_turns: List[dict],
) -> None:
    """Fold delta turns into the state in place.

    Matches the semantics of _reconstruct_and_classify: storage_mode="full"
    resets totals/tool_calls/tool_bytes/thinking/read_counts and re-absorbs
    the snapshot; other modes add to them.
    """
    for row in new_turns:
        turn_number = row["turn_number"]
        storage_mode = row["storage_mode"]
        messages = _parse_turn_messages(row)
        if messages is None:
            state.max_turn_processed = turn_number
            continue

        if storage_mode == "full":
            state.read_counts = {}
            state.totals = {cat: 0 for cat in CATEGORIES}
            state.tool_call_counts = {}
            state.tool_byte_totals = {}
            state.thinking_count = 0

        d_totals, d_calls, d_bytes, d_thinking = _classify_messages_delta(messages)
        for cat in CATEGORIES:
            state.totals[cat] += d_totals[cat]
        for name, count in d_calls.items():
            state.tool_call_counts[name] = state.tool_call_counts.get(name, 0) + count
        for name, info in d_bytes.items():
            existing = state.tool_byte_totals.setdefault(name, {"use": 0, "result": 0})
            existing["use"] += info["use"]
            existing["result"] += info["result"]
        state.thinking_count += d_thinking

        for msg in messages:
            for target in _extract_read_targets(msg):
                state.read_counts[target] = state.read_counts.get(target, 0) + 1

        state.max_turn_processed = turn_number


def _build_composition_result_from_state(state: _CompositionState) -> dict:
    """Render a result dict from accumulated state."""
    dupes = {k: v for k, v in state.read_counts.items() if v > 1}
    return _build_composition_result(
        state.totals,
        dupes=dupes,
        tool_calls=state.tool_call_counts,
        tool_bytes=state.tool_byte_totals,
        thinking_count=state.thinking_count,
    )


def _cold_start_turn(conn: sqlite3.Connection, session_id: str) -> int:
    """turn_number of the newest usable "full" snapshot for a session, or 0 when none.

    Folding a "full" row resets every field of _CompositionState before
    re-absorbing the snapshot, so folding from the newest full row yields the
    same state as folding from turn 1. A cold cache therefore only needs the
    rows from that snapshot onward. Loading the whole session instead
    OOM-killed the relay on 2026-09-29: one image-heavy session held 3.67 GB
    of stored turns, and the first dashboard poll after every restart pulled
    all of it into memory.
    """
    row = conn.execute(
        """SELECT MAX(turn_number) AS max_full FROM conversation_turns
           WHERE session_id = ? AND storage_mode = 'full'
             AND request_messages IS NOT NULL""",
        (session_id,),
    ).fetchone()
    if not row or row["max_full"] is None:
        return 0
    return int(row["max_full"])


def _full_rebuild_composition_state(
    conn: sqlite3.Connection,
    session_id: str,
) -> Optional[_CompositionState]:
    """Fetch the turns from the newest full snapshot onward and fold them into a fresh state."""
    start_after = max(_cold_start_turn(conn, session_id) - 1, 0)
    turns = conn.execute(
        """SELECT turn_number, storage_mode, request_messages
           FROM conversation_turns
           WHERE session_id = ? AND turn_number > ?
           ORDER BY turn_number ASC""",
        (session_id, start_after),
    ).fetchall()
    if not turns:
        return None
    state = _CompositionState()
    _fold_delta_into_composition_state(state, [dict(t) for t in turns])
    return state


# ── Public API ──


def analyze_session_composition(
    conn: sqlite3.Connection,
    session_id: str,
) -> Optional[dict]:
    """Analyze context composition for a session. Returns cached result if available.

    Uses an incremental cache: when new turns arrive, only those past
    max_turn_processed are fetched and folded into existing state. Compaction
    events (storage_mode="full") reset totals/read_counts then re-absorb the
    snapshot. On any fold exception, falls back to a full
    rebuild so a corrupted increment can't poison the cache permanently.

    Returns dict with: categories, total_bytes, est_tokens, snr, duplicate_read_count.
    Returns None if no history data exists.
    """
    row = conn.execute(
        "SELECT MAX(turn_number) AS max_turn FROM conversation_turns WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if not row or row["max_turn"] is None:
        return None
    max_turn = row["max_turn"]

    cached = _cache.get(session_id)

    if cached is not None and max_turn < cached.max_turn_processed:
        logger.warning(
            "max_turn regressed for %s (cached=%d, now=%d) — rebuilding composition cache",
            session_id, cached.max_turn_processed, max_turn,
        )
        cached = None

    if cached is not None and max_turn == cached.max_turn_processed:
        return _build_composition_result_from_state(cached)

    if cached is not None:
        start_after = cached.max_turn_processed
    else:
        # Cold cache: start at the newest full snapshot, not at turn 1 (see _cold_start_turn).
        start_after = max(_cold_start_turn(conn, session_id) - 1, 0)
    new_turns = conn.execute(
        """SELECT turn_number, storage_mode, request_messages
           FROM conversation_turns
           WHERE session_id = ? AND turn_number > ?
           ORDER BY turn_number ASC""",
        (session_id, start_after),
    ).fetchall()

    if not new_turns and cached is None:
        return None

    state = cached if cached is not None else _CompositionState()
    try:
        _fold_delta_into_composition_state(state, [dict(t) for t in new_turns])
    except Exception:
        logger.exception("Incremental composition fold failed for %s — rebuilding", session_id)
        rebuilt = _full_rebuild_composition_state(conn, session_id)
        if rebuilt is None:
            _cache.pop(session_id, None)
            return None
        state = rebuilt

    _cache.put(session_id, state)
    return _build_composition_result_from_state(state)


# ── Per-turn analysis ──

# session_id -> _PerTurnState
_per_turn_cache = _BoundedCache()


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
            messages = req_json if isinstance(req_json, list) else json.loads(req_json)
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


def _classify_accumulated_categories(accumulated: List[dict]) -> Tuple[Dict[str, int], int]:
    """Classify a message list into category byte totals.

    Used by the per-turn fold path, which only needs categories, not the
    extended tool-call/tool-bytes/thinking breakdown, and adds each turn's
    result to its running totals.
    """
    totals: Dict[str, int] = {cat: 0 for cat in CATEGORIES}
    for msg in accumulated:
        sizes = _classify_message(msg)
        for cat in CATEGORIES:
            totals[cat] += sizes.get(cat, 0)
    return totals, sum(totals.values())


def _fold_delta_into_per_turn_state(
    state: _PerTurnState,
    new_turns: List[dict],
) -> None:
    """Fold delta turns into the per-turn state in place.

    For each turn, adds the turn's messages to the running category totals
    and appends the composition of the whole context at that point. A
    storage_mode="full" row resets the totals to its snapshot first; it counts
    as a compaction when the snapshot has fewer messages than the context had.
    Same results as re-classifying the accumulated context every turn
    (_reconstruct_per_turn), since each category is a per-message sum.
    """
    for row in new_turns:
        turn_number = row["turn_number"]
        storage_mode = row["storage_mode"]
        messages = _parse_turn_messages(row)
        if messages is None:
            state.max_turn_processed = turn_number
            continue

        compacted = False
        if storage_mode == "full":
            if state.msg_count and len(messages) < state.msg_count:
                compacted = True
            state.msg_count = 0
            state.totals = {cat: 0 for cat in CATEGORIES}
        state.msg_count += len(messages)

        d_totals, _ = _classify_accumulated_categories(messages)
        for cat in CATEGORIES:
            state.totals[cat] += d_totals[cat]
        total_bytes = sum(state.totals.values())
        categories = {}
        for cat in CATEGORIES:
            b = state.totals[cat]
            pct = round(b / total_bytes * 100, 1) if total_bytes > 0 else 0.0
            categories[cat] = {"bytes": b, "pct": pct}

        state.turn_results.append({
            "turn": turn_number,
            "msgs": state.msg_count,
            "compacted": compacted,
            "composition": {
                "total_bytes": total_bytes,
                "est_tokens": total_bytes // 4,
                "categories": categories,
            },
        })
        state.max_turn_processed = turn_number


def _full_rebuild_per_turn_state(
    conn: sqlite3.Connection,
    session_id: str,
) -> Optional[_PerTurnState]:
    """Fetch all turns and fold them into a fresh per-turn state."""
    turns = conn.execute(
        """SELECT turn_number, storage_mode, request_messages
           FROM conversation_turns
           WHERE session_id = ?
           ORDER BY turn_number ASC""",
        (session_id,),
    ).fetchall()
    if not turns:
        return None
    state = _PerTurnState()
    _fold_delta_into_per_turn_state(state, [dict(t) for t in turns])
    return state


def analyze_session_composition_per_turn(
    conn: sqlite3.Connection,
    session_id: str,
) -> Optional[dict]:
    """Analyze per-turn composition for a session. Returns sampled data for large sessions.

    Uses an incremental cache: only turns past max_turn_processed are fetched
    and folded. Sampling is applied on each call against the full result list.

    Returns dict with: session_id, total_turns, sampled, turns[].
    Returns None if no history data exists.
    """
    row = conn.execute(
        "SELECT MAX(turn_number) AS max_turn FROM conversation_turns WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if not row or row["max_turn"] is None:
        return None
    max_turn = row["max_turn"]

    cached = _per_turn_cache.get(session_id)

    if cached is not None and max_turn < cached.max_turn_processed:
        logger.warning(
            "max_turn regressed for %s (per-turn cached=%d, now=%d) — rebuilding",
            session_id, cached.max_turn_processed, max_turn,
        )
        cached = None

    if cached is None or max_turn > cached.max_turn_processed:
        start_after = cached.max_turn_processed if cached is not None else 0
        new_turns = conn.execute(
            """SELECT turn_number, storage_mode, request_messages
               FROM conversation_turns
               WHERE session_id = ? AND turn_number > ?
               ORDER BY turn_number ASC""",
            (session_id, start_after),
        ).fetchall()

        if not new_turns and cached is None:
            return None

        state = cached if cached is not None else _PerTurnState()
        try:
            _fold_delta_into_per_turn_state(state, [dict(t) for t in new_turns])
        except Exception:
            logger.exception("Per-turn incremental fold failed for %s — rebuilding", session_id)
            rebuilt = _full_rebuild_per_turn_state(conn, session_id)
            if rebuilt is None:
                _per_turn_cache.pop(session_id, None)
                return None
            state = rebuilt

        _per_turn_cache.put(session_id, state)
    else:
        state = cached

    sampled = len(state.turn_results) > 50
    sampled_turns = _sample_turns(state.turn_results) if sampled else state.turn_results

    return {
        "session_id": session_id,
        "total_turns": len(state.turn_results),
        "sampled": sampled,
        "turns": sampled_turns,
    }


def clear_cache(session_id: Optional[str] = None) -> None:
    """Clear the composition caches for a session, or all of them.

    The file cache (Codex / Gemini) is keyed by session file path and
    revalidated by mtime on every read, so only a full clear empties it.
    """
    if session_id:
        _cache.pop(session_id, None)
        _per_turn_cache.pop(session_id, None)
    else:
        _cache.clear()
        _per_turn_cache.clear()
        _file_cache.clear()


def cache_stats() -> Dict[str, Any]:
    """Sizes and eviction count of the composition caches (reported by /_health)."""
    for cache in (_cache, _per_turn_cache, _file_cache):
        cache.sweep()
    return {
        "sessions": len(_cache),
        "per_turn_sessions": len(_per_turn_cache),
        "per_turn_turns": sum(len(state.turn_results) for state in _per_turn_cache.values()),
        "files": len(_file_cache),
        "evicted": _cache.evicted + _per_turn_cache.evicted + _file_cache.evicted,
        "max": _CACHE_MAX,
        "ttl_s": _CACHE_TTL_S,
    }


# ── File-based composition (Codex / Gemini) ──

# path_str -> (mtime, result_dict)
_file_cache = _BoundedCache()

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
    _file_cache.put(path, (mtime, result))
    return result
