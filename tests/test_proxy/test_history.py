"""Tests for proxy/history.py -- session history capture, diff computation, compaction detection."""

import json
import sqlite3
import time

import pytest

from llm_relay.proxy.db import (
    get_history_sessions,
    get_session_compactions,
    get_session_history,
    log_compaction_event,
    log_conversation_turn,
)
from llm_relay.proxy.history import (
    _compute_delta,
    _detect_compaction,
    _extract_model_params,
    _extract_response_content,
    _extract_thinking,
    _session_prev,
    capture_delegation_turn,
    capture_turn,
    capture_turn_streamed,
)


def _make_db():
    """Create an in-memory DB with all required tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # Minimal requests table (needed for FK reference)
    conn.execute("""CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY,
        ts REAL NOT NULL,
        session_id TEXT,
        endpoint TEXT,
        model TEXT,
        input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        cache_creation INTEGER DEFAULT 0,
        cache_read INTEGER DEFAULT 0,
        read_ratio REAL DEFAULT 0.0,
        status_code INTEGER,
        latency_ms REAL,
        is_stream INTEGER DEFAULT 0,
        raw_usage TEXT,
        request_body_bytes INTEGER DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS conversation_turns (
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_conv_turns_session ON conversation_turns(session_id, turn_number)"
    )
    conn.execute("""CREATE TABLE IF NOT EXISTS compaction_events (
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_compaction_session ON compaction_events(session_id)")
    return conn


@pytest.fixture(autouse=True)
def _clear_session_prev():
    """Clear the session state cache before each test."""
    _session_prev.clear()
    yield
    _session_prev.clear()


# ── TestComputeDelta ──


class TestComputeDelta:
    def test_first_turn_stores_full(self):
        messages = [{"role": "user", "content": "hello"}]
        result, mode = _compute_delta(messages, previous_count=0)
        assert mode == "full"
        assert result == messages

    def test_normal_delta(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "how are you"},
        ]
        result, mode = _compute_delta(messages, previous_count=1)
        assert mode == "delta"
        assert len(result) == 2
        assert result[0]["role"] == "assistant"
        assert result[1]["role"] == "user"

    def test_compaction_stores_full(self):
        # Fewer messages than before = compaction
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        result, mode = _compute_delta(messages, previous_count=5)
        assert mode == "full"
        assert len(result) == 2

    def test_empty_delta_stores_full(self):
        messages = [{"role": "user", "content": "hello"}]
        result, mode = _compute_delta(messages, previous_count=1)
        assert mode == "full"

    def test_large_delta(self):
        messages = [{"role": "user", "content": "msg-{}".format(i)} for i in range(20)]
        result, mode = _compute_delta(messages, previous_count=10)
        assert mode == "delta"
        assert len(result) == 10


# ── TestExtractThinking ──


class TestExtractThinking:
    def test_no_thinking(self):
        blocks = [{"type": "text", "text": "hello"}]
        result = _extract_thinking(blocks)
        assert result == []

    def test_single_thinking(self):
        blocks = [
            {"type": "thinking", "thinking": "let me think..."},
            {"type": "text", "text": "hello"},
        ]
        result = _extract_thinking(blocks)
        assert len(result) == 1
        assert result[0]["thinking"] == "let me think..."

    def test_multiple_thinking(self):
        blocks = [
            {"type": "thinking", "thinking": "first"},
            {"type": "text", "text": "mid"},
            {"type": "thinking", "thinking": "second"},
        ]
        result = _extract_thinking(blocks)
        assert len(result) == 2

    def test_non_list_input(self):
        assert _extract_thinking("not a list") == []
        assert _extract_thinking(None) == []
        assert _extract_thinking(42) == []

    def test_mixed_block_types(self):
        blocks = [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "answer"},
            {"type": "tool_use", "name": "bash", "input": {}},
        ]
        result = _extract_thinking(blocks)
        assert len(result) == 1
        assert result[0]["type"] == "thinking"


# ── TestDetectCompaction ──


class TestDetectCompaction:
    def test_first_turn_no_compaction(self):
        result = _detect_compaction("s1", 5, 0, 1000, 0)
        assert result is None

    def test_normal_growth_no_compaction(self):
        result = _detect_compaction("s1", 7, 5, 2000, 1000)
        assert result is None

    def test_message_count_drop(self):
        result = _detect_compaction("s1", 3, 10, 500, 1000)
        assert result is not None
        assert result["dropped_count"] == 7
        assert result["previous_count"] == 10
        assert result["current_count"] == 3

    def test_token_drop_over_30pct(self):
        result = _detect_compaction("s1", 10, 10, 600, 1000)
        assert result is not None
        assert result["token_drop_pct"] == 40.0

    def test_token_drop_under_30pct(self):
        result = _detect_compaction("s1", 10, 10, 800, 1000)
        assert result is None

    def test_both_count_and_token_drop(self):
        result = _detect_compaction("s1", 5, 10, 300, 1000)
        assert result is not None
        assert result["dropped_count"] == 5
        assert result["token_drop_pct"] == 70.0

    def test_zero_previous_tokens(self):
        # No token tracking available
        result = _detect_compaction("s1", 7, 5, 2000, 0)
        assert result is None


# ── TestExtractResponseContent ──


class TestExtractResponseContent:
    def test_text_response(self):
        resp = {"content": [{"type": "text", "text": "hello"}]}
        msg, thinking = _extract_response_content(resp)
        assert msg is not None
        assert json.loads(msg)[0]["text"] == "hello"
        assert thinking is None

    def test_thinking_response(self):
        resp = {"content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "answer"},
        ]}
        msg, thinking = _extract_response_content(resp)
        assert msg is not None
        assert thinking is not None
        assert len(json.loads(thinking)) == 1

    def test_no_content(self):
        resp = {"model": "claude-3"}
        msg, thinking = _extract_response_content(resp)
        assert msg is None
        assert thinking is None


# ── TestExtractModelParams ──


class TestExtractModelParams:
    def test_all_params(self):
        req = {"model": "claude-3", "temperature": 0.7, "max_tokens": 4096}
        model, temp, maxt = _extract_model_params(req)
        assert model == "claude-3"
        assert temp == 0.7
        assert maxt == 4096

    def test_missing_params(self):
        model, temp, maxt = _extract_model_params({})
        assert model is None
        assert temp is None
        assert maxt is None


# ── TestCaptureTurn ──


class TestCaptureTurn:
    def test_basic_capture(self):
        conn = _make_db()
        req = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "hello"}],
        }
        resp = {"content": [{"type": "text", "text": "hi there"}]}

        capture_turn(conn, "sid-1", req, resp, input_tokens=100, request_size=50, response_size=30)

        rows = conn.execute("SELECT * FROM conversation_turns").fetchall()
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["session_id"] == "sid-1"
        assert row["turn_number"] == 1
        assert row["storage_mode"] == "full"
        assert row["model"] == "claude-3"
        assert row["provider"] == "anthropic"

    def test_second_turn_delta(self):
        conn = _make_db()
        msgs1 = [{"role": "user", "content": "hello"}]
        msgs2 = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "how are you"},
        ]
        resp = {"content": [{"type": "text", "text": "I'm fine"}]}

        capture_turn(conn, "sid-1", {"messages": msgs1}, resp, input_tokens=100)
        capture_turn(conn, "sid-1", {"messages": msgs2}, resp, input_tokens=200)

        rows = conn.execute("SELECT * FROM conversation_turns ORDER BY turn_number").fetchall()
        assert len(rows) == 2
        assert dict(rows[0])["storage_mode"] == "full"
        assert dict(rows[1])["storage_mode"] == "delta"
        # Delta should only have 2 new messages
        delta = json.loads(dict(rows[1])["request_messages"])
        assert len(delta) == 2

    def test_no_session_id_skips(self):
        conn = _make_db()
        capture_turn(conn, None, {"messages": [{"role": "user", "content": "hi"}]}, {})
        rows = conn.execute("SELECT * FROM conversation_turns").fetchall()
        assert len(rows) == 0

    def test_no_messages_skips(self):
        conn = _make_db()
        capture_turn(conn, "sid-1", {"model": "claude-3"}, {})
        rows = conn.execute("SELECT * FROM conversation_turns").fetchall()
        assert len(rows) == 0

    def test_raw_mode_always_full(self):
        conn = _make_db()
        msgs1 = [{"role": "user", "content": "hello"}]
        msgs2 = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "how are you"},
        ]
        resp = {"content": [{"type": "text", "text": "ok"}]}

        capture_turn(conn, "sid-1", {"messages": msgs1}, resp, input_tokens=100, raw_mode=True)
        capture_turn(conn, "sid-1", {"messages": msgs2}, resp, input_tokens=200, raw_mode=True)

        rows = conn.execute("SELECT * FROM conversation_turns ORDER BY turn_number").fetchall()
        assert all(dict(r)["storage_mode"] == "full" for r in rows)

    def test_compaction_detection(self):
        conn = _make_db()
        # Turn 1: 10 messages
        msgs1 = [{"role": "user", "content": "msg-{}".format(i)} for i in range(10)]
        # Turn 2: only 3 messages (compaction)
        msgs2 = [{"role": "user", "content": "msg-{}".format(i)} for i in range(3)]
        resp = {"content": [{"type": "text", "text": "ok"}]}

        capture_turn(conn, "sid-1", {"messages": msgs1}, resp, input_tokens=5000)
        capture_turn(conn, "sid-1", {"messages": msgs2}, resp, input_tokens=1000)

        # Check conversation_turns
        rows = conn.execute("SELECT * FROM conversation_turns ORDER BY turn_number").fetchall()
        assert len(rows) == 2
        assert dict(rows[1])["storage_mode"] == "full"  # Compaction forces full

        # Check compaction_events
        events = conn.execute("SELECT * FROM compaction_events").fetchall()
        assert len(events) == 1
        evt = dict(events[0])
        assert evt["previous_count"] == 10
        assert evt["current_count"] == 3
        assert evt["dropped_count"] == 7


# ── TestCaptureTurnStreamed ──


class TestCaptureTurnStreamed:
    def test_basic_streamed_capture(self):
        conn = _make_db()
        req = {"messages": [{"role": "user", "content": "hello"}]}
        content = [{"type": "text", "text": "streamed response"}]

        capture_turn_streamed(
            conn, "sid-1", req, content,
            model="claude-3", input_tokens=100,
        )

        rows = conn.execute("SELECT * FROM conversation_turns").fetchall()
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["model"] == "claude-3"
        resp = json.loads(row["response_message"])
        assert resp[0]["text"] == "streamed response"

    def test_streamed_with_thinking(self):
        conn = _make_db()
        req = {"messages": [{"role": "user", "content": "think about this"}]}
        content = [
            {"type": "thinking", "thinking": "let me consider..."},
            {"type": "text", "text": "here is my answer"},
        ]

        capture_turn_streamed(conn, "sid-1", req, content, input_tokens=100)

        rows = conn.execute("SELECT * FROM conversation_turns").fetchall()
        row = dict(rows[0])
        assert row["thinking_blocks"] is not None
        thinking = json.loads(row["thinking_blocks"])
        assert len(thinking) == 1
        assert thinking[0]["thinking"] == "let me consider..."

    def test_no_session_skips(self):
        conn = _make_db()
        capture_turn_streamed(conn, None, {"messages": [{"role": "user", "content": "hi"}]}, [])
        rows = conn.execute("SELECT * FROM conversation_turns").fetchall()
        assert len(rows) == 0


# ── TestCaptureDelegationTurn ──


class TestCaptureDelegationTurn:
    def test_basic_delegation(self):
        conn = _make_db()
        capture_delegation_turn(
            conn,
            session_id="deleg-1",
            cli_id="openai-codex",
            prompt="fix the bug",
            output="Done! Fixed the null check.",
            model="gpt-5.4",
        )

        rows = conn.execute("SELECT * FROM conversation_turns").fetchall()
        assert len(rows) == 1
        row = dict(rows[0])
        assert row["provider"] == "openai-codex"
        assert row["model"] == "gpt-5.4"
        assert row["storage_mode"] == "full"
        req = json.loads(row["request_messages"])
        assert req[0]["content"] == "fix the bug"

    def test_multiple_delegations(self):
        conn = _make_db()
        capture_delegation_turn(conn, "d-1", "openai-codex", "task 1", "out 1")
        capture_delegation_turn(conn, "d-1", "openai-codex", "task 2", "out 2")

        rows = conn.execute("SELECT * FROM conversation_turns ORDER BY turn_number").fetchall()
        assert len(rows) == 2
        assert dict(rows[0])["turn_number"] == 1
        assert dict(rows[1])["turn_number"] == 2


# ── TestDBFunctions ──


class TestDBLogConversationTurn:
    def test_insert_and_retrieve(self):
        conn = _make_db()
        row_id = log_conversation_turn(
            conn,
            session_id="s1",
            turn_number=1,
            storage_mode="full",
            request_messages='[{"role":"user","content":"hi"}]',
            response_message='[{"type":"text","text":"hello"}]',
            model="claude-3",
            total_message_count=1,
            provider="anthropic",
        )
        assert row_id > 0

        rows = get_session_history(conn, "s1")
        assert len(rows) == 1
        assert rows[0]["turn_number"] == 1
        assert rows[0]["storage_mode"] == "full"

    def test_turn_range_filter(self):
        conn = _make_db()
        for i in range(5):
            log_conversation_turn(
                conn, session_id="s1", turn_number=i + 1,
                request_messages="[]",
            )

        # All turns
        all_turns = get_session_history(conn, "s1")
        assert len(all_turns) == 5

        # Range filter
        subset = get_session_history(conn, "s1", turn_start=2, turn_end=4)
        assert len(subset) == 3
        assert subset[0]["turn_number"] == 2
        assert subset[2]["turn_number"] == 4

    def test_thinking_excluded_by_default(self):
        conn = _make_db()
        log_conversation_turn(
            conn, session_id="s1", turn_number=1,
            thinking_blocks='[{"type":"thinking","thinking":"hmm"}]',
        )

        rows = get_session_history(conn, "s1", include_thinking=False)
        assert "thinking_blocks" not in rows[0]

        rows_with = get_session_history(conn, "s1", include_thinking=True)
        assert "thinking_blocks" in rows_with[0]


class TestDBCompactionEvents:
    def test_log_and_retrieve(self):
        conn = _make_db()
        log_compaction_event(
            conn,
            session_id="s1",
            turn_number=5,
            previous_count=20,
            current_count=8,
            dropped_count=12,
            previous_tokens=50000,
            current_tokens=20000,
            token_drop_pct=60.0,
        )

        events = get_session_compactions(conn, "s1")
        assert len(events) == 1
        assert events[0]["dropped_count"] == 12
        assert events[0]["token_drop_pct"] == 60.0

    def test_empty_session(self):
        conn = _make_db()
        events = get_session_compactions(conn, "nonexistent")
        assert events == []


class TestDBHistorySessions:
    def test_list_sessions_with_history(self):
        conn = _make_db()
        now = time.time()
        for sid in ["s1", "s2"]:
            for i in range(3):
                conn.execute(
                    """INSERT INTO conversation_turns
                       (ts, session_id, turn_number, storage_mode, provider,
                        request_size_bytes, response_size_bytes)
                       VALUES (?, ?, ?, 'full', 'anthropic', 100, 50)""",
                    (now, sid, i + 1),
                )
        conn.commit()

        sessions = get_history_sessions(conn, window_hours=1)
        assert len(sessions) == 2
        for s in sessions:
            assert s["total_turns"] == 3

    def test_window_filter(self):
        conn = _make_db()
        old_ts = time.time() - 48 * 3600  # 48h ago
        conn.execute(
            """INSERT INTO conversation_turns
               (ts, session_id, turn_number, storage_mode, provider)
               VALUES (?, 'old-session', 1, 'full', 'anthropic')""",
            (old_ts,),
        )
        conn.commit()

        sessions = get_history_sessions(conn, window_hours=24)
        assert len(sessions) == 0
