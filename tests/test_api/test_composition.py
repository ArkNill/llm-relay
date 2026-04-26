"""Tests for context composition analysis module and API integration."""

import json
import sqlite3
import time

from llm_relay.proxy.composition import (
    CATEGORIES,
    _classify_content_blocks,
    _classify_message,
    _reconstruct_and_classify,
    analyze_session_composition,
    analyze_session_composition_per_turn,
    clear_cache,
)

# ── Classification unit tests ──


class TestClassifyContentBlocks:
    def test_string_content(self):
        result = _classify_content_blocks("hello world")
        assert result["text"] > 0
        assert "tool_use" not in result

    def test_text_block(self):
        blocks = [{"type": "text", "text": "some output"}]
        result = _classify_content_blocks(blocks)
        assert result["text"] > 0

    def test_tool_use_block(self):
        blocks = [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/tmp/x"}}]
        result = _classify_content_blocks(blocks)
        assert result["tool_use"] > 0
        assert result.get("text", 0) == 0

    def test_tool_result_block(self):
        blocks = [{"type": "tool_result", "tool_use_id": "t1", "content": "file contents here"}]
        result = _classify_content_blocks(blocks)
        assert result["tool_result"] > 0

    def test_thinking_block(self):
        blocks = [{"type": "thinking", "thinking": "", "signature": "abc123"}]
        result = _classify_content_blocks(blocks)
        assert result["thinking"] > 0

    def test_mixed_blocks(self):
        blocks = [
            {"type": "thinking", "thinking": ""},
            {"type": "text", "text": "answer"},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
        ]
        result = _classify_content_blocks(blocks)
        assert result["thinking"] > 0
        assert result["text"] > 0
        assert result["tool_use"] > 0


class TestClassifyMessage:
    def test_system_message(self):
        msg = {"role": "system", "content": "You are a helpful assistant."}
        result = _classify_message(msg)
        assert result["system"] > 0
        assert result.get("user_text", 0) == 0

    def test_user_text_message(self):
        msg = {"role": "user", "content": "How do I fix this?"}
        result = _classify_message(msg)
        assert result["user_text"] > 0

    def test_user_tool_result_message(self):
        msg = {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "file data here..."}
            ],
        }
        result = _classify_message(msg)
        assert result["tool_result"] > 0
        assert result.get("user_text", 0) == 0

    def test_assistant_text(self):
        msg = {"role": "assistant", "content": [{"type": "text", "text": "Here is the answer."}]}
        result = _classify_message(msg)
        assert result["assistant_text"] > 0

    def test_assistant_tool_use(self):
        msg = {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/x"}},
            ],
        }
        result = _classify_message(msg)
        assert result["tool_use"] > 0

    def test_assistant_thinking(self):
        msg = {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "text", "text": "done"},
            ],
        }
        result = _classify_message(msg)
        assert result["thinking_overhead"] > 0
        assert result["assistant_text"] > 0


# ── Reconstruction tests ──


def _make_turn(turn_number, storage_mode, messages):
    return {
        "turn_number": turn_number,
        "storage_mode": storage_mode,
        "request_messages": json.dumps(messages),
    }


class TestReconstructAndClassify:
    def test_single_full_turn(self):
        turns = [
            _make_turn(1, "full", [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            ]),
        ]
        cat_bytes, total, dupes = _reconstruct_and_classify(turns)
        assert total > 0
        assert cat_bytes["user_text"] > 0
        assert cat_bytes["assistant_text"] > 0
        assert len(dupes) == 0

    def test_delta_accumulation(self):
        turns = [
            _make_turn(1, "full", [
                {"role": "user", "content": "first"},
            ]),
            _make_turn(2, "delta", [
                {"role": "assistant", "content": [{"type": "text", "text": "response"}]},
                {"role": "user", "content": "second"},
            ]),
        ]
        cat_bytes, total, dupes = _reconstruct_and_classify(turns)
        assert cat_bytes["user_text"] > 0
        assert cat_bytes["assistant_text"] > 0

    def test_compaction_replaces_context(self):
        turns = [
            _make_turn(1, "full", [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": [{"type": "text", "text": "long response " * 100}]},
            ]),
            _make_turn(2, "full", [
                {"role": "user", "content": "after compaction"},
            ]),
        ]
        cat_bytes, total, dupes = _reconstruct_and_classify(turns)
        # After compaction, only the second full snapshot should be in context
        assert cat_bytes["assistant_text"] == 0
        assert cat_bytes["user_text"] > 0

    def test_duplicate_read_detection(self):
        read_msg = {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/tmp/test.py"}},
            ],
        }
        turns = [
            _make_turn(1, "full", [read_msg]),
            _make_turn(2, "delta", [read_msg]),
            _make_turn(3, "delta", [read_msg]),
        ]
        _, _, dupes = _reconstruct_and_classify(turns)
        assert "/tmp/test.py" in dupes
        assert dupes["/tmp/test.py"] == 3

    def test_invalid_json_skipped(self):
        turns = [
            {"turn_number": 1, "storage_mode": "full", "request_messages": "not valid json"},
            _make_turn(2, "full", [{"role": "user", "content": "ok"}]),
        ]
        cat_bytes, total, dupes = _reconstruct_and_classify(turns)
        assert total > 0

    def test_null_request_messages_skipped(self):
        turns = [
            {"turn_number": 1, "storage_mode": "full", "request_messages": None},
        ]
        cat_bytes, total, dupes = _reconstruct_and_classify(turns)
        assert total == 0


