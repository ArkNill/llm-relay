"""Tests for history API endpoints and MCP tool."""

import json
import sys
import time
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from llm_relay.api.routes import get_api_routes


def _make_app():
    return Starlette(routes=get_api_routes())


def _make_history_data():
    """Return mock data for get_session_history."""
    return [
        {
            "id": 1,
            "ts": time.time() - 100,
            "session_id": "sid-1",
            "turn_number": 1,
            "storage_mode": "full",
            "request_messages": '[{"role":"user","content":"hello"}]',
            "response_message": '[{"type":"text","text":"hi"}]',
            "model": "claude-3",
            "temperature": None,
            "max_tokens": None,
            "total_message_count": 1,
            "previous_message_count": 0,
            "request_size_bytes": 50,
            "response_size_bytes": 30,
            "provider": "anthropic",
            "request_id": None,
        },
        {
            "id": 2,
            "ts": time.time() - 50,
            "session_id": "sid-1",
            "turn_number": 2,
            "storage_mode": "delta",
            "request_messages": '[{"role":"assistant","content":"hi"},{"role":"user","content":"how?"}]',
            "response_message": '[{"type":"text","text":"fine"}]',
            "model": "claude-3",
            "temperature": None,
            "max_tokens": None,
            "total_message_count": 3,
            "previous_message_count": 1,
            "request_size_bytes": 80,
            "response_size_bytes": 40,
            "provider": "anthropic",
            "request_id": None,
        },
    ]


def _make_sessions_data():
    """Return mock data for get_history_sessions."""
    return [
        {
            "session_id": "sid-1",
            "total_turns": 5,
            "first_ts": time.time() - 3600,
            "last_ts": time.time(),
            "total_request_bytes": 500,
            "total_response_bytes": 300,
            "provider": "anthropic",
        },
    ]


def _make_compaction_data():
    """Return mock data for get_session_compactions."""
    return [
        {
            "id": 1,
            "ts": time.time() - 50,
            "session_id": "sid-1",
            "turn_number": 3,
            "previous_count": 10,
            "current_count": 5,
            "dropped_count": 5,
            "previous_tokens": 50000,
            "current_tokens": 25000,
            "token_drop_pct": 50.0,
            "dropped_roles": None,
        },
    ]


# ── GET /api/v1/history ──


class TestHistorySessionsEndpoint:
    @patch("llm_relay.api.display.discover_external_cli_sessions", return_value=[])
    @patch("llm_relay.proxy.db.get_history_sessions", return_value=_make_sessions_data())
    @patch("llm_relay.proxy.db.get_conn")
    def test_list_sessions(self, mock_conn, mock_get, _mock_ext):
        client = TestClient(_make_app())
        resp = client.get("/api/v1/history")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["sessions"][0]["session_id"] == "sid-1"

    @patch("llm_relay.api.display.discover_external_cli_sessions", return_value=[])
    @patch("llm_relay.proxy.db.get_history_sessions", return_value=[])
    @patch("llm_relay.proxy.db.get_conn")
    def test_empty_result(self, mock_conn, mock_get, _mock_ext):
        client = TestClient(_make_app())
        resp = client.get("/api/v1/history")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    @patch("llm_relay.api.display.discover_external_cli_sessions", return_value=[])
    @patch("llm_relay.proxy.db.get_history_sessions", return_value=_make_sessions_data())
    @patch("llm_relay.proxy.db.get_conn")
    def test_window_param(self, mock_conn, mock_get, _mock_ext):
        client = TestClient(_make_app())
        resp = client.get("/api/v1/history?window=48")
        assert resp.status_code == 200
        mock_get.assert_called_once()
        # Verify window was passed
        args = mock_get.call_args
        assert args[1].get("window_hours", args[0][1] if len(args[0]) > 1 else 24) is not None


# ── GET /api/v1/history/{session_id} ──


