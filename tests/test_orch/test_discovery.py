"""Tests for orch/discovery.py — CLI detection and auth probing."""

import os
from unittest.mock import MagicMock, patch

import pytest

from llm_relay.orch.discovery import (
    _CLI_REGISTRY,
    _discover_one,
    _probe_antigravity,
    _probe_claude,
    _probe_codex,
    _probe_gemini,
    discover_all,
    get_available,
    refresh,
)
from llm_relay.orch.models import AuthMethod, CLIStatus


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

    @patch("llm_relay.orch.discovery.shutil.which", return_value=None)
    def test_antigravity_has_no_api_key_auth(self, mock_which):
        # GEMINI_API_KEY belongs to gemini-cli; agy is subscription sign-in only.
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-value"}, clear=False):
            s = _discover_one("antigravity-cli", "agy", "")
        assert s.api_key_available is False
        assert s.api_key_name is None
        assert s.preferred_auth == AuthMethod.NONE

    @patch("llm_relay.orch.discovery.shutil.which", return_value="/usr/bin/agy")
    @patch("llm_relay.orch.discovery._get_version", return_value="1.5.13")
    @patch("llm_relay.orch.discovery._probe_auth", return_value=True)
    def test_antigravity_installed_and_signed_in(self, mock_probe, mock_ver, mock_which):
        s = _discover_one("antigravity-cli", "agy", "")
        assert s.installed is True
        assert s.preferred_auth == AuthMethod.CLI_OAUTH
        assert s.api_key_name is None


class TestRegistry:
    def test_antigravity_registered(self):
        assert ("antigravity-cli", "agy", "") in _CLI_REGISTRY

    def test_existing_entries_unchanged(self):
        assert ("gemini-cli", "gemini", "GEMINI_API_KEY") in _CLI_REGISTRY
        assert ("claude-code", "claude", "ANTHROPIC_API_KEY") in _CLI_REGISTRY
        assert ("openai-codex", "codex", "OPENAI_API_KEY") in _CLI_REGISTRY


