"""Non-200 upstream replies to streaming requests (2026-10-23, R9-6).

A 429/529 answer to a streamed request was relayed in full and recorded as
``stream_end=completed``, so a relay-side wait could not be told apart from an
upstream error. The row now says ``stream_end=upstream_status`` with the status,
and the API's ``error.type`` is kept from an SSE ``error`` event, a plain JSON
error body (what the API sends before a stream starts) or a non-streaming error
reply. Upstream is an httpx MockTransport and the DB writer a recorder, as in
test_proxy_encoding.py.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient
from test_proxy.test_proxy_encoding import MODEL, _Recorder, _Upstream

from llm_relay.proxy import proxy

ERROR_EVENT = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
LABELLED_529 = ("upstream_status", 529, "overloaded_error")
SSE_ERROR = "event: error\ndata: {}\n\n".format(json.dumps(ERROR_EVENT))


def _sse(*events):
    return "".join("event: {}\ndata: {}\n\n".format(e["type"], json.dumps(e)) for e in events)


SSE_OK = _sse(
    {"type": "message_start", "message": {"model": MODEL, "usage": {"input_tokens": 10, "output_tokens": 1}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "pong"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
    {"type": "message_stop"},
)


@pytest.fixture
def relay(monkeypatch):
    upstream = _Upstream()
    writer = _Recorder()
    monkeypatch.setattr(proxy, "_db_writer", writer)
    monkeypatch.setattr(
        proxy, "_client", httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url="http://upstream.test"),
    )
    monkeypatch.setattr(proxy, "_HISTORY_ENABLED", False)
    monkeypatch.setattr(proxy, "_cache_fix_available", False)
    monkeypatch.setattr(proxy, "_effort_fix_available", False)
    monkeypatch.setattr(proxy, "_tokpress_available", False)
    monkeypatch.setattr(proxy, "_MAX_REWRITE_BYTES", 0)
    return SimpleNamespace(client=TestClient(proxy.app), upstream=upstream, writer=writer)


def _post(relay, stream=True):
    payload = {"model": MODEL, "max_tokens": 1, "stream": stream, "messages": [{"role": "user", "content": "ping"}]}
    return relay.client.post(
        "/v1/messages",
        content=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", "x-claude-code-session-id": "sess-r96"},
    )


def test_529_stream_with_an_sse_error_event_is_labelled(relay):
    relay.upstream.reply = httpx.Response(529, headers={"content-type": "text/event-stream"}, content=SSE_ERROR)

    resp = _post(relay)

    assert resp.status_code == 529
    assert resp.text == SSE_ERROR  # relayed unchanged
    logged = relay.writer.kwargs("log_request")
    assert (logged["status_code"], logged["is_stream"]) == (529, True)
    raw = logged["raw_usage"]
    assert (raw["stream_end"], raw["upstream_status"], raw["error_type"]) == LABELLED_529


def test_529_stream_with_a_plain_json_error_body_is_labelled(relay):
    # Before a stream starts the API answers with an ordinary JSON error body.
    relay.upstream.reply = httpx.Response(529, json=ERROR_EVENT)

    resp = _post(relay)

    assert resp.status_code == 529
    raw = relay.writer.kwargs("log_request")["raw_usage"]
    assert (raw["stream_end"], raw["upstream_status"], raw["error_type"]) == LABELLED_529


def test_200_stream_still_completes(relay):
    relay.upstream.reply = httpx.Response(200, headers={"content-type": "text/event-stream"}, content=SSE_OK)

    resp = _post(relay)

    assert resp.status_code == 200
    logged = relay.writer.kwargs("log_request")
    assert (logged["status_code"], logged["model"], logged["output_tokens"]) == (200, MODEL, 1)
    raw = logged["raw_usage"]
    assert raw["stream_end"] == "completed" and raw["message_stop"] is True
    assert "upstream_status" not in raw and "error_type" not in raw


def test_error_event_inside_a_200_stream_keeps_the_first_error_type(relay):
    second = {"type": "error", "error": {"type": "api_error", "message": "later"}}
    body = _sse({"type": "message_start", "message": {"model": MODEL, "usage": {"input_tokens": 1}}}) + SSE_ERROR
    body += _sse(second)
    relay.upstream.reply = httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    _post(relay)

    raw = relay.writer.kwargs("log_request")["raw_usage"]
    assert (raw["stream_end"], raw["error_type"]) == ("completed", "overloaded_error")
    assert "upstream_status" not in raw


def test_non_stream_error_reply_records_its_error_type(relay):
    relay.upstream.reply = httpx.Response(529, json=ERROR_EVENT)

    resp = _post(relay, stream=False)

    assert resp.status_code == 529 and resp.json() == ERROR_EVENT
    logged = relay.writer.kwargs("log_request")
    assert (logged["status_code"], logged["is_stream"]) == (529, False)
    assert logged["raw_usage"] == {"error_type": "overloaded_error"}
