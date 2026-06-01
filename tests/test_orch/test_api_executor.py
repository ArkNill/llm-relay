"""Tests for orch/api_executor.py — HTTP API delegation for providers without a CLI."""

from __future__ import annotations

import json
import os
import tempfile
import urllib.error
from unittest.mock import patch

import pytest

from llm_relay.orch.api_executor import (
    PROVIDERS,
    api_provider_status,
    execute_api,
    list_api_providers,
)
from llm_relay.orch.models import AuthMethod


# ── list / status ────────────────────────────────────────────────────────────


def test_list_api_providers_includes_grok():
    assert "grok" in list_api_providers()


def test_api_provider_status_unknown_returns_error():
    st = api_provider_status("does-not-exist")
    assert "error" in st


def test_api_provider_status_grok_shape(monkeypatch):
    # Force no key resolved so usable=False, but the shape should still be complete.
    monkeypatch.setenv("XAI_API_KEY_PATH", "/nonexistent/path")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    # also point legacy fallback at nothing
    with patch("os.path.expanduser", side_effect=lambda p: "/nonexistent/path/legacy" if "~/grok.key" in p else p):
        st = api_provider_status("grok")
    assert st["provider_id"] == "xai-grok"
    assert st["kind"] == "http-api"
    assert st["endpoint"].startswith("https://")
    assert st["default_model"] == "grok-4.3"
    assert st["auth_method"] == "api_key"
    assert "usable" in st


def test_api_provider_status_grok_with_keyfile(tmp_path, monkeypatch):
    key_path = tmp_path / "grok.key"
    key_path.write_text("xai-test-token-1234567890\n", encoding="utf-8")
    monkeypatch.setenv("XAI_API_KEY_PATH", str(key_path))
    st = api_provider_status("grok")
    assert st["api_key_available"] is True
    assert st["usable"] is True


# ── execute_api: error paths ─────────────────────────────────────────────────


def test_execute_api_unknown_provider():
    result = execute_api("nope", "hello")
    assert result.success is False
    assert result.cli_id == "unknown-api"
    assert result.auth_method == AuthMethod.NONE
    assert "Unknown API provider" in result.error
    assert result.exit_code == 2


def test_execute_api_no_key(monkeypatch, tmp_path):
    monkeypatch.setenv("XAI_API_KEY_PATH", str(tmp_path / "missing-key"))
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    # also redirect legacy ~/grok.key lookup
    with patch("os.path.expanduser", side_effect=lambda p: str(tmp_path / "no-legacy") if "~/grok.key" in p else p):
        result = execute_api("grok", "hello")
    assert result.success is False
    assert "No API key available" in result.error
    assert result.exit_code == 1


# ── execute_api: success path (urllib mocked) ────────────────────────────────


def _mock_response(payload: dict, status: int = 200):
    """Build a context-manager mock for urlopen()'s return value."""
    class _Resp:
        def __init__(self, body, code):
            self._body = body
            self.status = code

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return _Resp(json.dumps(payload).encode("utf-8"), status)


def test_execute_api_success(monkeypatch, tmp_path):
    key_path = tmp_path / "grok.key"
    key_path.write_text("xai-token\n", encoding="utf-8")
    monkeypatch.setenv("XAI_API_KEY_PATH", str(key_path))

    response_payload = {
        "id": "x123",
        "object": "chat.completion",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "Hi from Grok"}}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
    }

    with patch(
        "llm_relay.orch.api_executor.urllib.request.urlopen",
        return_value=_mock_response(response_payload),
    ):
        result = execute_api("grok", "hello there")

    assert result.success is True
    assert result.cli_id == "xai-grok"
    assert result.auth_method == AuthMethod.API_KEY
    assert result.output == "Hi from Grok"
    assert result.exit_code == 200
    assert result.model_used == "grok-4.3"


def test_execute_api_success_with_model_and_system(monkeypatch, tmp_path):
    key_path = tmp_path / "grok.key"
    key_path.write_text("xai-token", encoding="utf-8")
    monkeypatch.setenv("XAI_API_KEY_PATH", str(key_path))

    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["auth"] = req.get_header("Authorization")
        return _mock_response(
            {"choices": [{"message": {"content": "Reviewed."}}]}, 200
        )

    with patch("llm_relay.orch.api_executor.urllib.request.urlopen", side_effect=fake_urlopen):
        result = execute_api("grok", "review this", model="grok-4.20-0309-reasoning", system="You are a reviewer.")

    assert result.success is True
    assert captured["body"]["model"] == "grok-4.20-0309-reasoning"
    assert captured["body"]["messages"][0] == {"role": "system", "content": "You are a reviewer."}
    assert captured["body"]["messages"][1] == {"role": "user", "content": "review this"}
    assert captured["auth"] == "Bearer xai-token"


