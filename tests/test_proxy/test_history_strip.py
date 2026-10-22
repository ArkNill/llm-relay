"""Tests for history._strip_binary_payloads -- inline images/documents are stored as size markers.

2026-09-29: image-reading subagents produced 24 MB conversation_turns rows (3.7 GB in a day);
the stored copy now keeps only the block shape plus the dropped payload's size.
"""

import json
import sqlite3

import pytest

from llm_relay.proxy.db import get_session_history
from llm_relay.proxy.history import _session_prev, _strip_binary_payloads, capture_turn_streamed

_PNG_B64 = "iVBORw0KGgo" * 20000  # ~220 KB of base64 text


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE conversation_turns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        session_id TEXT NOT NULL,
        request_id INTEGER,
        turn_number INTEGER NOT NULL,
        storage_mode TEXT NOT NULL DEFAULT 'delta',
        request_messages TEXT,
        response_message TEXT,
        thinking_blocks TEXT,
        model TEXT,
        temperature REAL,
        max_tokens INTEGER,
        total_message_count INTEGER,
        previous_message_count INTEGER,
        request_size_bytes INTEGER DEFAULT 0,
        response_size_bytes INTEGER DEFAULT 0,
        provider TEXT DEFAULT 'anthropic'
    )""")
    conn.execute("""CREATE TABLE compaction_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        session_id TEXT NOT NULL,
        turn_number INTEGER NOT NULL,
        previous_count INTEGER,
        current_count INTEGER,
        dropped_count INTEGER,
        previous_tokens INTEGER,
        current_tokens INTEGER,
        token_drop_pct REAL,
        dropped_roles TEXT
    )""")
    return conn


def _image_block(data=_PNG_B64):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def _messages_with_image():
    return [
        {"role": "user", "content": "Read the screenshot"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read",
                                           "input": {"file_path": "/tmp/shot.png"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [_image_block(), {"type": "text", "text": "ok"}]},
            {"type": "text", "text": "what does it show?"},
        ]},
    ]


@pytest.fixture(autouse=True)
def _clear_session_state():
    _session_prev.clear()
    yield
    _session_prev.clear()


def test_strip_nested_tool_result_image():
    messages = _messages_with_image()
    stripped = _strip_binary_payloads(messages)

    img = stripped[2]["content"][0]["content"][0]
    assert img["type"] == "image"
    assert img["source"]["type"] == "base64"
    assert img["source"]["media_type"] == "image/png"
    assert img["source"]["data"] == ""
    assert img["source"]["omitted_bytes"] == len(_PNG_B64)
    # Siblings and other messages are passed through untouched (by reference).
    assert stripped[2]["content"][0]["content"][1] == {"type": "text", "text": "ok"}
    assert stripped[2]["content"][1] == {"type": "text", "text": "what does it show?"}
    assert stripped[0] is messages[0]
    assert stripped[1] is messages[1]
    # Copy-on-write: the original request is not mutated.
    assert messages[2]["content"][0]["content"][0]["source"]["data"] == _PNG_B64


def test_strip_top_level_document_block():
    messages = [{"role": "user", "content": [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0" * 100}},
        {"type": "text", "text": "summarize"},
    ]}]
    stripped = _strip_binary_payloads(messages)
    doc = stripped[0]["content"][0]
    assert doc["type"] == "document"
    assert doc["source"]["data"] == ""
    assert doc["source"]["omitted_bytes"] == len("JVBERi0" * 100)
    assert stripped[0]["content"][1] is messages[0]["content"][1]


def test_strip_without_inline_payload_returns_same_list():
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        {"role": "user", "content": [{"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}]},
        {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": ""}}]},
        "not-a-dict",
    ]
    assert _strip_binary_payloads(messages) is messages


def test_capture_stores_stripped_payload():
    conn = _make_db()
    messages = _messages_with_image()
    req_json = {"model": "claude-haiku-4-5-20251001", "messages": messages}

    capture_turn_streamed(
        conn, "sess-img", req_json,
        accumulated_content=[{"type": "text", "text": "A screenshot of a terminal."}],
        model="claude-haiku-4-5-20251001", request_size=len(json.dumps(req_json)),
    )

    turns = get_session_history(conn, "sess-img")
    assert len(turns) == 1
    stored = json.loads(turns[0]["request_messages"])
    img = stored[2]["content"][0]["content"][0]
    assert img["source"]["data"] == ""
    assert img["source"]["omitted_bytes"] == len(_PNG_B64)
    assert len(turns[0]["request_messages"]) < 2000
    # The real body size is still recorded from the proxy's measurement.
    assert turns[0]["request_size_bytes"] > len(_PNG_B64)
    # The in-flight request object keeps its payload.
    assert messages[2]["content"][0]["content"][0]["source"]["data"] == _PNG_B64
