"""Tests for context composition analysis module and API integration."""

import json
import sqlite3
import time

from llm_relay.proxy.composition import (
    CATEGORIES,
    _classify_content_blocks,
    _classify_message,
    _reconstruct_and_classify,
    analyze_file_composition,
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
        cat_bytes, total, dupes, _tc, _tb, _thc = _reconstruct_and_classify(turns)
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
        cat_bytes, total, dupes, _tc, _tb, _thc = _reconstruct_and_classify(turns)
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
        cat_bytes, total, dupes, _tc, _tb, _thc = _reconstruct_and_classify(turns)
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
        _, _, dupes, _tc, _tb, _thc = _reconstruct_and_classify(turns)
        assert "/tmp/test.py" in dupes
        assert dupes["/tmp/test.py"] == 3

    def test_invalid_json_skipped(self):
        turns = [
            {"turn_number": 1, "storage_mode": "full", "request_messages": "not valid json"},
            _make_turn(2, "full", [{"role": "user", "content": "ok"}]),
        ]
        cat_bytes, total, dupes, _tc, _tb, _thc = _reconstruct_and_classify(turns)
        assert total > 0

    def test_null_request_messages_skipped(self):
        turns = [
            {"turn_number": 1, "storage_mode": "full", "request_messages": None},
        ]
        cat_bytes, total, dupes, _tc, _tb, _thc = _reconstruct_and_classify(turns)
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
        """snr_recommendation should be a string when SNR is below LLM_SNR_WARNING."""
        monkeypatch.setenv("LLM_SNR_WARNING", "1.0")
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


# ── File-based composition tests (Codex / Gemini) ──


class TestAnalyzeFileCompositionCodex:
    """Tests for analyze_file_composition() with Codex JSONL format."""

    def setup_method(self):
        clear_cache()

    def _write_jsonl(self, tmp_path, entries):
        p = tmp_path / "session.jsonl"
        lines = [json.dumps(e, ensure_ascii=False) for e in entries]
        p.write_text("\n".join(lines), encoding="utf-8")
        return str(p)

    def test_basic_codex_session(self, tmp_path):
        entries = [
            {"timestamp": "2026-04-27T12:00:00Z", "type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "hello world"}],
            }},
            {"timestamp": "2026-04-27T12:00:01Z", "type": "response_item", "payload": {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "Hi there!"}],
            }},
        ]
        path = self._write_jsonl(tmp_path, entries)
        result = analyze_file_composition(path, "openai-codex")
        assert result is not None
        assert result["categories"]["user_text"]["bytes"] > 0
        assert result["categories"]["assistant_text"]["bytes"] > 0
        assert result["total_bytes"] > 0
        assert result["est_tokens"] > 0
        assert "snr" in result

    def test_codex_function_call(self, tmp_path):
        entries = [
            {"timestamp": "2026-04-27T12:00:00Z", "type": "response_item", "payload": {
                "type": "function_call", "name": "exec_command",
                "arguments": '{"cmd":"ls","workdir":"/tmp"}',
                "call_id": "call_123",
            }},
            {"timestamp": "2026-04-27T12:00:01Z", "type": "response_item", "payload": {
                "type": "function_call_output", "call_id": "call_123",
                "output": "file1.py\nfile2.py\nfile3.py",
            }},
        ]
        path = self._write_jsonl(tmp_path, entries)
        result = analyze_file_composition(path, "openai-codex")
        assert result is not None
        assert result["categories"]["tool_use"]["bytes"] > 0
        assert result["categories"]["tool_result"]["bytes"] > 0

    def test_codex_reasoning(self, tmp_path):
        entries = [
            {"timestamp": "2026-04-27T12:00:00Z", "type": "response_item", "payload": {
                "type": "reasoning",
                "summary": [{"text": "Let me think about this..."}],
                "encrypted_content": "gAAAA" + "x" * 200,
            }},
        ]
        path = self._write_jsonl(tmp_path, entries)
        result = analyze_file_composition(path, "openai-codex")
        assert result is not None
        assert result["categories"]["thinking_overhead"]["bytes"] > 0

    def test_codex_developer_role(self, tmp_path):
        entries = [
            {"timestamp": "2026-04-27T12:00:00Z", "type": "response_item", "payload": {
                "type": "message", "role": "developer",
                "content": [{"type": "input_text", "text": "You are Codex, a coding agent."}],
            }},
        ]
        path = self._write_jsonl(tmp_path, entries)
        result = analyze_file_composition(path, "openai-codex")
        assert result is not None
        assert result["categories"]["system"]["bytes"] > 0

    def test_codex_session_meta(self, tmp_path):
        entries = [
            {"timestamp": "2026-04-27T12:00:00Z", "type": "session_meta", "payload": {
                "id": "test-id",
                "base_instructions": {"text": "System instructions here"},
            }},
            {"timestamp": "2026-04-27T12:00:01Z", "type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            }},
        ]
        path = self._write_jsonl(tmp_path, entries)
        result = analyze_file_composition(path, "openai-codex")
        assert result is not None
        assert result["categories"]["system"]["bytes"] > 0

    def test_codex_all_categories_present(self, tmp_path):
        entries = [
            {"timestamp": "2026-04-27T12:00:00Z", "type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "test"}],
            }},
        ]
        path = self._write_jsonl(tmp_path, entries)
        result = analyze_file_composition(path, "openai-codex")
        assert result is not None
        for cat in CATEGORIES:
            assert cat in result["categories"]
            assert "bytes" in result["categories"][cat]
            assert "pct" in result["categories"][cat]

    def test_codex_snr_low_when_tool_heavy(self, tmp_path):
        entries = [
            {"timestamp": "2026-04-27T12:00:00Z", "type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "x"}],
            }},
            {"timestamp": "2026-04-27T12:00:01Z", "type": "response_item", "payload": {
                "type": "function_call_output", "call_id": "c1",
                "output": "very long output " * 100,
            }},
        ]
        path = self._write_jsonl(tmp_path, entries)
        result = analyze_file_composition(path, "openai-codex")
        assert result is not None
        assert result["snr"] < 1.0

    def test_codex_cache_hit(self, tmp_path):
        entries = [
            {"timestamp": "2026-04-27T12:00:00Z", "type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "cached"}],
            }},
        ]
        path = self._write_jsonl(tmp_path, entries)
        r1 = analyze_file_composition(path, "openai-codex")
        r2 = analyze_file_composition(path, "openai-codex")
        assert r1 is r2  # Same object = cache hit

    def test_codex_nonexistent_file(self):
        result = analyze_file_composition("/nonexistent/path.jsonl", "openai-codex")
        assert result is None

    def test_codex_empty_file(self, tmp_path):
        p = tmp_path / "empty.jsonl"
        p.write_text("", encoding="utf-8")
        result = analyze_file_composition(str(p), "openai-codex")
        assert result is None

    def test_codex_mixed_session(self, tmp_path):
        """Full realistic Codex session with all entry types."""
        entries = [
            {"timestamp": "2026-04-27T12:00:00Z", "type": "session_meta", "payload": {
                "id": "test", "base_instructions": {"text": "System prompt"},
            }},
            {"timestamp": "2026-04-27T12:00:01Z", "type": "response_item", "payload": {
                "type": "message", "role": "developer",
                "content": [{"type": "input_text", "text": "Sandbox permissions"}],
            }},
            {"timestamp": "2026-04-27T12:00:02Z", "type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "Fix the bug"}],
            }},
            {"timestamp": "2026-04-27T12:00:03Z", "type": "response_item", "payload": {
                "type": "reasoning", "summary": [{"text": "thinking..."}],
                "encrypted_content": "gAAAA" + "y" * 100,
            }},
            {"timestamp": "2026-04-27T12:00:04Z", "type": "response_item", "payload": {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "I'll look at the code."}],
            }},
            {"timestamp": "2026-04-27T12:00:05Z", "type": "response_item", "payload": {
                "type": "function_call", "name": "exec_command",
                "arguments": '{"cmd":"cat src/main.py"}',
            }},
            {"timestamp": "2026-04-27T12:00:06Z", "type": "response_item", "payload": {
                "type": "function_call_output", "call_id": "c1",
                "output": "def main():\n    pass\n",
            }},
        ]
        path = self._write_jsonl(tmp_path, entries)
        result = analyze_file_composition(path, "openai-codex")
        assert result is not None
        cats = result["categories"]
        assert cats["system"]["bytes"] > 0
        assert cats["user_text"]["bytes"] > 0
        assert cats["assistant_text"]["bytes"] > 0
        assert cats["tool_use"]["bytes"] > 0
        assert cats["tool_result"]["bytes"] > 0
        assert cats["thinking_overhead"]["bytes"] > 0

    def test_codex_duplicate_reads(self, tmp_path):
        """Duplicate file reads via exec_command should be tracked."""
        def _fc(cmd):
            return {"timestamp": "2026-04-27T12:00:00Z", "type": "response_item", "payload": {
                "type": "function_call", "name": "exec_command",
                "arguments": json.dumps({"cmd": cmd}),
            }}

        def _fco(output="ok"):
            return {"timestamp": "2026-04-27T12:00:01Z", "type": "response_item", "payload": {
                "type": "function_call_output", "call_id": "c1", "output": output,
            }}

        entries = [
            _fc("sed -n '1,100p' src/main.py"), _fco(),
            _fc("sed -n '100,200p' src/main.py"), _fco(),
            _fc("sed -n '1,50p' src/utils.py"), _fco(),
            _fc("cat src/main.py"), _fco(),
            _fc("nl -ba src/main.py"), _fco(),
        ]
        path = self._write_jsonl(tmp_path, entries)
        result = analyze_file_composition(path, "openai-codex")
        assert result is not None
        assert result["duplicate_read_count"] >= 1
        assert "src/main.py" in result["duplicate_reads"]
        assert result["duplicate_reads"]["src/main.py"] >= 3

    def test_codex_duplicate_read_warning(self, tmp_path, monkeypatch):
        """duplicate_read_warning should be True when threshold exceeded."""
        monkeypatch.setenv("DUPLICATE_READ_WARN_THRESHOLD", "3")

        def _fc(cmd):
            return {"timestamp": "2026-04-27T12:00:00Z", "type": "response_item", "payload": {
                "type": "function_call", "name": "exec_command",
                "arguments": json.dumps({"cmd": cmd}),
            }}

        entries = [
            _fc("sed -n '1,50p' hot.py"),
            _fc("sed -n '50,100p' hot.py"),
            _fc("cat hot.py"),
        ]
        path = self._write_jsonl(tmp_path, entries)
        result = analyze_file_composition(path, "openai-codex")
        assert result is not None
        assert result["duplicate_read_warning"] is True


