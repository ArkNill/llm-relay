"""Tests for llm-relay init setup module."""

import json
from unittest.mock import MagicMock, patch

from llm_relay.setup_init import (
    _configure_claude_code,
    _detect_clis,
    _find_available_port,
    _init_db,
    _is_port_in_use,
    _read_json,
    _write_config,
    _write_json,
    run_init,
)


class TestDetectCLIs:
    @patch("shutil.which", return_value=None)
    def test_no_clis(self, _mock_which, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        clis = _detect_clis()
        assert len(clis) == 0

    @patch("shutil.which")
    def test_detects_claude(self, mock_which, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".claude").mkdir()
        mock_which.side_effect = lambda name: "/usr/bin/claude" if name == "claude" else None
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="2.1.0\n")
            clis = _detect_clis()
        assert len(clis) >= 1
        cc = [c for c in clis if c["id"] == "claude-code"]
        assert len(cc) == 1
        assert cc[0]["version"] == "2.1.0"


class TestPortHelpers:
    def test_is_port_in_use_false(self):
        # Port 59999 is very unlikely to be in use
        assert _is_port_in_use(59999) is False

    def test_find_available_port(self):
        port = _find_available_port(59990)
        assert 59990 <= port < 60010


class TestJsonHelpers:
    def test_read_missing_file(self, tmp_path):
        result = _read_json(tmp_path / "missing.json")
        assert result == {}

    def test_read_write_roundtrip(self, tmp_path):
        path = tmp_path / "test.json"
        data = {"key": "value", "num": 42}
        _write_json(path, data)
        result = _read_json(path)
        assert result == data

    def test_read_malformed(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json", encoding="utf-8")
        result = _read_json(path)
        assert result == {}


class TestConfigureClaudeCode:
    def test_creates_settings(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        actions = _configure_claude_code(8083)
        settings = json.loads((claude_dir / "settings.json").read_text())
        assert settings["env"]["ANTHROPIC_BASE_URL"] == "http://localhost:8083"
        assert "llm-relay" in settings["mcpServers"]
        assert any("ANTHROPIC_BASE_URL" in a for a in actions)
        assert any("MCP" in a for a in actions)

    def test_skips_if_already_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        existing = {
            "env": {"ANTHROPIC_BASE_URL": "http://localhost:8083"},
            "mcpServers": {"llm-relay": {"command": "llm-relay-mcp"}},
        }
        (claude_dir / "settings.json").write_text(json.dumps(existing))
        actions = _configure_claude_code(8083)
        assert all("skipped" in a.lower() for a in actions)

    def test_merges_with_existing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        existing = {"env": {"OTHER_VAR": "keep"}, "someKey": True}
        (claude_dir / "settings.json").write_text(json.dumps(existing))
        _configure_claude_code(8083)
        settings = json.loads((claude_dir / "settings.json").read_text())
        assert settings["env"]["OTHER_VAR"] == "keep"
        assert settings["env"]["ANTHROPIC_BASE_URL"] == "http://localhost:8083"
        assert settings["someKey"] is True

    def test_dry_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        _configure_claude_code(8083, dry_run=True)
        assert not (claude_dir / "settings.json").exists()

    def test_different_port(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        _configure_claude_code(9090)
        settings = json.loads((claude_dir / "settings.json").read_text())
        assert settings["env"]["ANTHROPIC_BASE_URL"] == "http://localhost:9090"


class TestInitDB:
    def test_creates_db(self, tmp_path):
        db_dir = tmp_path / ".llm-relay"
        result = _init_db(db_dir)
        assert "created" in result.lower() or "exists" in result.lower()
        assert db_dir.exists()

    def test_existing_db(self, tmp_path):
        db_dir = tmp_path / ".llm-relay"
        db_dir.mkdir()
        (db_dir / "usage.db").write_text("fake")
        result = _init_db(db_dir)
        assert "exists" in result.lower()


class TestWriteConfig:
    def test_creates_config(self, tmp_path):
        db_dir = tmp_path / ".llm-relay"
        db_dir.mkdir()
        result = _write_config(db_dir, 8083)
        assert "written" in result.lower()
        config = json.loads((db_dir / "config.json").read_text())
        assert config["port"] == 8083
        assert config["history"] is True

    def test_skips_existing(self, tmp_path):
        db_dir = tmp_path / ".llm-relay"
        db_dir.mkdir()
        (db_dir / "config.json").write_text("{}")
        result = _write_config(db_dir, 8083)
        assert "skipped" in result.lower()


class TestRunInit:
    @patch("llm_relay.setup_init._start_server", return_value=(True, "OK"))
    @patch("llm_relay.setup_init._health_check", return_value=(True, {"proxy": {"ok": True}}))
    def test_dry_run(self, _hc, _srv, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LLM_RELAY_DB", str(tmp_path / ".llm-relay" / "usage.db"))
        (tmp_path / ".claude").mkdir()
        summary = run_init(port=59998, dry_run=True)
        assert summary["version"] is not None
        assert "dry-run" in str(summary["db"]).lower()
        # Settings.json should NOT be created in dry run
        assert not (tmp_path / ".claude" / "settings.json").exists()

    def test_skip_server(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("LLM_RELAY_DB", str(tmp_path / ".llm-relay" / "usage.db"))
        summary = run_init(port=59997, skip_server=True)
        assert summary["server"] is None or "not started" in str(summary.get("server", ""))
