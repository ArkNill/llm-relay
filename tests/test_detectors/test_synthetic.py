"""Tests for synthetic entry detector."""

from __future__ import annotations

from pathlib import Path

from helpers import make_entry, write_session_file

from llm_relay.detect.synthetic import SyntheticDetector
from llm_relay.detect.models import Severity
from llm_relay.detect.parser import parse_session


class TestSyntheticDetector:
    def test_detects_synthetic(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        write_session_file(
            session,
            [
                make_entry(model="claude-opus-4-6"),
                make_entry(
                    uuid="synth-1",
                    model="<synthetic>",
                    stop_reason="stop_sequence",
                    input_tokens=0,
                    output_tokens=0,
                    cache_creation=0,
                    cache_read=0,
                ),
            ],
        )
        parsed = parse_session(session)
        findings = SyntheticDetector().check(parsed)
        assert len(findings) == 1
        assert findings[0].severity == Severity.CRITICAL
        assert "synth-1" in findings[0].evidence[0]

    def test_no_findings_on_clean_session(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        write_session_file(session, [make_entry()])
        parsed = parse_session(session)
        findings = SyntheticDetector().check(parsed)
        assert findings == []

    def test_counts_multiple_synthetic(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        entries = [
            make_entry(
                uuid=f"synth-{i}",
                model="<synthetic>",
                stop_reason="stop_sequence",
                input_tokens=0,
                output_tokens=0,
                cache_creation=0,
                cache_read=0,
            )
            for i in range(5)
        ]
        write_session_file(session, entries)
        parsed = parse_session(session)
        findings = SyntheticDetector().check(parsed)
        assert "5 synthetic entries" in findings[0].detail
