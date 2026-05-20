"""Tests for verify config checks."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from llm_relay.verify import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_WARN,
)
from llm_relay.verify.config import verify_config


def _make_initialized_db_dir(root: Path) -> Path:
    """Create a fake db dir with usage.db containing the requests table."""
    db_dir = root / ".llm-relay"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "usage.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE requests (id INTEGER PRIMARY KEY, ts REAL, session_id TEXT)"
        )
        conn.commit()
    finally:
        conn.close()
    return db_dir


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect db_dir_for_env to a temp dir for isolation."""
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setattr(
        "llm_relay.verify.config.db_dir_for_env",
        lambda: fake / ".llm-relay",
    )
    return fake


class TestVerifyConfig:
    def test_returns_report_with_config_target(self, fake_home):
        report = verify_config()
        assert report.target == "config"

    def test_runs_all_seven_checks(self, fake_home):
        report = verify_config()
        check_ids = {c.id for c in report.checks}
        assert check_ids == {
            "db_dir_exists",
            "db_initialized",
            "db_writable",
            "config_file",
            "knowledge_dir",
            "port_available",
            "no_deprecated_env",
        }

    def test_db_dir_missing_fails(self, fake_home):
        report = verify_config()
        check = next(c for c in report.checks if c.id == "db_dir_exists")
        assert check.status == STATUS_FAIL
        assert "Run `llm-relay init`" in check.remediation

    def test_db_dir_exists_when_initialized(self, fake_home):
        _make_initialized_db_dir(fake_home)
        report = verify_config()
        check = next(c for c in report.checks if c.id == "db_dir_exists")
        assert check.status == STATUS_PASS

    def test_db_initialized_pass(self, fake_home):
        _make_initialized_db_dir(fake_home)
        report = verify_config()
        check = next(c for c in report.checks if c.id == "db_initialized")
        assert check.status == STATUS_PASS
        assert "requests" in check.data["present_tables"]

    def test_db_initialized_fail_without_requests_table(self, fake_home):
        db_dir = fake_home / ".llm-relay"
        db_dir.mkdir(parents=True)
        # Create an empty db without the requests table
        conn = sqlite3.connect(str(db_dir / "usage.db"))
        conn.close()
        report = verify_config()
        check = next(c for c in report.checks if c.id == "db_initialized")
        assert check.status == STATUS_FAIL
        assert "requests" in check.data["missing_tables"]

    def test_db_writable_pass(self, fake_home):
        _make_initialized_db_dir(fake_home)
        report = verify_config()
        check = next(c for c in report.checks if c.id == "db_writable")
        assert check.status == STATUS_PASS

    def test_config_file_warn_when_missing(self, fake_home):
        _make_initialized_db_dir(fake_home)
        report = verify_config()
        check = next(c for c in report.checks if c.id == "config_file")
        assert check.status == STATUS_WARN

    def test_config_file_pass_when_present(self, fake_home):
        db_dir = _make_initialized_db_dir(fake_home)
        (db_dir / "config.json").write_text(json.dumps({"port": 8083}))
        report = verify_config()
        check = next(c for c in report.checks if c.id == "config_file")
        assert check.status == STATUS_PASS
        assert "port" in check.data["keys"]

    def test_knowledge_dir_warn_when_missing(self, fake_home):
        _make_initialized_db_dir(fake_home)
        report = verify_config()
        check = next(c for c in report.checks if c.id == "knowledge_dir")
        assert check.status == STATUS_WARN

    def test_knowledge_dir_pass_when_present(self, fake_home):
        db_dir = _make_initialized_db_dir(fake_home)
        (db_dir / "knowledge").mkdir()
        report = verify_config()
        check = next(c for c in report.checks if c.id == "knowledge_dir")
        assert check.status == STATUS_PASS

    def test_no_deprecated_env_pass_when_clean(self, fake_home, monkeypatch):
        for name in list(__import__("os").environ):
            if name.startswith(("CCPULSE_", "CC_RELAY_")):
                monkeypatch.delenv(name, raising=False)
        report = verify_config()
        check = next(c for c in report.checks if c.id == "no_deprecated_env")
        assert check.status == STATUS_PASS

    def test_no_deprecated_env_warn_when_legacy_set(self, fake_home, monkeypatch):
        monkeypatch.setenv("CCPULSE_DEBUG", "1")
        report = verify_config()
        check = next(c for c in report.checks if c.id == "no_deprecated_env")
        assert check.status == STATUS_WARN
        assert "CCPULSE_DEBUG" in check.data["found"]

    def test_port_available_check_data_has_port(self, fake_home):
        report = verify_config(port=59999)  # extremely unlikely to be bound
        check = next(c for c in report.checks if c.id == "port_available")
        assert check.data["port"] == 59999