# ── analyze_session_composition tests ──


class TestAnalyzeSessionComposition:
    def setup_method(self):
        clear_cache()

    def _make_conn(self, turns_data):
        """Create an in-memory SQLite DB with test data."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE conversation_turns (
                id INTEGER PRIMARY KEY,
                ts REAL, session_id TEXT, turn_number INTEGER,
                storage_mode TEXT, request_messages TEXT,
                request_size_bytes INTEGER DEFAULT 0,
                response_size_bytes INTEGER DEFAULT 0,
                provider TEXT DEFAULT 'anthropic'
            )"""
        )
        for i, t in enumerate(turns_data):
            conn.execute(
                """INSERT INTO conversation_turns
                   (id, ts, session_id, turn_number, storage_mode, request_messages)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (i + 1, time.time(), "test-session", t["turn_number"],
                 t["storage_mode"], t["request_messages"]),
            )
        conn.commit()
        return conn

    def test_basic_analysis(self):
        conn = self._make_conn([
            _make_turn(1, "full", [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": [{"type": "text", "text": "world"}]},
            ]),
        ])
        result = analyze_session_composition(conn, "test-session")
        assert result is not None
        assert "categories" in result
        assert "snr" in result
        assert result["total_bytes"] > 0
        assert result["categories"]["user_text"]["pct"] > 0
        assert result["categories"]["assistant_text"]["pct"] > 0

    def test_returns_none_for_missing_session(self):
        conn = self._make_conn([])
        result = analyze_session_composition(conn, "nonexistent")
        assert result is None

    def test_cache_hit(self):
        conn = self._make_conn([
            _make_turn(1, "full", [{"role": "user", "content": "test"}]),
        ])
        r1 = analyze_session_composition(conn, "test-session")
        r2 = analyze_session_composition(conn, "test-session")
        assert r1 is r2  # Same object = cache hit

    def test_cache_invalidation_on_new_turn(self):
        conn = self._make_conn([
            _make_turn(1, "full", [{"role": "user", "content": "test"}]),
        ])
        r1 = analyze_session_composition(conn, "test-session")

        # Add a new turn
        conn.execute(
            """INSERT INTO conversation_turns
               (id, ts, session_id, turn_number, storage_mode, request_messages)
               VALUES (99, ?, 'test-session', 2, 'delta', ?)""",
            (time.time(), json.dumps([{"role": "assistant", "content": "reply"}])),
        )
        conn.commit()

        r2 = analyze_session_composition(conn, "test-session")
        assert r2 is not r1  # Different object = cache miss, recomputed

    def test_snr_calculation(self):
        conn = self._make_conn([
            _make_turn(1, "full", [
                {"role": "user", "content": "short"},
                {"role": "assistant", "content": [
                    {"type": "text", "text": "short"},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t1",
                     "content": "very long tool result " * 50},
                ]},
            ]),
        ])
        result = analyze_session_composition(conn, "test-session")
        # Tool result dominates → SNR should be low
        assert result["snr"] < 1.0
        assert result["categories"]["tool_result"]["pct"] > 50

    def test_all_categories_present(self):
        conn = self._make_conn([
            _make_turn(1, "full", [{"role": "user", "content": "x"}]),
        ])
        result = analyze_session_composition(conn, "test-session")
        for cat in CATEGORIES:
            assert cat in result["categories"]
            assert "bytes" in result["categories"][cat]
            assert "pct" in result["categories"][cat]

    def test_duplicate_reads_dict_returned(self):
        """duplicate_reads dict should contain {filepath: count} for files read > 1 time."""
        read_msg = {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "Read",
                 "input": {"file_path": "/tmp/a.py"}},
                {"type": "tool_use", "id": "t2", "name": "Read",
                 "input": {"file_path": "/tmp/b.py"}},
            ],
        }
        conn = self._make_conn([
            _make_turn(1, "full", [read_msg]),
            _make_turn(2, "delta", [read_msg]),
            _make_turn(3, "delta", [read_msg]),
        ])
        result = analyze_session_composition(conn, "test-session")
        assert "duplicate_reads" in result
        assert result["duplicate_reads"]["/tmp/a.py"] == 3
        assert result["duplicate_reads"]["/tmp/b.py"] == 3
        assert result["duplicate_read_count"] == 2

    def test_duplicate_read_warning_threshold(self, monkeypatch):
        """duplicate_read_warning is True when any file exceeds the threshold."""
        monkeypatch.setenv("DUPLICATE_READ_WARN_THRESHOLD", "3")
        read_msg = {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "Read",
                 "input": {"file_path": "/tmp/hot.py"}},
            ],
        }
        conn = self._make_conn([
            _make_turn(1, "full", [read_msg]),
            _make_turn(2, "delta", [read_msg]),
            _make_turn(3, "delta", [read_msg]),
        ])
        clear_cache()
        result = analyze_session_composition(conn, "test-session")
        assert result["duplicate_read_warning"] is True

    def test_duplicate_read_warning_false_below_threshold(self, monkeypatch):
        """duplicate_read_warning is False when all files are below threshold."""
        monkeypatch.setenv("DUPLICATE_READ_WARN_THRESHOLD", "10")
        read_msg = {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "Read",
                 "input": {"file_path": "/tmp/ok.py"}},
            ],
        }
        conn = self._make_conn([
            _make_turn(1, "full", [read_msg]),
            _make_turn(2, "delta", [read_msg]),
        ])
        clear_cache()
        result = analyze_session_composition(conn, "test-session")
        assert result["duplicate_read_warning"] is False

    def test_snr_recommendation_when_low(self, monkeypatch):
        """snr_recommendation should be a string when SNR is below CC_SNR_WARNING."""
        monkeypatch.setenv("CC_SNR_WARNING", "1.0")
        conn = self._make_conn([
            _make_turn(1, "full", [
                {"role": "user", "content": "short"},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t1",
                     "content": "very long tool result " * 50},
                ]},
            ]),
        ])
        clear_cache()
        result = analyze_session_composition(conn, "test-session")
        assert result["snr_recommendation"] is not None
        assert "SNR" in result["snr_recommendation"]
        assert "new session" in result["snr_recommendation"]

    def test_snr_recommendation_none_when_healthy(self):
        """snr_recommendation should be None when SNR is above threshold."""
        conn = self._make_conn([
            _make_turn(1, "full", [
                {"role": "user", "content": "a " * 500},
                {"role": "assistant", "content": [{"type": "text", "text": "b " * 500}]},
            ]),
        ])
        clear_cache()
        result = analyze_session_composition(conn, "test-session")
        assert result["snr_recommendation"] is None


class TestPerTurnComposition:
    """Tests for analyze_session_composition_per_turn()."""

    def _make_conn(self, turns):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE conversation_turns (
            id INTEGER PRIMARY KEY,
            ts REAL,
            session_id TEXT,
            turn_number INTEGER,
            storage_mode TEXT,
            request_messages TEXT,
            response_message TEXT,
            thinking_blocks TEXT,
            model TEXT,
            temperature REAL,
            max_tokens INTEGER,
            total_message_count INTEGER DEFAULT 0,
            previous_message_count INTEGER DEFAULT 0,
            request_size_bytes INTEGER DEFAULT 0,
            response_size_bytes INTEGER DEFAULT 0,
            request_id INTEGER
        )""")
        for i, t in enumerate(turns):
            conn.execute(
                """INSERT INTO conversation_turns
                   (id, ts, session_id, turn_number, storage_mode, request_messages)
                   VALUES (?, ?, 'test-session', ?, ?, ?)""",
                (i + 1, time.time(), t["turn_number"], t["storage_mode"], t["request_messages"]),
            )
        conn.commit()
        clear_cache()
        return conn

    def test_basic_per_turn(self):
        """Per-turn analysis should return one entry per turn."""
        conn = self._make_conn([
            _make_turn(1, "full", [{"role": "user", "content": "hello"}]),
            _make_turn(2, "delta", [{"role": "assistant", "content": [{"type": "text", "text": "world"}]}]),
            _make_turn(3, "delta", [{"role": "user", "content": "more"}]),
        ])
        result = analyze_session_composition_per_turn(conn, "test-session")
        assert result is not None
        assert result["total_turns"] == 3
        assert result["sampled"] is False
        assert len(result["turns"]) == 3
        # Each turn should have growing total_bytes
        assert result["turns"][0]["composition"]["total_bytes"] > 0
        assert result["turns"][2]["composition"]["total_bytes"] > result["turns"][0]["composition"]["total_bytes"]

    def test_returns_none_for_missing_session(self):
        conn = self._make_conn([])
        result = analyze_session_composition_per_turn(conn, "nonexistent")
        assert result is None

    def test_sampling_over_50_turns(self):
        """Sessions with > 50 turns should be sampled."""
        turns = [
            _make_turn(i + 1, "full" if i == 0 else "delta",
                       [{"role": "user", "content": f"msg {i}"}])
            for i in range(60)
        ]
        conn = self._make_conn(turns)
        result = analyze_session_composition_per_turn(conn, "test-session")
        assert result["total_turns"] == 60
        assert result["sampled"] is True
        assert len(result["turns"]) < 60
        # First and last turns should be included
        turn_nums = [t["turn"] for t in result["turns"]]
        assert 1 in turn_nums
        assert 60 in turn_nums

    def test_compaction_detection(self):
        """Compaction should be detected when full turn has fewer messages."""
        conn = self._make_conn([
            _make_turn(1, "full", [
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": [{"type": "text", "text": "b"}]},
                {"role": "user", "content": "c"},
            ]),
            _make_turn(2, "full", [
                {"role": "user", "content": "compacted"},
            ]),
        ])
        result = analyze_session_composition_per_turn(conn, "test-session")
        assert result["turns"][1]["compacted"] is True