class TestAnalyzeFileCompositionGemini:
    """Tests for analyze_file_composition() with Gemini JSON format."""

    def setup_method(self):
        clear_cache()

    def test_basic_gemini_session(self, tmp_path):
        data = [
            {"sessionId": "s1", "messageId": 0, "type": "user",
             "message": "Hello Gemini", "timestamp": "2026-04-15T00:07:08Z"},
            {"sessionId": "s1", "messageId": 1, "type": "user",
             "message": "How are you?", "timestamp": "2026-04-15T00:07:20Z"},
        ]
        p = tmp_path / "logs.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        result = analyze_file_composition(str(p), "gemini-cli")
        assert result is not None
        assert result["categories"]["user_text"]["bytes"] > 0
        assert result["total_bytes"] > 0

    def test_gemini_dict_format(self, tmp_path):
        data = {
            "sessionId": "s1",
            "messages": [
                {"type": "user", "content": "Question", "timestamp": "2026-04-15T00:00:00Z"},
                {"type": "model", "content": "Answer here", "timestamp": "2026-04-15T00:00:01Z"},
            ],
        }
        p = tmp_path / "session.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        result = analyze_file_composition(str(p), "gemini-cli")
        assert result is not None
        assert result["categories"]["user_text"]["bytes"] > 0
        assert result["categories"]["assistant_text"]["bytes"] > 0

    def test_gemini_empty_file(self, tmp_path):
        p = tmp_path / "empty.json"
        p.write_text("", encoding="utf-8")
        result = analyze_file_composition(str(p), "gemini-cli")
        assert result is None

    def test_gemini_all_categories_present(self, tmp_path):
        data = [
            {"sessionId": "s1", "type": "user", "message": "test"},
        ]
        p = tmp_path / "session.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        result = analyze_file_composition(str(p), "gemini-cli")
        assert result is not None
        for cat in CATEGORIES:
            assert cat in result["categories"]

    def test_gemini_nonexistent_file(self):
        result = analyze_file_composition("/nonexistent/path.json", "gemini-cli")
        assert result is None

    def test_gemini_cache_hit(self, tmp_path):
        data = [{"sessionId": "s1", "type": "user", "message": "cached msg"}]
        p = tmp_path / "session.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        r1 = analyze_file_composition(str(p), "gemini-cli")
        r2 = analyze_file_composition(str(p), "gemini-cli")
        assert r1 is r2

    def test_gemini_assistant_type(self, tmp_path):
        """Gemini 'gemini' type should map to assistant_text."""
        data = [
            {"sessionId": "s1", "type": "user", "message": "hi"},
            {"sessionId": "s1", "type": "gemini", "message": "hello back"},
        ]
        p = tmp_path / "session.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        result = analyze_file_composition(str(p), "gemini-cli")
        assert result is not None
        assert result["categories"]["assistant_text"]["bytes"] > 0

    def test_unknown_provider(self, tmp_path):
        p = tmp_path / "file.json"
        p.write_text('{"test": true}', encoding="utf-8")
        result = analyze_file_composition(str(p), "unknown-provider")
        assert result is None


