"""Tests for JSONL session parser."""

from __future__ import annotations

import json
from pathlib import Path

from helpers import make_entry, write_session_file

from llm_relay.detect.parser import parse_session


class TestParseSession:
    def test_basic_parsing(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        entries = [
            make_entry(entry_type="user", uuid="u1", content="hello"),
            make_entry(uuid="a1", parent_uuid="u1", model="claude-opus-4-6"),
        ]
        write_session_file(session, entries)

        result = parse_session(session)
        assert result.entry_count == 2
        assert result.parse_errors == 0
        assert result.entries[0].type == "user"
        assert result.entries[1].type == "assistant"
        assert result.entries[1].model == "claude-opus-4-6"

    def test_usage_extraction(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        write_session_file(
            session,
            [make_entry(input_tokens=1000, output_tokens=500, cache_creation=100, cache_read=900)],
        )

        result = parse_session(session)
        usage = result.entries[0].usage
        assert usage is not None
        assert usage.input_tokens == 1000
        assert usage.cache_read_ratio == 0.9

    def test_synthetic_detection(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        write_session_file(
            session,
            [
                make_entry(
                    model="<synthetic>",
                    stop_reason="stop_sequence",
                    input_tokens=0,
                    output_tokens=0,
                    cache_creation=0,
                    cache_read=0,
                )
            ],
        )

        result = parse_session(session)
        assert result.entries[0].is_synthetic

    def test_malformed_lines_skipped(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        with open(session, "w") as f:
            f.write(json.dumps(make_entry()) + "\n")
            f.write("this is not valid json\n")
            f.write("{broken\n")
            f.write(json.dumps(make_entry(uuid="second")) + "\n")

        result = parse_session(session)
        assert result.entry_count == 2
        assert result.parse_errors == 2

    def test_empty_lines_skipped(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        with open(session, "w") as f:
            f.write("\n")
            f.write(json.dumps(make_entry()) + "\n")
            f.write("\n\n")

        result = parse_session(session)
        assert result.entry_count == 1
        assert result.parse_errors == 0

    def test_null_bytes_detected(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        entry = make_entry()
        with open(session, "w") as f:
            line = json.dumps(entry)
            f.write(line + "\n")
            # Inject null byte into a line
            f.write('{"type":"user",\x00"uuid":"bad"}\n')

        result = parse_session(session)
        assert result.null_bytes_found

    def test_session_metadata(self, tmp_path: Path) -> None:
        session = tmp_path / "abc12345.jsonl"
        write_session_file(
            session,
            [
                make_entry(timestamp="2026-04-01T10:00:00Z", version="2.1.90"),
                make_entry(timestamp="2026-04-01T12:00:00Z", version="2.1.91"),
            ],
        )

        result = parse_session(session)
        assert result.session_id == "abc12345"
        assert result.first_timestamp == "2026-04-01T10:00:00Z"
        assert result.last_timestamp == "2026-04-01T12:00:00Z"
        assert result.version == "2.1.91"
        assert result.all_versions == ["2.1.90", "2.1.91"]

    def test_group_by_request_id(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        write_session_file(
            session,
            [
                make_entry(uuid="a1", request_id="req_001", stop_reason=""),
                make_entry(uuid="a2", request_id="req_001", stop_reason="end_turn"),
                make_entry(uuid="a3", request_id="req_002", stop_reason="end_turn"),
            ],
        )

        result = parse_session(session)
        groups = result.group_by_request_id()
        assert len(groups) == 2
        assert len(groups["req_001"]) == 2
        assert len(groups["req_002"]) == 1

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        result = parse_session(tmp_path / "nope.jsonl")
        assert result.entry_count == 0
        assert result.file_size_bytes == 0

    def test_system_entry_subtype(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        write_session_file(
            session,
            [make_entry(entry_type="system", subtype="microcompact_boundary", content="boundary")],
        )

        result = parse_session(session)
        assert result.entries[0].type == "system"
        assert result.entries[0].subtype == "microcompact_boundary"

    def test_tool_results_extraction(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        write_session_file(
            session,
            [
                make_entry(
                    entry_type="user",
                    content=[
                        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "file contents here"},
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_2",
                            "content": "[Old tool result content cleared]",
                        },
                    ],
                )
            ],
        )

        result = parse_session(session)
        tool_results = result.entries[0].get_tool_results()
        assert len(tool_results) == 2
