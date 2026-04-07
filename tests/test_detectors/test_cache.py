"""Tests for cache efficiency detector."""

from __future__ import annotations

from pathlib import Path

from helpers import make_entry, write_session_file

from llm_relay.detect.cache import CacheDetector
from llm_relay.detect.models import Severity
from llm_relay.detect.parser import parse_session


class TestCacheDetector:
    def test_healthy_cache(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        write_session_file(
            session,
            [make_entry(request_id=f"req_{i}", cache_read=900, cache_creation=100) for i in range(5)],
        )
        parsed = parse_session(session)
        findings = CacheDetector().check(parsed)
        assert len(findings) >= 1
        assert findings[0].severity == Severity.INFO
        assert "90%" in findings[0].detail

    def test_low_cache(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        write_session_file(
            session,
            [make_entry(request_id=f"req_{i}", cache_read=600, cache_creation=400) for i in range(5)],
        )
        parsed = parse_session(session)
        findings = CacheDetector().check(parsed)
        assert findings[0].severity == Severity.WARN

    def test_critical_cache(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        write_session_file(
            session,
            [make_entry(request_id=f"req_{i}", cache_read=10, cache_creation=990) for i in range(5)],
        )
        parsed = parse_session(session)
        findings = CacheDetector().check(parsed)
        assert findings[0].severity == Severity.CRITICAL

    def test_cold_start_detection(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        entries = [make_entry(request_id=f"req_{i}", cache_read=10, cache_creation=990) for i in range(4)] + [
            make_entry(request_id="req_5", cache_read=900, cache_creation=100)
        ]
        write_session_file(session, entries)
        parsed = parse_session(session)
        findings = CacheDetector().check(parsed)
        cold_start = [f for f in findings if f.detector_id == "cold_start"]
        assert len(cold_start) == 1

    def test_no_usage_data(self, tmp_path: Path) -> None:
        session = tmp_path / "test.jsonl"
        write_session_file(
            session,
            [make_entry(entry_type="user", content="hello")],
        )
        parsed = parse_session(session)
        findings = CacheDetector().check(parsed)
        assert findings == []
