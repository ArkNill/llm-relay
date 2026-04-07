"""Tests for microcompact/context stripping detector."""

from __future__ import annotations

from pathlib import Path

from helpers import make_entry, write_session_file

from llm_relay.detect.microcompact import MicrocompactDetector
from llm_relay.detect.models import Severity
from llm_relay.detect.parser import parse_session


class TestMicrocompactDetector:
    def test_detects_cleared_tool_results(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        write_session_file(
            session,
            [
                make_entry(
                    entry_type="user",
                    content=[
                        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "real content"},
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_2",
                            "content": "[Old tool result content cleared]",
                        },
                    ],
                )
            ],
        )
        parsed = parse_session(session)
        findings = MicrocompactDetector().check(parsed)
        mc_findings = [f for f in findings if f.detector_id == "microcompact"]
        assert len(mc_findings) == 1
        assert mc_findings[0].severity == Severity.WARN

    def test_critical_at_50_plus(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        entries = [
            make_entry(
                entry_type="user",
                uuid=f"u-{i}",
                content=[
                    {"type": "tool_result", "tool_use_id": f"t_{i}", "content": "[Old tool result content cleared]"}
                ],
            )
            for i in range(55)
        ]
        write_session_file(session, entries)
        parsed = parse_session(session)
        findings = MicrocompactDetector().check(parsed)
        mc_findings = [f for f in findings if f.detector_id == "microcompact"]
        assert mc_findings[0].severity == Severity.CRITICAL

    def test_detects_compact_boundary(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        write_session_file(
            session,
            [
                make_entry(entry_type="system", subtype="compact_boundary", content="boundary"),
                make_entry(entry_type="system", subtype="microcompact_boundary", content="micro"),
            ],
        )
        parsed = parse_session(session)
        findings = MicrocompactDetector().check(parsed)
        boundary_findings = [f for f in findings if f.detector_id == "compact_boundary"]
        assert len(boundary_findings) == 1
        assert "1 compact + 1 microcompact" in boundary_findings[0].detail

    def test_clean_session(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        write_session_file(
            session,
            [make_entry(entry_type="user", content=[{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}])],
        )
        parsed = parse_session(session)
        findings = MicrocompactDetector().check(parsed)
        assert findings == []
