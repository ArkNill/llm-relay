"""Tests for connection type detection."""

from unittest.mock import patch

import llm_relay.api.display as _display_mod
from llm_relay.api.display import detect_connection_type


class TestDetectConnectionType:
    def setup_method(self):
        _display_mod._conn_type_cache.clear()

    def test_invalid_pid(self):
        assert detect_connection_type(0) == "unknown"
        assert detect_connection_type(-1) == "unknown"

    @patch.object(_display_mod, "_get_parent_comm_chain", return_value=[])
    @patch.object(_display_mod, "_read_proc_environ", return_value={})
    def test_native_no_env(self, mock_env, mock_chain):
        assert detect_connection_type(1234) == "native"

    @patch.object(_display_mod, "_get_parent_comm_chain", return_value=[])
    @patch.object(
        _display_mod, "_read_proc_environ",
        return_value={"SSH_CONNECTION": "192.168.1.10 54321 192.168.1.20 22"},
    )
    def test_ssh(self, mock_env, mock_chain):
        assert detect_connection_type(1234) == "ssh"

    @patch.object(_display_mod, "_get_parent_comm_chain", return_value=[])
    @patch.object(_display_mod, "_read_proc_environ", return_value={"TMUX": "/tmp/tmux-1000/default,12345,0"})
    def test_tmux_local(self, mock_env, mock_chain):
        assert detect_connection_type(1234) == "tmux"

    @patch.object(_display_mod, "_get_parent_comm_chain", return_value=[])
    @patch.object(
        _display_mod, "_read_proc_environ",
        return_value={"SSH_CONNECTION": "192.168.1.10 54321 192.168.1.20 22", "TMUX": "/tmp/tmux-1000/default,12345,0"},
    )
    def test_ssh_plus_tmux(self, mock_env, mock_chain):
        assert detect_connection_type(1234) == "ssh+tmux"

    @patch.object(_display_mod, "_get_parent_comm_chain", return_value=[])
    @patch.object(
        _display_mod, "_read_proc_environ",
        return_value={"SSH_CONNECTION": "100.64.0.5 54321 100.64.0.1 22"},
    )
    def test_tailscale(self, mock_env, mock_chain):
        assert detect_connection_type(1234) == "tailscale"

    @patch.object(_display_mod, "_get_parent_comm_chain", return_value=[])
    @patch.object(
        _display_mod, "_read_proc_environ",
        return_value={"SSH_CONNECTION": "100.100.1.5 54321 100.100.1.1 22", "TMUX": "1"},
    )
    def test_tailscale_plus_tmux(self, mock_env, mock_chain):
        assert detect_connection_type(1234) == "tailscale+tmux"

    @patch.object(_display_mod, "_get_parent_comm_chain", return_value=[])
    @patch.object(_display_mod, "_read_proc_environ", return_value={"STY": "12345.pts-0.host"})
    def test_screen(self, mock_env, mock_chain):
        assert detect_connection_type(1234) == "screen"

    @patch.object(_display_mod, "_get_parent_comm_chain", return_value=[])
    @patch.object(
        _display_mod, "_read_proc_environ",
        return_value={"SSH_CONNECTION": "10.0.0.1 1234 10.0.0.2 22", "STY": "12345.pts-0.host"},
    )
    def test_ssh_plus_screen(self, mock_env, mock_chain):
        assert detect_connection_type(1234) == "ssh+screen"

    @patch.object(_display_mod, "_get_parent_comm_chain", return_value=[])
    @patch.object(_display_mod, "_read_proc_environ", return_value={"MOSH_SESSION_ID": "abc123"})
    def test_mosh(self, mock_env, mock_chain):
        assert detect_connection_type(1234) == "mosh"

    @patch.object(_display_mod, "_get_parent_comm_chain", return_value=[])
    @patch.object(
        _display_mod, "_read_proc_environ",
        return_value={"MOSH_SESSION_ID": "abc123", "TMUX": "1"},
    )
    def test_mosh_plus_tmux(self, mock_env, mock_chain):
        assert detect_connection_type(1234) == "mosh+tmux"

    @patch.object(
        _display_mod, "_get_parent_comm_chain",
        return_value=[(1234, "claude"), (1200, "bash"), (1100, "sshd")],
    )
    @patch.object(_display_mod, "_read_proc_environ", return_value={})
    def test_ssh_from_parent_tree(self, mock_env, mock_chain):
        assert detect_connection_type(1234) == "ssh"

    @patch.object(
        _display_mod, "_get_parent_comm_chain",
        return_value=[(1234, "claude"), (1200, "bash"), (1100, "tmux: server")],
    )
    @patch.object(_display_mod, "_read_proc_environ", return_value={})
    def test_tmux_from_parent_tree(self, mock_env, mock_chain):
        assert detect_connection_type(1234) == "tmux"

    @patch.object(_display_mod, "_get_parent_comm_chain", return_value=[])
    @patch.object(_display_mod, "_read_proc_environ", return_value={"TMUX": "1"})
    def test_cache_hit(self, mock_env, mock_chain):
        r1 = detect_connection_type(9999)
        assert r1 == "tmux"
        # Second call should use cache (mock won't be called again)
        mock_env.return_value = {}
        r2 = detect_connection_type(9999)
        assert r2 == "tmux"
        assert mock_env.call_count == 1