def test_execute_api_http_error(monkeypatch, tmp_path):
    key_path = tmp_path / "grok.key"
    key_path.write_text("xai-bad", encoding="utf-8")
    monkeypatch.setenv("XAI_API_KEY_PATH", str(key_path))

    # urllib.error.HTTPError expects (url, code, msg, hdrs, fp); we synthesize one.
    import io
    err = urllib.error.HTTPError(
        url="https://api.x.ai/v1/chat/completions",
        code=401,
        msg="Unauthorized",
        hdrs=None,
        fp=io.BytesIO(b'{"error":"invalid api key"}'),
    )
    with patch("llm_relay.orch.api_executor.urllib.request.urlopen", side_effect=err):
        result = execute_api("grok", "hello")

    assert result.success is False
    assert result.exit_code == 401
    assert "HTTP 401" in result.error
    assert "invalid api key" in result.error


def test_execute_api_transport_error(monkeypatch, tmp_path):
    key_path = tmp_path / "grok.key"
    key_path.write_text("xai-token", encoding="utf-8")
    monkeypatch.setenv("XAI_API_KEY_PATH", str(key_path))

    with patch(
        "llm_relay.orch.api_executor.urllib.request.urlopen",
        side_effect=urllib.error.URLError("network down"),
    ):
        result = execute_api("grok", "hello")

    assert result.success is False
    assert "Transport error" in result.error
    assert result.exit_code == 1


def test_execute_api_non_json_response(monkeypatch, tmp_path):
    key_path = tmp_path / "grok.key"
    key_path.write_text("xai-token", encoding="utf-8")
    monkeypatch.setenv("XAI_API_KEY_PATH", str(key_path))

    class _PlainResp:
        status = 200
        def read(self):
            return b"<html>upstream broken</html>"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    with patch("llm_relay.orch.api_executor.urllib.request.urlopen", return_value=_PlainResp()):
        result = execute_api("grok", "hello")

    assert result.success is False
    assert "non-JSON" in result.error


def test_execute_api_empty_choices(monkeypatch, tmp_path):
    key_path = tmp_path / "grok.key"
    key_path.write_text("xai-token", encoding="utf-8")
    monkeypatch.setenv("XAI_API_KEY_PATH", str(key_path))

    with patch(
        "llm_relay.orch.api_executor.urllib.request.urlopen",
        return_value=_mock_response({"choices": []}),
    ):
        result = execute_api("grok", "hello")

    assert result.success is False
    assert "no assistant text" in result.error


# ── key resolution: file vs env vs legacy ────────────────────────────────────


def test_xai_key_prefers_explicit_file_path(monkeypatch, tmp_path):
    key_path = tmp_path / "explicit.key"
    key_path.write_text("from-explicit-file", encoding="utf-8")
    monkeypatch.setenv("XAI_API_KEY_PATH", str(key_path))
    monkeypatch.setenv("XAI_API_KEY", "from-env-should-not-win")

    from llm_relay.orch.api_executor import _xai_key
    assert _xai_key() == "from-explicit-file"


def test_xai_key_falls_back_to_env(monkeypatch, tmp_path):
    monkeypatch.setenv("XAI_API_KEY_PATH", str(tmp_path / "no-file"))
    monkeypatch.setenv("XAI_API_KEY", "from-env-fallback")
    with patch("os.path.expanduser", side_effect=lambda p: str(tmp_path / "no-legacy") if "~/grok.key" in p else p):
        from llm_relay.orch.api_executor import _xai_key
        assert _xai_key() == "from-env-fallback"


def test_xai_key_returns_none_when_neither_set(monkeypatch, tmp_path):
    monkeypatch.setenv("XAI_API_KEY_PATH", str(tmp_path / "no-file"))
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    with patch("os.path.expanduser", side_effect=lambda p: str(tmp_path / "no-legacy") if "~/grok.key" in p else p):
        from llm_relay.orch.api_executor import _xai_key
        assert _xai_key() is None


def test_xai_key_strips_whitespace(monkeypatch, tmp_path):
    key_path = tmp_path / "g.key"
    key_path.write_text("  xai-with-padding  \n\n", encoding="utf-8")
    monkeypatch.setenv("XAI_API_KEY_PATH", str(key_path))

    from llm_relay.orch.api_executor import _xai_key
    assert _xai_key() == "xai-with-padding"
