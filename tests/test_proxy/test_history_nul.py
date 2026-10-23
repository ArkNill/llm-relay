"""NUL escapes in captured history and capture failures (2026-10-23, R9-12).

PostgreSQL jsonb rejects the JSON escape ``\\u0000``, so a conversation turn whose
messages carried a NUL character failed to insert. ``capture_turn_streamed``
swallowed the error at debug level, the background writer counted the job as
written (no retry, no spill), and the conversation's state was never advanced:
every later turn re-sent the same delta and failed the same way (about 660 turns
lost over 10 days). SQLite stores the text as-is, so these tests check the stored
text for the escape instead of relying on the database to reject it.
"""

from __future__ import annotations

import json
import logging

import pytest
from test_proxy.test_history import _make_db

from llm_relay.proxy import history
from llm_relay.proxy.dbwriter import DBWriter
from llm_relay.proxy.history import (
    _sanitize_json_text,
    capture_delegation_turn,
    capture_turn,
    capture_turn_streamed,
    reset_state,
)

NUL_ESCAPE = "\\u0000"
REPLACEMENT_ESCAPE = "\\ufffd"
HISTORY_LOGGER = "llm-relay.history"


@pytest.fixture(autouse=True)
def _reset():
    reset_state()
    yield
    reset_state()


def _boom(*args, **kwargs):
    raise RuntimeError("jsonb rejected the row")


def _nul_request():
    return {"model": "m", "messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "bin\x00ary"}]}]}


# ── _sanitize_json_text ──


def test_nul_escape_in_serialized_json_is_replaced():
    text = json.dumps({"content": "a\x00b"}, ensure_ascii=False)
    assert NUL_ESCAPE in text  # json.dumps escapes control characters even with ensure_ascii=False

    cleaned, count = _sanitize_json_text(text)

    assert count == 1
    assert NUL_ESCAPE not in cleaned and cleaned.count(REPLACEMENT_ESCAPE) == 1
    assert json.loads(cleaned) == {"content": "a\ufffdb"}


def test_literal_backslash_u0000_text_is_not_touched():
    # A literal backslash followed by "u0000" (serialized as \\u0000) is text, not a NUL;
    # a backslash followed by a real NUL (\\\u0000) is an escaped backslash plus the escape.
    original = {"content": "path\\u0000 and \\\x00"}
    text = json.dumps(original, ensure_ascii=False)

    cleaned, count = _sanitize_json_text(text)

    assert count == 1
    assert json.loads(cleaned) == {"content": "path\\u0000 and \\\ufffd"}


def test_text_without_nul_is_returned_unchanged():
    text = json.dumps([{"type": "text", "text": "plain"}])
    assert _sanitize_json_text(text) == (text, 0)


# ── capture paths ──


def test_capture_stores_no_nul_escape_and_logs_the_count(caplog):
    conn = _make_db()
    content = [{"type": "thinking", "thinking": "th\x00ink"}, {"type": "text", "text": "out\x00put"}]

    with caplog.at_level(logging.INFO, logger=HISTORY_LOGGER):
        capture_turn_streamed(conn, "sess-nul-1", _nul_request(), content, input_tokens=10)

    row = conn.execute("SELECT request_messages, response_message, thinking_blocks FROM conversation_turns").fetchone()
    for column in ("request_messages", "response_message", "thinking_blocks"):
        assert NUL_ESCAPE not in row[column], column
        assert REPLACEMENT_ESCAPE in row[column], column
    lines = [r.getMessage() for r in caplog.records if "NUL escape" in r.getMessage()]
    # 1 in the request, 2 in the response blocks, 1 in the extracted thinking block
    assert lines == ["history: replaced 4 NUL escape(s) session=sess-nul turn=1"]
    assert history._session_prev["sess-nul-1"][0] == 1  # the capture advanced the state


@pytest.mark.parametrize("capture", [capture_turn, capture_turn_streamed])
def test_insert_failure_propagates_and_leaves_the_state_alone(capture, monkeypatch):
    conn = _make_db()
    monkeypatch.setattr(history, "log_conversation_turn", _boom)
    req = {"messages": [{"role": "user", "content": "hi"}]}
    blocks = [{"type": "text", "text": "a"}]
    response = {"content": blocks} if capture is capture_turn else blocks

    with pytest.raises(RuntimeError):
        capture(conn, "sess-fail", req, response)

    # Not advanced: a retry (or the next turn) re-sends the same delta instead of skipping it.
    assert "sess-fail" not in history._session_prev
    assert not history._conv_prev


def test_failed_capture_is_retried_by_the_writer_then_spilled(tmp_path, monkeypatch):
    monkeypatch.setattr("llm_relay.proxy.dbwriter.time.sleep", lambda s: None)
    monkeypatch.setattr("llm_relay.proxy.dbwriter._RETRIES", 3)
    monkeypatch.setattr(history, "log_conversation_turn", _boom)
    writer = DBWriter(conn_factory=_make_db, spill_dir=tmp_path)
    writer.start()

    writer.submit(
        capture_turn_streamed,
        session_id="sess-spill",
        req_json=_nul_request(),
        accumulated_content=[{"type": "text", "text": "ok"}],
    )
    writer.stop(timeout=5)

    assert (writer.stats["written"], writer.stats["retried"], writer.stats["spilled"]) == (0, 3, 1)
    files = list(tmp_path.glob("spill-*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert (record["fn"], record["reason"]) == ("capture_turn_streamed", "retries exhausted")
    assert record["kwargs"]["session_id"] == "sess-spill"


def test_delegation_capture_sanitizes_and_warns_on_failure(caplog, monkeypatch):
    conn = _make_db()
    capture_delegation_turn(conn, "sess-dlg", "codex", "pro\x00mpt", "out\x00put")
    row = conn.execute("SELECT request_messages, response_message FROM conversation_turns").fetchone()
    assert NUL_ESCAPE not in row["request_messages"] and NUL_ESCAPE not in row["response_message"]

    monkeypatch.setattr(history, "log_conversation_turn", _boom)
    with caplog.at_level(logging.WARNING, logger=HISTORY_LOGGER):
        capture_delegation_turn(conn, "sess-dlg", "codex", "again", "fails")  # never raises
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == HISTORY_LOGGER]
    assert len(warnings) == 1
    assert warnings[0].getMessage().startswith("history capture failed (delegation) session=sess-dlg")
