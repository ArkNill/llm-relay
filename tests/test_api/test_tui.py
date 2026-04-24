"""Tests for TUI renderer (llm-relay top)."""

from unittest.mock import patch

from rich.console import Console, Group
from rich.panel import Panel

from llm_relay.detect.tui import (
    _fmt_duration,
    _fmt_tokens,
    _render_session_panel,
    render_top,
)


class TestFormatHelpers:
    def test_fmt_tokens_zero(self):
        assert _fmt_tokens(0) == "0"

    def test_fmt_tokens_small(self):
        assert _fmt_tokens(500) == "500"

    def test_fmt_tokens_thousands(self):
        assert "K" in _fmt_tokens(50000)

    def test_fmt_tokens_millions(self):
        assert "M" in _fmt_tokens(1_500_000)

    def test_fmt_duration_seconds(self):
        assert _fmt_duration(30) == "30s"

    def test_fmt_duration_minutes(self):
        assert _fmt_duration(150) == "2m"

    def test_fmt_duration_hours(self):
        assert _fmt_duration(7500) == "2h5m"


class TestRenderSessionPanel:
    def test_basic_session(self):
        session = {
            "session_id": "22e8effe-5b37-4726-a52f-260d29581908",
            "provider_name": "Claude Code",
            "provider": "claude-code",
            "turns": 142,
            "current_ctx": 229000,
            "peak_ctx": 229000,
            "recent_peak": 229000,
            "cumul_unique": 1200000,
            "ceiling": 1000000,
            "zone": "orange",
            "zone_a": "yellow",
            "zone_b": "orange",
            "connection_type": "tailscale",
            "duration_s": 9000,
            "last_prompt": "컨텍스트 관리 효율화",
            "composition": {
                "categories": {
                    "user_text": {"bytes": 12000, "pct": 10.2},
                    "assistant_text": {"bytes": 8000, "pct": 6.5},
                    "tool_use": {"bytes": 30000, "pct": 24.5},
                    "tool_result": {"bytes": 55000, "pct": 45.0},
                    "thinking_overhead": {"bytes": 17000, "pct": 13.8},
                    "system": {"bytes": 0, "pct": 0.0},
                },
                "snr": 0.38,
                "duplicate_read_count": 26,
            },
        }
        panel = _render_session_panel(session)
        assert isinstance(panel, Panel)
        # Title should contain session info
        assert "22e8effe" in str(panel.title)
        assert "tailscale" in str(panel.title)

    def test_session_without_composition(self):
        session = {
            "session_id": "abc12345-0000-0000-0000-000000000000",
            "provider_name": "Codex",
            "turns": 5,
            "current_ctx": 10000,
            "peak_ctx": 10000,
            "ceiling": 1000000,
            "zone": "green",
            "zone_a": "green",
            "zone_b": "green",
            "composition": None,
            "duration_s": 60,
        }
        panel = _render_session_panel(session)
        assert isinstance(panel, Panel)

    def test_session_without_prompt(self):
        session = {
            "session_id": "def67890-0000-0000-0000-000000000000",
            "provider_name": "Claude Code",
            "turns": 1,
            "current_ctx": 0,
            "peak_ctx": 0,
            "ceiling": 1000000,
            "zone": "green",
            "zone_a": "green",
            "zone_b": "green",
            "last_prompt": "",
            "composition": None,
            "duration_s": 0,
        }
        panel = _render_session_panel(session)
        assert isinstance(panel, Panel)


class TestRenderTop:
    @patch("llm_relay.detect.tui.fetch_display_data", return_value=None)
    def test_proxy_unreachable(self, mock_fetch):
        result = render_top("127.0.0.1", 8083)
        assert isinstance(result, Group)
        # Should render without error

    @patch(
        "llm_relay.detect.tui.fetch_display_data",
        return_value={"count": 0, "sessions": []},
    )
    def test_no_sessions(self, mock_fetch):
        result = render_top("127.0.0.1", 8083)
        assert isinstance(result, Group)

    @patch(
        "llm_relay.detect.tui.fetch_display_data",
        return_value={
            "count": 1,
            "sessions": [
                {
                    "session_id": "test-sess-1234",
                    "provider_name": "Claude Code",
                    "turns": 50,
                    "current_ctx": 150000,
                    "peak_ctx": 200000,
                    "recent_peak": 180000,
                    "cumul_unique": 500000,
                    "ceiling": 1000000,
                    "zone": "yellow",
                    "zone_a": "yellow",
                    "zone_b": "green",
                    "connection_type": "ssh+tmux",
                    "duration_s": 3600,
                    "last_prompt": "Fix the bug",
                    "composition": None,
                },
            ],
        },
    )
    def test_with_sessions(self, mock_fetch):
        result = render_top("127.0.0.1", 8083)
        assert isinstance(result, Group)
        # Render to string to verify no errors
        console = Console(width=120, force_terminal=True)
        with console.capture() as capture:
            console.print(result)
        output = capture.get()
        assert "test-ses" in output
        assert "ssh+tmux" in output
        assert "50 turns" in output
