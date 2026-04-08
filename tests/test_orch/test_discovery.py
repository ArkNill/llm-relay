"""Tests for orch/discovery.py — CLI detection and auth probing."""

import os
from unittest.mock import patch, MagicMock

import pytest

from llm_relay.orch.discovery import (
    _discover_one,
    _probe_claude,
    _probe_codex,
    _probe_gemini,
    discover_all,
    get_available,
    refresh,
)
from llm_relay.orch.models import AuthMethod


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear discovery cache before each test."""
    import llm_relay.orch.discovery as d
    d._cache = None
    yield
    d._cache = None


class TestDiscoverOne:
    @patch("llm_relay.orch.discovery.shutil.which", return_value="/usr/bin/claude")
    @patch("llm_relay.orch.discovery._get_version", return_value="2.1.91")
    @patch("llm_relay.orch.discovery._probe_auth", return_value=True)
    def test_installed_and_authenticated(self, mock_probe, mock_ver, mock_which):
        s = _discover_one("claude-code", "claude", "ANTHROPIC_API_KEY")
        assert s.installed is True
        assert s.cli_authenticated is True
        assert s.preferred_auth == AuthMethod.CLI_OAUTH
        assert s.version == "2.1.91"
        assert s.binary_path == "/usr/bin/claude"

    @patch("llm_relay.orch.discovery.shutil.which", return_value=None)
    def test_not_installed_no_api_key(self, mock_which):
        with patch.dict(os.environ, {}, clear=True):
            s = _discover_one("claude-code", "claude", "ANTHROPIC_API_KEY")
        assert s.installed is False
        assert s.cli_authenticated is False
        assert s.preferred_auth == AuthMethod.NONE

    @patch("llm_relay.orch.discovery.shutil.which", return_value=None)
    def test_not_installed_with_api_key(self, mock_which):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test123"}, clear=False):
            s = _discover_one("openai-codex", "codex", "OPENAI_API_KEY")
        assert s.installed is False
        assert s.api_key_available is True
        assert s.preferred_auth == AuthMethod.API_KEY

    @patch("llm_relay.orch.discovery.shutil.which", return_value="/usr/bin/codex")
    @patch("llm_relay.orch.discovery._get_version", return_value="0.118.0")
    @patch("llm_relay.orch.discovery._probe_auth", return_value=False)
    def test_installed_not_authenticated_with_api_key(self, mock_probe, mock_ver, mock_which):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False):
            s = _discover_one("openai-codex", "codex", "OPENAI_API_KEY")
        assert s.installed is True
        assert s.cli_authenticated is False
        assert s.preferred_auth == AuthMethod.API_KEY


class TestProbes:
    def test_probe_claude_no_dir(self, tmp_path):
        with patch("llm_relay.orch.discovery.os.path.isdir", return_value=False):
            assert _probe_claude("/usr/bin/claude") is False

    def test_probe_claude_with_projects(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        projects_dir = claude_dir / "projects"
        projects_dir.mkdir(parents=True)
        with patch("llm_relay.orch.discovery.os.path.isdir", side_effect=lambda p: os.path.isdir(p) if str(tmp_path) not in p else True):
            with patch("llm_relay.orch.discovery.os.path.expanduser", side_effect=lambda p: str(tmp_path / p.lstrip("~/"))) if False else patch("llm_relay.orch.discovery.os.path.isdir") as mock_isdir:
                mock_isdir.return_value = True
                assert _probe_claude("/usr/bin/claude") is True

    def test_probe_codex_no_file(self):
        with patch("llm_relay.orch.discovery.os.path.isfile", return_value=False):
            assert _probe_codex("/usr/bin/codex") is False

    def test_probe_codex_with_auth(self, tmp_path):
        auth_file = tmp_path / "auth.json"
        auth_file.write_text('{"access_token": "test_token_long_enough"}')
        with patch("llm_relay.orch.discovery.os.path.expanduser", return_value=str(auth_file)):
            with patch("llm_relay.orch.discovery.os.path.isfile", return_value=True):
                with patch("builtins.open", MagicMock(return_value=MagicMock(__enter__=MagicMock(return_value=MagicMock(read=MagicMock(return_value='{"access_token":"long_token_here"}'))), __exit__=MagicMock(return_value=False)))):
                    assert _probe_codex("/usr/bin/codex") is True

    def test_probe_gemini_no_file(self):
        with patch("llm_relay.orch.discovery.os.path.isfile", return_value=False):
            assert _probe_gemini("/usr/bin/gemini") is False

    def test_probe_gemini_with_oauth(self):
        with patch("llm_relay.orch.discovery.os.path.isfile", return_value=True):
            assert _probe_gemini("/usr/bin/gemini") is True


class TestDiscoverAll:
    @patch("llm_relay.orch.discovery._discover_one")
    def test_caching(self, mock_discover):
        mock_discover.return_value = CLIStatus(
            cli_id="test", binary_name="test", binary_path=None,
            installed=False, cli_authenticated=False,
            api_key_name="TEST", api_key_available=False,
            preferred_auth=AuthMethod.NONE,
        )
        result1 = discover_all()
        result2 = discover_all()
        # Should only call _discover_one 3 times (once per CLI, first call only)
        assert mock_discover.call_count == 3
        assert len(result1) == 3
        assert len(result2) == 3

    @patch("llm_relay.orch.discovery._discover_one")
    def test_refresh_clears_cache(self, mock_discover):
        mock_discover.return_value = CLIStatus(
            cli_id="test", binary_name="test", binary_path=None,
            installed=False, cli_authenticated=False,
            api_key_name="TEST", api_key_available=False,
            preferred_auth=AuthMethod.NONE,
        )
        discover_all()
        assert mock_discover.call_count == 3
        refresh()
        assert mock_discover.call_count == 6


class TestGetAvailable:
    @patch("llm_relay.orch.discovery.discover_all")
    def test_require_auth_filters(self, mock_all):
        from llm_relay.orch.models import CLIStatus
        mock_all.return_value = [
            CLIStatus("a", "a", "/a", True, True, "A", False, AuthMethod.CLI_OAUTH),
            CLIStatus("b", "b", None, False, False, "B", False, AuthMethod.NONE),
            CLIStatus("c", "c", "/c", True, False, "C", True, AuthMethod.API_KEY),
        ]
        result = get_available(require_auth=True)
        assert len(result) == 2
        assert result[0].cli_id == "a"
        assert result[1].cli_id == "c"

    @patch("llm_relay.orch.discovery.discover_all")
    def test_no_auth_required(self, mock_all):
        from llm_relay.orch.models import CLIStatus
        mock_all.return_value = [
            CLIStatus("a", "a", "/a", True, True, "A", False, AuthMethod.CLI_OAUTH),
            CLIStatus("b", "b", None, False, False, "B", False, AuthMethod.NONE),
        ]
        result = get_available(require_auth=False)
        assert len(result) == 1  # Only installed ones
        assert result[0].cli_id == "a"


# Import CLIStatus for test fixtures
from llm_relay.orch.models import CLIStatus
