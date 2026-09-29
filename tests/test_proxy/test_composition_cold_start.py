"""Cold-cache composition analysis loads only the newest full snapshot onward.

2026-09-29: a fresh relay process folded every stored turn of a session (3.67 GB for an
image-heavy session) on the first dashboard poll and was OOM-killed on every restart.
Because a "full" row resets the whole state before re-absorbing its snapshot, folding
from the newest full row must give the same result as folding from turn 1.
"""

import json
import sqlite3
from unittest import mock

import pytest

from llm_relay.proxy import composition
from llm_relay.proxy.composition import (
    _cache,
    _cold_start_turn,
    _CompositionState,
    _fold_delta_into_composition_state,
    _full_rebuild_composition_state,
    analyze_session_composition,
)

SID = "sess-cold"


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE conversation_turns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        session_id TEXT NOT NULL,
        turn_number INTEGER NOT NULL,
        storage_mode TEXT NOT NULL DEFAULT 'delta',
        request_messages TEXT
    )""")
    return conn


def _insert(conn, turn, mode, messages, session_id=SID):
    conn.execute(
        "INSERT INTO conversation_turns (ts, session_id, turn_number, storage_mode, request_messages)"
        " VALUES (?, ?, ?, ?, ?)",
        (float(turn), session_id, turn, mode, None if messages is None else json.dumps(messages)),
    )
    conn.commit()


def _read(path):
    return {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t-" + path, "name": "Read", "input": {"file_path": path}}]}


def _result(text):
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": text}]}


def _text(role, text):
    return {"role": role, "content": text}


def _seed_session(conn):
    """1 full, 2 delta, 3 full (compaction), 4 delta, 5 delta -- distinct content per turn."""
    _insert(conn, 1, "full", [_text("user", "u1 " * 10), _text("assistant", "a1 " * 20)])
    _insert(conn, 2, "delta", [_read("/a.py"), _result("A" * 300)])
    _insert(conn, 3, "full", [_text("user", "summary " * 30), _read("/a.py"), _result("A" * 300),
                              _read("/a.py"), _result("A" * 300), _text("assistant", "a3")])
    _insert(conn, 4, "delta", [_read("/b.py"), _result("B" * 500), {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "hmm " * 50}, {"type": "text", "text": "done"}]}])
    _insert(conn, 5, "delta", [_text("user", "u5"), _text("assistant", "a5 " * 40)])


def _all_rows(conn, session_id=SID):
    rows = conn.execute(
        "SELECT turn_number, storage_mode, request_messages FROM conversation_turns"
        " WHERE session_id = ? ORDER BY turn_number ASC", (session_id,)).fetchall()
    return [dict(r) for r in rows]


@pytest.fixture(autouse=True)
def _clear_cache():
    _cache.clear()
    yield
    _cache.clear()


def test_cold_start_turn_is_newest_usable_full():
    conn = _make_db()
    _seed_session(conn)
    assert _cold_start_turn(conn, SID) == 3

    _insert(conn, 6, "full", None)  # unusable snapshot (NULL payload) is ignored
    assert _cold_start_turn(conn, SID) == 3

    assert _cold_start_turn(conn, "missing") == 0
    _insert(conn, 1, "delta", [_text("user", "x")], session_id="delta-only")
    assert _cold_start_turn(conn, "delta-only") == 0


def test_cold_analysis_matches_folding_from_turn_one():
    conn = _make_db()
    _seed_session(conn)

    with mock.patch.object(composition, "_cold_start_turn", return_value=0):
        expected = analyze_session_composition(conn, SID)  # legacy behaviour: all rows
    assert expected is not None
    assert _cache[SID].max_turn_processed == 5
    _cache.clear()

    actual = analyze_session_composition(conn, SID)
    assert actual == expected
    assert _cache[SID].max_turn_processed == 5


def test_cold_analysis_does_not_fetch_rows_before_newest_full():
    conn = _make_db()
    _seed_session(conn)
    seen = []
    original = composition._fold_delta_into_composition_state

    def spy(state, new_turns):
        seen.extend(t["turn_number"] for t in new_turns)
        return original(state, new_turns)

    with mock.patch.object(composition, "_fold_delta_into_composition_state", side_effect=spy):
        assert analyze_session_composition(conn, SID) is not None
    assert seen == [3, 4, 5]

    # Warm cache: only turns past max_turn_processed are fetched.
    _insert(conn, 6, "delta", [_text("user", "u6")])
    seen.clear()
    with mock.patch.object(composition, "_fold_delta_into_composition_state", side_effect=spy):
        analyze_session_composition(conn, SID)
    assert seen == [6]


def test_full_rebuild_matches_state_folded_from_turn_one():
    conn = _make_db()
    _seed_session(conn)

    reference = _CompositionState()
    _fold_delta_into_composition_state(reference, _all_rows(conn))

    rebuilt = _full_rebuild_composition_state(conn, SID)
    assert rebuilt is not None
    assert rebuilt == reference


def test_session_without_full_rows_still_analyzed():
    conn = _make_db()
    _insert(conn, 1, "delta", [_text("user", "u1"), _text("assistant", "a1")], session_id="legacy")
    _insert(conn, 2, "delta", [_read("/c.py"), _result("C" * 100)], session_id="legacy")

    reference = _CompositionState()
    _fold_delta_into_composition_state(reference, _all_rows(conn, "legacy"))
    result = analyze_session_composition(conn, "legacy")
    assert result is not None
    assert _cache["legacy"] == reference
