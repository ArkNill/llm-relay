"""Tests for session file discovery."""

from __future__ import annotations

from pathlib import Path

from helpers import make_entry, write_session_file

from llm_relay.detect.scanner import discover_sessions


class TestDiscoverSessions:
    def test_finds_jsonl_files(self, tmp_claude_home: Path) -> None:
        projects = tmp_claude_home / "projects" / "test-project"
        session_file = projects / "abc12345-1234-5678-abcd-123456789abc.jsonl"
        write_session_file(session_file, [make_entry()])

        results = discover_sessions(tmp_claude_home / "projects")
        assert len(results) == 1
        assert results[0].session_id == "abc12345-1234-5678-abcd-123456789abc"
        assert results[0].short_id == "abc12345"
        assert results[0].project_dir == "test-project"

    def test_sorts_by_mtime_newest_first(self, tmp_claude_home: Path) -> None:
        import time

        projects = tmp_claude_home / "projects" / "test-project"
        old = projects / "old-session.jsonl"
        write_session_file(old, [make_entry()])
        time.sleep(0.05)
        new = projects / "new-session.jsonl"
        write_session_file(new, [make_entry()])

        results = discover_sessions(tmp_claude_home / "projects")
        assert len(results) == 2
        assert results[0].session_id == "new-session"

    def test_limit(self, tmp_claude_home: Path) -> None:
        projects = tmp_claude_home / "projects" / "test-project"
        for i in range(5):
            write_session_file(projects / f"session-{i}.jsonl", [make_entry()])

        results = discover_sessions(tmp_claude_home / "projects", limit=3)
        assert len(results) == 3

    def test_session_filter(self, tmp_claude_home: Path) -> None:
        projects = tmp_claude_home / "projects" / "test-project"
        write_session_file(projects / "abc12345.jsonl", [make_entry()])
        write_session_file(projects / "def67890.jsonl", [make_entry()])

        results = discover_sessions(tmp_claude_home / "projects", session_filter="abc")
        assert len(results) == 1
        assert results[0].session_id == "abc12345"

    def test_empty_dir(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        results = discover_sessions(empty)
        assert results == []

    def test_nonexistent_dir(self, tmp_path: Path) -> None:
        results = discover_sessions(tmp_path / "nope")
        assert results == []

    def test_multiple_projects(self, tmp_claude_home: Path) -> None:
        proj_a = tmp_claude_home / "projects" / "project-a"
        proj_b = tmp_claude_home / "projects" / "project-b"
        proj_a.mkdir(parents=True)
        proj_b.mkdir(parents=True)

        write_session_file(proj_a / "sess-a.jsonl", [make_entry()])
        write_session_file(proj_b / "sess-b.jsonl", [make_entry()])

        results = discover_sessions(tmp_claude_home / "projects")
        assert len(results) == 2