class TestHistoryDetailEndpoint:
    @patch("llm_relay.proxy.db.get_session_history", return_value=_make_history_data())
    @patch("llm_relay.proxy.db.get_conn")
    def test_get_session_history(self, mock_conn, mock_get):
        client = TestClient(_make_app())
        resp = client.get("/api/v1/history/sid-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "sid-1"
        assert data["total_turns"] == 2
        assert len(data["turns"]) == 2

    @patch("llm_relay.proxy.db.get_session_history", return_value=[])
    @patch("llm_relay.proxy.db.get_conn")
    def test_empty_session(self, mock_conn, mock_get):
        client = TestClient(_make_app())
        resp = client.get("/api/v1/history/nonexistent")
        assert resp.status_code == 200
        assert resp.json()["total_turns"] == 0

    @patch("llm_relay.proxy.db.get_session_history", return_value=_make_history_data())
    @patch("llm_relay.proxy.db.get_conn")
    def test_turn_range_params(self, mock_conn, mock_get):
        client = TestClient(_make_app())
        resp = client.get("/api/v1/history/sid-1?turn_start=1&turn_end=3")
        assert resp.status_code == 200
        # Verify params were passed to the DB function
        call_kwargs = mock_get.call_args[1]
        assert call_kwargs.get("turn_start") == 1
        assert call_kwargs.get("turn_end") == 3

    @patch("llm_relay.proxy.db.get_session_history", return_value=_make_history_data())
    @patch("llm_relay.proxy.db.get_conn")
    def test_include_thinking_param(self, mock_conn, mock_get):
        client = TestClient(_make_app())
        resp = client.get("/api/v1/history/sid-1?include_thinking=1")
        assert resp.status_code == 200
        call_kwargs = mock_get.call_args[1]
        assert call_kwargs.get("include_thinking") is True

    @patch("llm_relay.proxy.db.get_session_history", return_value=[])
    @patch("llm_relay.proxy.db.get_conn")
    def test_codex_session_file_fallback(self, mock_conn, mock_get, tmp_path):
        codex_dir = tmp_path / ".codex" / "sessions"
        codex_dir.mkdir(parents=True)
        session_file = codex_dir / "rollout-codex.jsonl"
        session_file.write_text(
            "\n".join([
                json.dumps({
                    "timestamp": "2026-04-24T00:00:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello"}],
                    },
                }),
                json.dumps({
                    "timestamp": "2026-04-24T00:00:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "hi"}],
                    },
                }),
                json.dumps({
                    "timestamp": "2026-04-24T00:00:02Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "arguments": "{\"cmd\":\"pwd\"}",
                        "call_id": "call_1",
                    },
                }),
                json.dumps({
                    "timestamp": "2026-04-24T00:00:03Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call_1",
                        "output": "done",
                    },
                }),
            ]) + "\n",
            encoding="utf-8",
        )

        with patch(
            "llm_relay.api.display._find_session_file",
            return_value=session_file,
        ):
            client = TestClient(_make_app())
            resp = client.get("/api/v1/history/rollout-codex")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total_turns"] == 1
        assert len(data["turns"]) == 1
        turn = data["turns"][0]
        assert turn["provider"] == "openai-codex"
        assert "hello" in turn["request_messages"]
        assert "output_text" in (turn["response_message"] or "")


# ── GET /api/v1/history/{session_id}/compactions ──


class TestCompactionEndpoint:
    @patch("llm_relay.proxy.db.get_session_compactions", return_value=_make_compaction_data())
    @patch("llm_relay.proxy.db.get_conn")
    def test_get_compactions(self, mock_conn, mock_get):
        client = TestClient(_make_app())
        resp = client.get("/api/v1/history/sid-1/compactions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "sid-1"
        assert data["compaction_count"] == 1
        assert data["compactions"][0]["dropped_count"] == 5

    @patch("llm_relay.proxy.db.get_session_compactions", return_value=[])
    @patch("llm_relay.proxy.db.get_conn")
    def test_no_compactions(self, mock_conn, mock_get):
        client = TestClient(_make_app())
        resp = client.get("/api/v1/history/sid-1/compactions")
        assert resp.status_code == 200
        assert resp.json()["compaction_count"] == 0


# ── MCP session_history tool ──


@pytest.mark.skipif(sys.version_info < (3, 10), reason="mcp requires Python 3.10+")
class TestSessionHistoryMCP:
    @patch("llm_relay.proxy.db.get_session_compactions", return_value=[])
    @patch("llm_relay.proxy.db.get_session_history", return_value=_make_history_data())
    @patch("llm_relay.proxy.db.get_conn")
    def test_basic_call(self, mock_conn, mock_hist, mock_comp):
        from llm_relay.mcp.server import session_history
        result = json.loads(session_history("sid-1"))
        assert result["session_id"] == "sid-1"
        assert result["total_turns"] == 2
        assert result["compaction_count"] == 0

    @patch("llm_relay.proxy.db.get_session_compactions", return_value=_make_compaction_data())
    @patch("llm_relay.proxy.db.get_session_history", return_value=[])
    @patch("llm_relay.proxy.db.get_conn")
    def test_with_compactions(self, mock_conn, mock_hist, mock_comp):
        from llm_relay.mcp.server import session_history
        result = json.loads(session_history("sid-1"))
        assert result["compaction_count"] == 1

    @patch("llm_relay.proxy.db.get_session_history", return_value=_make_history_data())
    @patch("llm_relay.proxy.db.get_session_compactions", return_value=[])
    @patch("llm_relay.proxy.db.get_conn")
    def test_turn_range(self, mock_conn, mock_comp, mock_hist):
        from llm_relay.mcp.server import session_history
        result = json.loads(session_history("sid-1", turn_start=1, turn_end=2))
        assert "turns" in result

    @patch("llm_relay.proxy.db.get_conn", side_effect=Exception("DB error"))
    def test_error_handling(self, mock_conn):
        from llm_relay.mcp.server import session_history
        result = json.loads(session_history("sid-1"))
        assert "error" in result
