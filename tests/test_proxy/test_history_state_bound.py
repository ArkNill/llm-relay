"""Bounded in-memory capture state (2026-10-23, R9-10a).

history's per-session and per-conversation baselines lived for the whole process,
one key per helper call (WebSearch/WebFetch, title generation). They are now LRU
dicts capped by LLM_RELAY_HISTORY_STATE_MAX. Eviction must never lose a turn: an
evicted conversation restarts with a full snapshot, and an evicted session is
re-seeded from the DB exactly as after a restart.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient
from test_proxy.test_history import _make_db

from llm_relay.proxy import history
from llm_relay.proxy.history import _conversation_key, capture_turn_streamed, reset_state

RESP = [{"type": "text", "text": "ok"}]


@pytest.fixture(autouse=True)
def _reset():
    reset_state()
    yield
    reset_state()


def _conv(first, n):
    return {"messages": [{"role": "user" if i % 2 == 0 else "assistant", "content": "{}-{}".format(first, i)}
                         for i in range(n)]}


def _rows(conn, sid):
    return [tuple(r) for r in conn.execute(
        "SELECT turn_number, storage_mode, previous_message_count FROM conversation_turns"
        " WHERE session_id = ? ORDER BY id", (sid,))]


def _events(conn):
    return conn.execute("SELECT count(*) FROM compaction_events").fetchone()[0]


def test_least_recently_used_conversation_is_evicted_and_restarts_full(monkeypatch):
    monkeypatch.setattr(history, "_STATE_MAX", 3)
    conn = _make_db()
    capture_turn_streamed(conn, "s", _conv("main", 3), RESP, input_tokens=300)
    capture_turn_streamed(conn, "s", _conv("helper1", 1), RESP, input_tokens=10)
    capture_turn_streamed(conn, "s", _conv("sub", 1), RESP, input_tokens=10)
    capture_turn_streamed(conn, "s", _conv("main", 5), RESP, input_tokens=500)  # touched: no longer the LRU key
    capture_turn_streamed(conn, "s", _conv("helper2", 1), RESP, input_tokens=10)  # 4th key -> evicts "helper1"

    assert len(history._conv_prev) == 3
    assert _conversation_key("s", _conv("helper1", 1)["messages"]) not in history._conv_prev
    assert _conversation_key("s", _conv("main", 1)["messages"]) in history._conv_prev
    assert history.state_stats()["evicted"] == 1

    capture_turn_streamed(conn, "s", _conv("helper1", 3), RESP, input_tokens=30)
    rows = _rows(conn, "s")
    assert rows[3] == (4, "delta", 3)
    assert rows[-1] == (6, "full", 0)  # evicted: a full snapshot again, turn numbers continue
    assert _events(conn) == 0  # and no false compaction event


def test_evicted_session_is_reseeded_from_the_db_like_after_a_restart(monkeypatch):
    monkeypatch.setattr(history, "_STATE_MAX", 3)
    conn = _make_db()
    for sid in ("s0", "s1", "s2", "s3"):
        capture_turn_streamed(conn, sid, _conv(sid, 1), RESP, input_tokens=10)

    assert len(history._session_prev) == 3 and "s0" not in history._session_prev
    assert len(history._conv_prev) == 3
    stats = history.state_stats()
    assert (stats["sessions"], stats["conversations"], stats["evicted"]) == (3, 3, 2)

    capture_turn_streamed(conn, "s0", _conv("s0", 3), RESP, input_tokens=30)
    assert _rows(conn, "s0") == [(1, "full", 0), (2, "delta", 1)]  # numbering and delta continue from the DB


def test_zero_disables_the_bound(monkeypatch):
    monkeypatch.setattr(history, "_STATE_MAX", 0)
    conn = _make_db()
    for i in range(20):
        capture_turn_streamed(conn, "s", _conv("helper{}".format(i), 1), RESP, input_tokens=10)
    assert len(history._conv_prev) == 20 and history.state_stats()["evicted"] == 0


def test_reset_state_clears_state_and_the_eviction_count(monkeypatch):
    monkeypatch.setattr(history, "_STATE_MAX", 1)
    conn = _make_db()
    capture_turn_streamed(conn, "a", _conv("a", 1), RESP)
    capture_turn_streamed(conn, "b", _conv("b", 1), RESP)
    assert history.state_stats()["evicted"] == 2
    reset_state()
    assert history.state_stats() == {"sessions": 0, "conversations": 0, "evicted": 0, "max": 1}


def test_health_reports_history_state():
    from llm_relay.proxy import proxy

    payload = TestClient(proxy.app).get("/_health").json()
    assert set(payload["history_state"]) == {"sessions", "conversations", "evicted", "max"}