class TestAnalyzeFileCompositionDuplicateFields:
    """Verify file composition results have all expected fields."""

    def setup_method(self):
        clear_cache()

    def test_result_fields_match_db_version(self, tmp_path):
        """File composition should return same keys as DB composition."""
        entries = [
            {"timestamp": "2026-04-27T12:00:00Z", "type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "test"}],
            }},
        ]
        p = tmp_path / "session.jsonl"
        p.write_text(json.dumps(entries[0]), encoding="utf-8")
        result = analyze_file_composition(str(p), "openai-codex")
        assert result is not None
        expected_keys = {
            "categories", "total_bytes", "est_tokens", "snr",
            "snr_recommendation", "duplicate_read_count",
            "duplicate_reads", "duplicate_read_warning",
            "tool_calls", "tool_bytes", "thinking_count",
        }
        # exec_stats is optional (only present when exec_total > 0)
        assert expected_keys.issubset(set(result.keys()))


# ── Per-tool detail tests ──


class TestCCToolCalls:
    """Tests for CC per-tool call extraction via _reconstruct_and_classify."""

    def setup_method(self):
        clear_cache()

    def test_tool_names_extracted(self):
        turns = [
            _make_turn(1, "full", [
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/tmp/a.py"}},
                    {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "ls"}},
                ]},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "t3", "name": "Read", "input": {"file_path": "/tmp/b.py"}},
                ]},
            ]),
        ]
        _, _, _, tool_calls, tool_bytes, _ = _reconstruct_and_classify(turns)
        assert tool_calls["Read"] == 2
        assert tool_calls["Bash"] == 1
        assert tool_bytes["Read"]["use"] > 0
        assert tool_bytes["Bash"]["use"] > 0

    def test_thinking_count(self):
        turns = [
            _make_turn(1, "full", [
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "", "signature": "s1"},
                    {"type": "text", "text": "answer"},
                ]},
                {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "", "signature": "s2"},
                ]},
            ]),
        ]
        _, _, _, _, _, thinking_count = _reconstruct_and_classify(turns)
        assert thinking_count == 2

    def test_db_composition_includes_tool_calls(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE conversation_turns (
            id INTEGER PRIMARY KEY, ts REAL, session_id TEXT, turn_number INTEGER,
            storage_mode TEXT, request_messages TEXT,
            request_size_bytes INTEGER DEFAULT 0, response_size_bytes INTEGER DEFAULT 0,
            provider TEXT DEFAULT 'anthropic')""")
        conn.execute(
            "INSERT INTO conversation_turns"
            " (id,ts,session_id,turn_number,storage_mode,request_messages) VALUES (?,?,?,?,?,?)",
            (1, time.time(), "test", 1, "full", json.dumps([
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file": "x"}},
                    {"type": "tool_use", "id": "t2", "name": "Grep", "input": {"pattern": "y"}},
                    {"type": "tool_use", "id": "t3", "name": "Edit", "input": {"file": "z"}},
                ]},
            ])),
        )
        conn.commit()
        result = analyze_session_composition(conn, "test")
        assert result is not None
        assert result["tool_calls"]["Edit"] == 2
        assert result["tool_calls"]["Grep"] == 1
        assert "tool_bytes" in result


class TestCodexToolDetail:
    """Tests for Codex per-tool extraction."""

    def setup_method(self):
        clear_cache()

    def _write_jsonl(self, tmp_path, entries):
        p = tmp_path / "session.jsonl"
        lines = [json.dumps(e, ensure_ascii=False) for e in entries]
        p.write_text("\n".join(lines), encoding="utf-8")
        return str(p)

    def test_exec_stats(self, tmp_path):
        entries = [
            {"timestamp": "2026-04-28T12:00:00Z", "type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "do stuff"}],
            }},
            {"timestamp": "2026-04-28T12:00:01Z", "type": "response_item", "payload": {
                "type": "function_call", "name": "exec_command",
                "arguments": '{"cmd":"ls"}', "call_id": "c1",
            }},
            {"timestamp": "2026-04-28T12:00:02Z", "type": "event_msg", "payload": {
                "type": "exec_command_end", "call_id": "c1",
                "exit_code": 0, "duration": {"secs": 1, "nanos": 500000000},
                "status": "completed",
            }},
            {"timestamp": "2026-04-28T12:00:03Z", "type": "response_item", "payload": {
                "type": "function_call", "name": "exec_command",
                "arguments": '{"cmd":"false"}', "call_id": "c2",
            }},
            {"timestamp": "2026-04-28T12:00:04Z", "type": "event_msg", "payload": {
                "type": "exec_command_end", "call_id": "c2",
                "exit_code": 1, "duration": {"secs": 0, "nanos": 100000000},
                "status": "failed",
            }},
        ]
        path = self._write_jsonl(tmp_path, entries)
        result = analyze_file_composition(path, "openai-codex")
        assert result is not None
        assert result["tool_calls"]["exec_command"] == 2
        assert "exec_stats" in result
        assert result["exec_stats"]["total"] == 2
        assert result["exec_stats"]["success"] == 1
        assert result["exec_stats"]["failed"] == 1
        assert result["exec_stats"]["avg_duration_ms"] > 0

    def test_reasoning_counted(self, tmp_path):
        entries = [
            {"timestamp": "2026-04-28T12:00:00Z", "type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "think"}],
            }},
            {"timestamp": "2026-04-28T12:00:01Z", "type": "response_item", "payload": {
                "type": "reasoning", "summary": [{"text": "hmm"}],
                "encrypted_content": "gAAAA" + "x" * 50,
            }},
            {"timestamp": "2026-04-28T12:00:02Z", "type": "response_item", "payload": {
                "type": "reasoning", "summary": [],
                "encrypted_content": "gBBBB" + "y" * 50,
            }},
        ]
        path = self._write_jsonl(tmp_path, entries)
        result = analyze_file_composition(path, "openai-codex")
        assert result is not None
        assert result["thinking_count"] == 2


class TestGeminiToolDetail:
    """Tests for Gemini per-tool extraction."""

    def setup_method(self):
        clear_cache()

    def test_tool_calls_counted(self, tmp_path):
        data = [
            {"type": "user", "content": [{"text": "search it"}]},
            {"type": "gemini", "content": "searching...",
             "toolCalls": [
                 {"name": "google_web_search", "args": {"query": "test"}, "result": [{"output": "found"}],
                  "status": "success"},
                 {"name": "run_shell_command", "args": {"command": "ls"}, "result": [{"output": "files"}],
                  "status": "success"},
             ]},
            {"type": "gemini", "content": "more...",
             "toolCalls": [
                 {"name": "google_web_search", "args": {"query": "more"}, "result": [{"output": "more"}],
                  "status": "success"},
             ]},
        ]
        p = tmp_path / "session.jsonl"
        lines = [json.dumps(e, ensure_ascii=False) for e in data]
        p.write_text("\n".join(lines), encoding="utf-8")
        result = analyze_file_composition(str(p), "gemini-cli")
        assert result is not None
        assert result["tool_calls"]["google_web_search"] == 2
        assert result["tool_calls"]["run_shell_command"] == 1
        assert result["tool_bytes"]["google_web_search"]["use"] > 0
        assert result["tool_bytes"]["google_web_search"]["result"] > 0

    def test_exec_stats_from_status(self, tmp_path):
        data = [
            {"type": "user", "content": [{"text": "run"}]},
            {"type": "gemini", "content": "running...",
             "toolCalls": [
                 {"name": "run_shell_command", "args": {"command": "ls"}, "result": [],
                  "status": "success"},
                 {"name": "run_shell_command", "args": {"command": "false"}, "result": [],
                  "status": "failed"},
             ]},
        ]
        p = tmp_path / "session.jsonl"
        lines = [json.dumps(e, ensure_ascii=False) for e in data]
        p.write_text("\n".join(lines), encoding="utf-8")
        result = analyze_file_composition(str(p), "gemini-cli")
        assert result is not None
        assert result["exec_stats"]["total"] == 2
        assert result["exec_stats"]["success"] == 1
        assert result["exec_stats"]["failed"] == 1

    def test_thinking_count(self, tmp_path):
        data = [
            {"type": "user", "content": [{"text": "think"}]},
            {"type": "gemini", "content": "thinking...",
             "thoughts": [
                 {"subject": "s1", "description": "detailed reasoning here"},
                 {"subject": "s2", "description": "more reasoning"},
             ]},
        ]
        p = tmp_path / "session.jsonl"
        lines = [json.dumps(e, ensure_ascii=False) for e in data]
        p.write_text("\n".join(lines), encoding="utf-8")
        result = analyze_file_composition(str(p), "gemini-cli")
        assert result is not None
        assert result["thinking_count"] == 2
