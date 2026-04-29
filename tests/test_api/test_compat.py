"""Tests for cross-platform process inspection helpers."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from llm_relay.api._compat import (
    get_process_tty,
    is_cli_process_alive,
)


class TestIsCliProcessAlive:
    def test_nonexistent_pid_returns_false(self):
        assert is_cli_process_alive(999999999) is False

    def test_zero_pid_returns_false(self):
        assert is_cli_process_alive(0) is False

    def test_negative_pid_returns_false(self):
        assert is_cli_process_alive(-1) is False

    def test_returns_bool(self):
        import os
        result = is_cli_process_alive(os.getpid())
        assert isinstance(result, bool)


class TestGetProcessTty:
    def test_nonexistent_pid_returns_none(self):
        assert get_process_tty(999999999) is None

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
    def test_own_pid_returns_string_or_none(self):
        import os
        result = get_process_tty(os.getpid())
        # May be None in CI (no TTY), but should not raise
        assert result is None or isinstance(result, str)


