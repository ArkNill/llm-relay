"""Restart re-seeding of history's per-session state (proxy/history.py::_prev_state).

Before 2026-09-28 the per-session state lived only in process memory, so every
relay restart renumbered a continuing session from turn 1 and stored another
full snapshot (duplicate turn numbers inside one session).
"""

from __future__ import annotations

import pytest
from test_proxy.test_history import _make_db

from llm_relay.proxy.db import get_last_turn_state
from llm_relay.proxy.history import _session_prev, capture_turn_streamed, reset_state


@pytest.fixture(autouse=True)
def _clear_session_prev():
    reset_state()
    yield
    reset_state()


def _req(n_messages: int) -> dict:
    return {
        "model": "m",
        "messages": [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(n_messages)
        ],
    }


def test_turn_numbers_continue_after_restart():
    conn = _make_db()
    capture_turn_streamed(conn, "s1", _req(1), [{"type": "text", "text": "a"}], input_tokens=100)
    capture_turn_streamed(conn, "s1", _req(3), [{"type": "text", "text": "b"}], input_tokens=120)

    reset_state()  # simulate a relay restart

    capture_turn_streamed(conn, "s1", _req(5), [{"type": "text", "text": "c"}], input_tokens=140)

    rows = conn.execute(
        "SELECT turn_number, storage_mode, total_message_count, previous_message_count "
        "FROM conversation_turns WHERE session_id='s1' ORDER BY id"
    ).fetchall()
    assert [(r["turn_number"], r["storage_mode"]) for r in rows] == [(1, "full"), (2, "delta"), (3, "delta")]
    assert rows[2]["previous_message_count"] == 3  # delta computed against the last stored snapshot
    assert get_last_turn_state(conn, "s1") == (3, 5)
    assert _session_prev["s1"] == (3, 5, 140)


def test_unknown_session_still_starts_at_one():
    conn = _make_db()
    assert get_last_turn_state(conn, "nope") is None
    capture_turn_streamed(conn, "s2", _req(1), [{"type": "text", "text": "a"}])
    row = conn.execute("SELECT turn_number, storage_mode FROM conversation_turns WHERE session_id='s2'").fetchone()
    assert (row["turn_number"], row["storage_mode"]) == (1, "full")


def test_seed_prefers_highest_turn_number():
    conn = _make_db()
    # Two historical runs for one session (the pre-fix duplicate pattern): 1..3 then 1..2.
    for tn, count in ((1, 1), (2, 3), (3, 5), (1, 7), (2, 9)):
        conn.execute(
            "INSERT INTO conversation_turns (ts, session_id, turn_number, storage_mode, total_message_count) "
            "VALUES (0, 's3', ?, 'delta', ?)",
            (tn, count),
        )
    assert get_last_turn_state(conn, "s3") == (3, 5)
    capture_turn_streamed(conn, "s3", _req(11), [{"type": "text", "text": "z"}])
    row = conn.execute(
        "SELECT turn_number, previous_message_count FROM conversation_turns "
        "WHERE session_id='s3' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert (row["turn_number"], row["previous_message_count"]) == (4, 5)
