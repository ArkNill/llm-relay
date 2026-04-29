"""Tests for doctor health checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_relay.recover.doctor import (
    DoctorReport,
    HealthResult,
    check_trust_dialog_hang,
    check_claude_json_corruption,
    check_zombie_sessions,
    check_relay_health,
    run_doctor,
)


class TestHealthResult:
    def test_ok_result(self):
        r = HealthResult("test", "ok", "all good")
        assert r.status == "ok"
        assert r.recommendation == ""

    def test_issue_result(self):
        r = HealthResult("test", "issue", "broken", recommendation="fix it", fixable=True)
        assert r.status == "issue"
        assert r.fixable is True


class TestDoctorReport:
    def test_empty_report(self):
        report = DoctorReport()
        assert report.issues == []
        assert report.warnings == []

    def test_mixed_results(self):
        report = DoctorReport(results=[
            HealthResult("a", "ok", "fine"),
            HealthResult("b", "issue", "broken"),
            HealthResult("c", "warning", "watch out"),
        ])
        assert len(report.issues) == 1
        assert len(report.warnings) == 1
        assert report.issues[0].name == "b"


class TestCheckTrustDialogHang:
    def test_no_claude_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr("llm_relay.recover.doctor._claude_json_path", lambda: tmp_path / "missing.json")
        result = check_trust_dialog_hang()
        assert result.status == "ok"

    def test_trust_dialog_true(self, tmp_path, monkeypatch):
        p = tmp_path / ".claude.json"
        p.write_text(json.dumps({"hasTrustDialogAccepted": True}))
        monkeypatch.setattr("llm_relay.recover.doctor._claude_json_path", lambda: p)
        result = check_trust_dialog_hang()
        assert result.status == "warning"

    def test_trust_dialog_false(self, tmp_path, monkeypatch):
        p = tmp_path / ".claude.json"
        p.write_text(json.dumps({"hasTrustDialogAccepted": False}))
        monkeypatch.setattr("llm_relay.recover.doctor._claude_json_path", lambda: p)
        result = check_trust_dialog_hang()
        assert result.status == "ok"

    def test_corrupted_json(self, tmp_path, monkeypatch):
        p = tmp_path / ".claude.json"
        p.write_text("{invalid json")
        monkeypatch.setattr("llm_relay.recover.doctor._claude_json_path", lambda: p)
        result = check_trust_dialog_hang()
        assert result.status == "issue"


class TestCheckClaudeJsonCorruption:
    def test_no_file_ok(self, tmp_path, monkeypatch):
        monkeypatch.setattr("llm_relay.recover.doctor._claude_json_path", lambda: tmp_path / "missing.json")
        result = check_claude_json_corruption()
        assert result.status == "ok"

    def test_valid_json_ok(self, tmp_path, monkeypatch):
        p = tmp_path / ".claude.json"
        p.write_text(json.dumps({"key": "value"}))
        monkeypatch.setattr("llm_relay.recover.doctor._claude_json_path", lambda: p)
        result = check_claude_json_corruption()
        assert result.status == "ok"

    def test_corrupted_json_issue(self, tmp_path, monkeypatch):
        p = tmp_path / ".claude.json"
        p.write_text("{bad json")
        monkeypatch.setattr("llm_relay.recover.doctor._claude_json_path", lambda: p)
        result = check_claude_json_corruption()
        assert result.status == "issue"


class TestCheckZombieSessions:
    def test_returns_health_result(self):
        result = check_zombie_sessions()
        assert isinstance(result, HealthResult)
        assert result.name == "zombie-sessions"
        assert result.status in ("ok", "warning")


class TestCheckRelayHealth:
    def test_returns_health_result(self):
        result = check_relay_health()
        assert isinstance(result, HealthResult)
        assert result.name == "relay-health"


class TestRunDoctor:
    def test_returns_report(self, tmp_path, monkeypatch):
        monkeypatch.setattr("llm_relay.recover.doctor._claude_json_path", lambda: tmp_path / "missing.json")
        monkeypatch.setattr("llm_relay.recover.doctor._claude_dir", lambda: tmp_path / "missing")
        report = run_doctor()
        assert isinstance(report, DoctorReport)
        assert len(report.results) >= 1