class TestProbes:
    def test_probe_claude_no_dir(self, tmp_path):
        with patch("llm_relay.orch.discovery.os.path.isdir", return_value=False):
            assert _probe_claude("/usr/bin/claude") is False

    def test_probe_claude_with_projects(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        projects_dir = claude_dir / "projects"
        projects_dir.mkdir(parents=True)
        with patch(
            "llm_relay.orch.discovery.os.path.isdir",
        ) as mock_isdir:
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
                mock_file = MagicMock(
                    __enter__=MagicMock(return_value=MagicMock(
                        read=MagicMock(
                            return_value='{"access_token":"long_token_here"}',
                        ),
                    )),
                    __exit__=MagicMock(return_value=False),
                )
                with patch("builtins.open", MagicMock(return_value=mock_file)):
                    assert _probe_codex("/usr/bin/codex") is True

    def test_probe_gemini_no_file(self):
        with patch("llm_relay.orch.discovery.os.path.isfile", return_value=False):
            assert _probe_gemini("/usr/bin/gemini") is False

    def test_probe_gemini_with_oauth(self):
        with patch("llm_relay.orch.discovery.os.path.isfile", return_value=True):
            assert _probe_gemini("/usr/bin/gemini") is True

    def test_probe_antigravity_no_file(self, tmp_path):
        missing = tmp_path / "antigravity-oauth-token"
        with patch("llm_relay.orch.discovery.os.path.expanduser", return_value=str(missing)):
            assert _probe_antigravity("/usr/bin/agy") is False

    def test_probe_antigravity_empty_file(self, tmp_path):
        token = tmp_path / "antigravity-oauth-token"
        token.write_text("")
        with patch("llm_relay.orch.discovery.os.path.expanduser", return_value=str(token)):
            assert _probe_antigravity("/usr/bin/agy") is False

    def test_probe_antigravity_non_empty_file(self, tmp_path):
        token = tmp_path / "antigravity-oauth-token"
        token.write_text("x")
        with patch("llm_relay.orch.discovery.os.path.expanduser", return_value=str(token)):
            assert _probe_antigravity("/usr/bin/agy") is True

    def test_probe_antigravity_directory_is_not_a_token(self, tmp_path):
        with patch("llm_relay.orch.discovery.os.path.expanduser", return_value=str(tmp_path)):
            assert _probe_antigravity("/usr/bin/agy") is False

    def test_probe_antigravity_os_error(self, tmp_path):
        with patch("llm_relay.orch.discovery.os.path.isfile", return_value=True):
            with patch("llm_relay.orch.discovery.os.path.getsize", side_effect=OSError("denied")):
                assert _probe_antigravity("/usr/bin/agy") is False

    def test_probe_auth_dispatches_antigravity(self):
        from llm_relay.orch.discovery import _probe_auth
        with patch("llm_relay.orch.discovery._probe_antigravity", return_value=True) as mock_probe:
            assert _probe_auth("antigravity-cli", "/usr/bin/agy") is True
        mock_probe.assert_called_once_with("/usr/bin/agy")


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
        # Should only call _discover_one 4 times (once per CLI, first call only)
        assert mock_discover.call_count == 4
        assert len(result1) == 4
        assert len(result2) == 4

    @patch("llm_relay.orch.discovery._discover_one")
    def test_refresh_clears_cache(self, mock_discover):
        mock_discover.return_value = CLIStatus(
            cli_id="test", binary_name="test", binary_path=None,
            installed=False, cli_authenticated=False,
            api_key_name="TEST", api_key_available=False,
            preferred_auth=AuthMethod.NONE,
        )
        discover_all()
        assert mock_discover.call_count == 4
        refresh()
        assert mock_discover.call_count == 8


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


class TestAntigravityLauncher:
    """Delegated agy calls go through the agyGt wrapper (Antigravity relay) when it is installed."""

    @staticmethod
    def _which_factory(available):
        def fake_which(name):
            return available.get(name)
        return fake_which

    def test_prefers_wrapper_when_present(self, monkeypatch):
        from llm_relay.orch import discovery

        monkeypatch.delenv("LLM_RELAY_AGY_DIRECT", raising=False)
        monkeypatch.setattr(discovery.shutil, "which", self._which_factory(
            {"agyGt": "/home/u/bin/agyGt", "agy": "/home/u/.local/bin/agy"}))
        assert discovery._which("antigravity-cli", "agy") == "/home/u/bin/agyGt"

    def test_falls_back_to_plain_binary(self, monkeypatch):
        from llm_relay.orch import discovery

        monkeypatch.delenv("LLM_RELAY_AGY_DIRECT", raising=False)
        monkeypatch.setattr(discovery.shutil, "which", self._which_factory({"agy": "/home/u/.local/bin/agy"}))
        assert discovery._which("antigravity-cli", "agy") == "/home/u/.local/bin/agy"

    def test_direct_env_skips_wrapper(self, monkeypatch):
        from llm_relay.orch import discovery

        monkeypatch.setenv("LLM_RELAY_AGY_DIRECT", "1")
        monkeypatch.setattr(discovery.shutil, "which", self._which_factory(
            {"agyGt": "/home/u/bin/agyGt", "agy": "/home/u/.local/bin/agy"}))
        assert discovery._which("antigravity-cli", "agy") == "/home/u/.local/bin/agy"

    def test_missing_everywhere(self, monkeypatch):
        from llm_relay.orch import discovery

        monkeypatch.setattr(discovery.shutil, "which", self._which_factory({}))
        assert discovery._which("antigravity-cli", "agy") is None

    def test_other_clis_only_look_for_their_binary(self, monkeypatch):
        from llm_relay.orch import discovery

        asked = []

        def fake_which(name):
            asked.append(name)
            return "/usr/bin/" + name

        monkeypatch.setattr(discovery.shutil, "which", fake_which)
        assert discovery._which("claude-code", "claude") == "/usr/bin/claude"
        assert asked == ["claude"]

    def test_status_keeps_binary_name_agy(self, monkeypatch):
        from llm_relay.orch import discovery

        monkeypatch.delenv("LLM_RELAY_AGY_DIRECT", raising=False)
        monkeypatch.setattr(discovery.shutil, "which", self._which_factory({"agyGt": "/home/u/bin/agyGt"}))
        monkeypatch.setattr(discovery, "_get_version", lambda path, name: "1.5.13")
        monkeypatch.setattr(discovery, "_probe_antigravity", lambda path: True)
        status = discovery._discover_one("antigravity-cli", "agy", "")
        assert status.binary_name == "agy"  # cli="agy" still resolves by binary name
        assert status.binary_path == "/home/u/bin/agyGt"
        assert status.is_usable()
