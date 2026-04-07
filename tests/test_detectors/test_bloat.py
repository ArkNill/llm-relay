"""Tests for session bloat detector."""

from __future__ import annotations

from pathlib import Path

from helpers import make_entry, write_session_file

from llm_relay.detect.bloat import BloatDetector
from llm_relay.detect.models import Severity
from llm_relay.detect.parser import parse_session


class TestBloatDetector:
    def test_detects_high_inflation(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        # 4 PRELIM + 1 FINAL per request = 5x
        entries = []
        for i in range(3):
            rid = f"req_{i}"
            for j in range(4):
                entries.append(make_entry(uuid=f"a-{i}-{j}", request_id=rid, stop_reason=""))
            entries.append(make_entry(uuid=f"a-{i}-final", request_id=rid, stop_reason="end_turn"))
        write_session_file(session, entries)

        parsed = parse_session(session)
        findings = BloatDetector().check(parsed)
        assert len(findings) == 1
        assert findings[0].severity == Severity.WARN
        assert "5.0x" in findings[0].detail

    def test_normal_session_no_finding(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        write_session_file(
            session,
            [make_entry(request_id=f"req_{i}") for i in range(5)],
        )
        parsed = parse_session(session)
        findings = BloatDetector().check(parsed)
        assert findings == []

    def test_mild_inflation_info(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        entries = []
        for i in range(3):
            rid = f"req_{i}"
            entries.append(make_entry(uuid=f"a-{i}-prelim", request_id=rid, stop_reason=""))
            entries.append(make_entry(uuid=f"a-{i}-final", request_id=rid, stop_reason="end_turn"))
        write_session_file(session, entries)

        parsed = parse_session(session)
        findings = BloatDetector().check(parsed)
        assert len(findings) == 1
        assert findings[0].severity == Severity.INFO
