"""Tests for resume corruption detector."""

from __future__ import annotations

import json
from pathlib import Path

from helpers import make_entry, write_session_file

from llm_relay.detect.resume import ResumeDetector
from llm_relay.detect.parser import parse_session


class TestResumeDetector:
    def test_detects_null_bytes(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        entry = make_entry()
        with open(session, "w") as f:
            f.write(json.dumps(entry) + "\n")
            f.write('{"type":"user",\x00"uuid":"bad"}\n')

        parsed = parse_session(session)
        findings = ResumeDetector().check(parsed)
        null_findings = [f for f in findings if "Null Byte" in f.title]
        assert len(null_findings) == 1

    def test_detects_timestamp_reversal(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        write_session_file(
            session,
            [
                make_entry(uuid="a1", timestamp="2026-04-03T12:00:00Z"),
                make_entry(uuid="a2", timestamp="2026-04-03T10:00:00Z"),  # earlier!
                make_entry(uuid="a3", timestamp="2026-04-03T14:00:00Z"),
            ],
        )
        parsed = parse_session(session)
        findings = ResumeDetector().check(parsed)
        ts_findings = [f for f in findings if "Timestamp" in f.title]
        assert len(ts_findings) == 1
        assert "1 timestamp reversal" in ts_findings[0].detail

    def test_detects_multi_version(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        write_session_file(
            session,
            [
                make_entry(uuid="a1", version="2.1.89"),
                make_entry(uuid="a2", version="2.1.91"),
            ],
        )
        parsed = parse_session(session)
        findings = ResumeDetector().check(parsed)
        version_findings = [f for f in findings if "Cross-Version" in f.title]
        assert len(version_findings) == 1

    def test_detects_dag_break(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        write_session_file(
            session,
            [
                make_entry(entry_type="user", uuid="u1", parent_uuid="root"),
                make_entry(uuid="a1", parent_uuid="u1"),
                # DAG break: no parentUuid mid-session
                make_entry(entry_type="user", uuid="u2", parent_uuid=""),
            ],
        )
        parsed = parse_session(session)
        findings = ResumeDetector().check(parsed)
        dag_findings = [f for f in findings if "DAG" in f.title]
        assert len(dag_findings) == 1

    def test_detects_parse_errors(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        with open(session, "w") as f:
            f.write(json.dumps(make_entry()) + "\n")
            f.write("not json\n")

        parsed = parse_session(session)
        findings = ResumeDetector().check(parsed)
        parse_findings = [f for f in findings if "Parse" in f.title]
        assert len(parse_findings) == 1

    def test_clean_session(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        write_session_file(
            session,
            [
                make_entry(uuid="a1", timestamp="2026-04-03T10:00:00Z"),
                make_entry(uuid="a2", timestamp="2026-04-03T11:00:00Z"),
            ],
        )
        parsed = parse_session(session)
        findings = ResumeDetector().check(parsed)
        assert findings == []
