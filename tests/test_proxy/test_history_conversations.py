"""Several conversations share one session_id (CC subagents, WebSearch/WebFetch helper calls).

Each conversation keeps its own delta baseline, so interleaving no longer produces false
compaction events or repeated full snapshots (2026-09-29: 263 false events and 295 full
rows of up to 24 MB inside one session).
"""

import json

import pytest
from test_proxy.test_history import _make_db

from llm_relay.proxy.history import (
    _conversation_key,
    _fingerprint_message,
    _session_prev,
    capture_turn_streamed,
    reset_state,
)

RESP = [{"type": "text", "text": "ok"}]


@pytest.fixture(autouse=True)
def _reset():
    reset_state()
    yield
    reset_state()


def _parent(n):
    return {"messages": [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "main-{}".format(i)} for i in range(n)]}


def _helper(text):
    return {"messages": [{"role": "user", "content": [{"type": "text", "text": text}]}]}


def _rows(conn, sid):
    return [dict(r) for r in conn.execute(
        "SELECT turn_number, storage_mode, total_message_count, previous_message_count, request_messages"
        " FROM conversation_turns WHERE session_id = ? ORDER BY id", (sid,))]


def _events(conn, sid):
    return conn.execute("SELECT count(*) AS n FROM compaction_events WHERE session_id = ?", (sid,)).fetchone()["n"]


def test_helper_calls_do_not_break_the_parent_delta():
    conn = _make_db()
    capture_turn_streamed(conn, "s", _parent(5), RESP, input_tokens=5000)
    capture_turn_streamed(conn, "s", _helper("Perform a web search for the query: qwen4"), RESP, input_tokens=300)
    capture_turn_streamed(conn, "s", _parent(7), RESP, input_tokens=5200)
    capture_turn_streamed(conn, "s", _helper("Perform a web search for the query: gemma4"), RESP, input_tokens=310)
    capture_turn_streamed(conn, "s", _parent(9), RESP, input_tokens=5400)

    rows = _rows(conn, "s")
    assert [(r["turn_number"], r["storage_mode"], r["previous_message_count"]) for r in rows] == [
        (1, "full", 0), (2, "full", 0), (3, "delta", 5), (4, "full", 0), (5, "delta", 7)]
    assert json.loads(rows[2]["request_messages"]) == _parent(7)["messages"][5:]
    assert json.loads(rows[4]["request_messages"]) == _parent(9)["messages"][7:]
    assert _events(conn, "s") == 0
    assert _session_prev["s"] == (5, 9, 5400)  # session-level state still tracks the last capture


def test_subagent_conversation_keeps_its_own_delta():
    conn = _make_db()
    sub1 = {"messages": [{"role": "user", "content": "task: explore the repo"}]}
    sub2 = {"messages": sub1["messages"] + [{"role": "assistant", "content": "a"}, {"role": "user", "content": "b"}]}
    capture_turn_streamed(conn, "s", _parent(5), RESP, input_tokens=5000)
    capture_turn_streamed(conn, "s", sub1, RESP, input_tokens=100)
    capture_turn_streamed(conn, "s", _parent(7), RESP, input_tokens=5200)
    capture_turn_streamed(conn, "s", sub2, RESP, input_tokens=150)
    capture_turn_streamed(conn, "s", _parent(9), RESP, input_tokens=5400)

    rows = _rows(conn, "s")
    assert [(r["storage_mode"], r["previous_message_count"]) for r in rows] == [
        ("full", 0), ("full", 0), ("delta", 5), ("delta", 1), ("delta", 7)]
    assert json.loads(rows[3]["request_messages"]) == sub2["messages"][1:]
    assert _events(conn, "s") == 0


def test_count_drop_inside_one_conversation_is_still_a_compaction():
    conn = _make_db()
    capture_turn_streamed(conn, "s", _parent(10), RESP, input_tokens=9000)
    capture_turn_streamed(conn, "s", _parent(3), RESP, input_tokens=1000)  # same first message, fewer messages

    rows = _rows(conn, "s")
    assert [(r["storage_mode"], r["previous_message_count"]) for r in rows] == [("full", 0), ("full", 10)]
    assert _events(conn, "s") == 1


def test_restart_fallback_keeps_the_delta_for_the_first_capture():
    conn = _make_db()
    capture_turn_streamed(conn, "s", _parent(3), RESP, input_tokens=100)
    reset_state()  # relay restart: the session state is re-seeded from the DB
    capture_turn_streamed(conn, "s", _parent(5), RESP, input_tokens=120)
    capture_turn_streamed(conn, "s", _helper("Perform a web search for the query: x"), RESP, input_tokens=10)
    capture_turn_streamed(conn, "s", _parent(7), RESP, input_tokens=140)

    rows = _rows(conn, "s")
    assert [(r["turn_number"], r["storage_mode"], r["previous_message_count"]) for r in rows] == [
        (1, "full", 0), (2, "delta", 3), (3, "full", 0), (4, "delta", 5)]
    assert _events(conn, "s") == 0


def test_new_conversation_after_restart_starts_full_without_a_second_false_event():
    conn = _make_db()
    capture_turn_streamed(conn, "s", _parent(5), RESP, input_tokens=5000)
    reset_state()
    # The seeded session baseline (5 messages) is applied to whichever conversation
    # shows up first; a later, different conversation starts its own chain.
    capture_turn_streamed(conn, "s", _helper("Perform a web search for the query: y"), RESP, input_tokens=10)
    capture_turn_streamed(conn, "s", _parent(7), RESP, input_tokens=5200)

    rows = _rows(conn, "s")
    assert (rows[2]["turn_number"], rows[2]["storage_mode"], rows[2]["previous_message_count"]) == (3, "full", 0)
    assert _events(conn, "s") <= 1


def test_fingerprint_ignores_cache_control_and_inline_payloads():
    def img(data):
        return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}

    m1 = {"role": "user", "content": [
        {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}, img("A" * 100)]}
    m2 = {"role": "user", "content": [{"type": "text", "text": "hi"}, img("B" * 100)]}
    assert _fingerprint_message(m1) == _fingerprint_message(m2)
    fp_x = _fingerprint_message({"role": "user", "content": "x"})
    assert fp_x != _fingerprint_message({"role": "user", "content": "y"})
    assert _conversation_key("s", [m1]) == ("s", _fingerprint_message(m1))
    assert _conversation_key("s", []) == ("s", "")
    # Fingerprinting never mutates the request.
    assert m1["content"][1]["source"]["data"] == "A" * 100
    assert "cache_control" in m1["content"][0]
