"""Tests for llm-relay init setup module (Docker-only mode)."""

from unittest.mock import MagicMock, patch

from llm_relay.setup_init import (
    _check_docker,
    _detect_clis,
    _is_port_in_use,
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
        assert _is_port_in_use(59999) is False


class TestCheckDocker:
    @patch("shutil.which", return_value=None)
    def test_docker_not_installed(self, _mock):
        result = _check_docker()
        assert result["installed"] is False
        assert result["running"] is False

    @patch("shutil.which", return_value="/usr/bin/docker")
    @patch("subprocess.run")
    def test_docker_installed_running(self, mock_run, _mock_which):
        mock_run.return_value = MagicMock(returncode=0, stdout="27.0.0\n")
        result = _check_docker()
        assert result["installed"] is True


class TestRunInit:
    @patch("llm_relay.setup_init._check_container", return_value={"running": False, "healthy": False, "port": None})
    @patch("llm_relay.setup_init._check_docker", return_value={"installed": True, "running": True, "compose": True, "version": "27.0.0"})
    def test_docker_not_running_shows_instructions(self, _docker, _container, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        summary = run_init(port=8080)
        assert summary["docker"]["installed"] is True
        assert len(summary["next_steps"]) > 0
        assert any("docker compose" in step.lower() for step in summary["next_steps"])

    @patch("llm_relay.setup_init._check_container", return_value={"running": True, "healthy": True, "port": 8080})
    @patch("llm_relay.setup_init._check_docker", return_value={"installed": True, "running": True, "compose": True, "version": "27.0.0"})
    def test_container_running_shows_urls(self, _docker, _container, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        summary = run_init(port=8080)
        assert summary["urls"]["dashboard"] == "http://localhost:8080/dashboard/"
        assert summary["container"]["healthy"] is True

    @patch("llm_relay.setup_init._check_container", return_value={"running": False, "healthy": False, "port": None})
    @patch("llm_relay.setup_init._check_docker", return_value={"installed": False, "running": False, "compose": False, "version": None})
    def test_no_docker_shows_install_link(self, _docker, _container, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        summary = run_init(port=8080)
        assert any("docker" in step.lower() and "install" in step.lower() for step in summary["next_steps"])

    def test_no_cc_settings_mutation(self, tmp_path, monkeypatch):
        """Verify init does NOT create or modify ~/.claude/settings.json."""
        monkeypatch.setenv("HOME", str(tmp_path))
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        with patch("llm_relay.setup_init._check_docker", return_value={"installed": False, "running": False, "compose": False, "version": None}):
            with patch("llm_relay.setup_init._check_container", return_value={"running": False, "healthy": False, "port": None}):
                run_init(port=8080)
        assert not (claude_dir / "settings.json").exists()

    def test_no_db_creation(self, tmp_path, monkeypatch):
        """Verify init does NOT create ~/.llm-relay/ directory."""
        monkeypatch.setenv("HOME", str(tmp_path))
        with patch("llm_relay.setup_init._check_docker", return_value={"installed": False, "running": False, "compose": False, "version": None}):
            with patch("llm_relay.setup_init._check_container", return_value={"running": False, "healthy": False, "port": None}):
                run_init(port=8080)
        assert not (tmp_path / ".llm-relay").exists()
