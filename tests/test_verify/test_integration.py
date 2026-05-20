"""Tests for verify integration checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_relay.verify import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIPPED,
    STATUS_WARN,
)
from llm_relay.verify.integration import ALL_CLIS, verify_integration


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect Path.home() and shutil.which to a controlled state.

    Also strips ANTHROPIC_BASE_URL from the process env so settings.json is the
    sole signal for the proxy_route check (otherwise the developer's real
    shell env leaks into the test result).
    """
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setattr("llm_relay.verify.integration.Path.home", lambda: fake)
    # Default: no binaries present. Tests override per-CLI.
    monkeypatch.setattr("llm_relay.verify.integration.shutil.which", lambda name: None)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    return fake


def _make_claude_settings(home: Path, *, base_url=None, mcp_registered=False) -> Path:
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True)
    settings = {}
    if base_url:
        settings["env"] = {"ANTHROPIC_BASE_URL": base_url}
    if mcp_registered:
        settings["mcpServers"] = {"llm-relay": {"command": "llm-relay-mcp", "type": "stdio"}}
    path = claude_dir / "settings.json"
    path.write_text(json.dumps(settings))
    return path


class TestVerifyIntegrationDispatch:
    def test_single_cli_target_is_cli_id(self, fake_home):
        report = verify_integration("claude-code")
        assert report.target == "claude-code"

    def test_all_target_aggregates(self, fake_home):
        report = verify_integration("all")
        assert report.target == "integration"
        # All sub-reports' check ids namespaced by cli_id
        prefixes = {c.id.split(".", 1)[0] for c in report.checks}
        assert prefixes == {"claude-code", "openai-codex", "gemini-cli"}

    def test_none_treated_as_all(self, fake_home):
        report = verify_integration(None)
        assert report.target == "integration"

    def test_unknown_cli_raises(self, fake_home):
        with pytest.raises(ValueError):
            verify_integration("unknown-cli")


class TestClaudeCodeIntegration:
    def test_binary_missing_skips_all(self, fake_home):
        # shutil.which returns None by default in fake_home
        report = verify_integration("claude-code")
        # When binary is missing, every check is skipped (no fail)
        statuses = {c.status for c in report.checks}
        assert STATUS_FAIL not in statuses
        assert STATUS_SKIPPED in statuses

    def test_binary_present_no_settings(self, fake_home, monkeypatch):
        monkeypatch.setattr(
            "llm_relay.verify.integration.shutil.which",
            lambda name: "/fake/claude" if name == "claude" else None,
        )
        report = verify_integration("claude-code")
        binary = next(c for c in report.checks if c.id == "binary")
        settings = next(c for c in report.checks if c.id == "settings_present")
        assert binary.status == STATUS_PASS
        assert settings.status == STATUS_FAIL

    def test_full_happy_path(self, fake_home, monkeypatch):
        monkeypatch.setattr(
            "llm_relay.verify.integration.shutil.which",
            lambda name: "/fake/claude" if name == "claude" else None,
        )
        _make_claude_settings(
            fake_home, base_url="http://localhost:8083", mcp_registered=True,
        )
        report = verify_integration("claude-code")
        assert report.overall == STATUS_PASS

    def test_proxy_route_warn_when_not_local(self, fake_home, monkeypatch):
        monkeypatch.setattr(
            "llm_relay.verify.integration.shutil.which",
            lambda name: "/fake/claude" if name == "claude" else None,
        )
        _make_claude_settings(
            fake_home, base_url="https://api.anthropic.com", mcp_registered=True,
        )
        report = verify_integration("claude-code")
        proxy = next(c for c in report.checks if c.id == "proxy_route")
        assert proxy.status == STATUS_WARN

    def test_mcp_not_registered_fails(self, fake_home, monkeypatch):
        monkeypatch.setattr(
            "llm_relay.verify.integration.shutil.which",
            lambda name: "/fake/claude" if name == "claude" else None,
        )
        _make_claude_settings(
            fake_home, base_url="http://localhost:8083", mcp_registered=False,
        )
        report = verify_integration("claude-code")
        mcp = next(c for c in report.checks if c.id == "mcp_server")
        assert mcp.status == STATUS_FAIL


class TestCodexIntegration:
    def test_codex_proxy_route_always_skipped(self, fake_home, monkeypatch):
        monkeypatch.setattr(
            "llm_relay.verify.integration.shutil.which",
            lambda name: "/fake/codex" if name == "codex" else None,
        )
        report = verify_integration("openai-codex")
        proxy = next(c for c in report.checks if c.id == "proxy_route")
        assert proxy.status == STATUS_SKIPPED
        # upstream limitation surfaced in data
        assert proxy.data["reason"] == "upstream"


class TestGeminiIntegration:
    def test_gemini_oauth_known_issue_is_warn(self, fake_home, monkeypatch):
        monkeypatch.setattr(
            "llm_relay.verify.integration.shutil.which",
            lambda name: "/fake/gemini" if name == "gemini" else None,
        )
        (fake_home / ".gemini").mkdir()
        report = verify_integration("gemini-cli")
        known = next(c for c in report.checks if c.id == "oauth_known_issue")
        assert known.status == STATUS_WARN
        assert "25425" in known.data["upstream_issue"]


class TestAggregatedAll:
    def test_all_clis_present_aggregates(self, fake_home, monkeypatch):
        # Make all three binaries appear present
        monkeypatch.setattr(
            "llm_relay.verify.integration.shutil.which",
            lambda name: "/fake/{}".format(name) if name in {"claude", "codex", "gemini"} else None,
        )
        _make_claude_settings(fake_home, base_url="http://localhost:8083", mcp_registered=True)
        (fake_home / ".codex").mkdir()
        (fake_home / ".codex" / "config.toml").write_text("# fake\n")
        (fake_home / ".gemini").mkdir()
        report = verify_integration(ALL_CLIS)
        # All three CLI sub-reports show up, namespaced by id
        prefixes = {c.id.split(".", 1)[0] for c in report.checks}
        assert prefixes == {"claude-code", "openai-codex", "gemini-cli"}
