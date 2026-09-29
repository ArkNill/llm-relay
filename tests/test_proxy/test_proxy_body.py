"""Proxy request-body path (2026-09-29).

The body is parsed once; the stream path reuses that parse, history capture
receives a copy with inline base64 replaced by size markers, and the bytes are
not cached on the Starlette request. Upstream is an httpx MockTransport and the
background DB writer is replaced by a recorder, so nothing touches a database.
"""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from llm_relay.proxy import proxy
from llm_relay.proxy.cc_cache_fix import normalize_request as cache_normalize
from llm_relay.proxy.cc_effort_fix import normalize_request as effort_normalize
from llm_relay.proxy.db import log_cache_diagnostic
from llm_relay.proxy.history import history_view

MODEL = "claude-haiku-4-5-20251001"
IMAGE_B64 = base64.b64encode(b"\x89PNG" + bytes(range(256)) * 64).decode("ascii")


def _sse(events):
    return b"".join(
        "event: {}\ndata: {}\n\n".format(name, json.dumps(payload)).encode("utf-8") for name, payload in events
    )


SSE_REPLY = _sse([
    ("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": MODEL, "content": [],
        "usage": {"input_tokens": 10, "output_tokens": 0, "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0,
                             "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "text_delta", "text": "pong"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                       "usage": {"output_tokens": 1}}),
    ("message_stop", {"type": "message_stop"}),
])

JSON_REPLY = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": MODEL,
    "content": [{"type": "text", "text": "pong"}], "stop_reason": "end_turn",
    "usage": {"input_tokens": 10, "output_tokens": 1, "cache_creation_input_tokens": 0,
              "cache_read_input_tokens": 0},
}


def _request(stream=True):
    """A Claude Code-shaped request that both the cache fix and the effort fix rewrite."""
    return {
        "model": MODEL,
        "max_tokens": 64,
        "temperature": 0.5,
        "stream": stream,
        "thinking": {"type": "disabled"},
        "output_config": {"effort": "max"},
        "system": [
            {"type": "text", "text": "x-anthropic-billing-header: cc_version=2.1.283.abc; cc_entrypoint=cli"},
            {"type": "text", "text": "You are a test.", "cache_control": {"type": "ephemeral"}},
        ],
        "tools": [
            {"name": "Write", "description": "w", "input_schema": {"type": "object"}},
            {"name": "Bash", "description": "b", "input_schema": {"type": "object"}},
        ],
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "Describe this image."},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": IMAGE_B64}},
                {"type": "text", "text": "One word.", "cache_control": {"type": "ephemeral"}},
            ]},
        ],
    }


class _Recorder:
    """Stand-in for the background DB writer: keeps submitted jobs, runs nothing."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, **kwargs):
        self.jobs.append((fn.__name__, kwargs))

    def names(self):
        return [name for name, _ in self.jobs]

    def kwargs(self, name):
        found = [kw for job, kw in self.jobs if job == name]
        assert len(found) == 1, "expected one {} job, got {}".format(name, len(found))
        return found[0]


class _Upstream:
    """MockTransport handler that keeps every request the relay sent upstream."""

    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=SSE_REPLY)
        return httpx.Response(200, json=JSON_REPLY)

    @property
    def only(self):
        assert len(self.requests) == 1
        return self.requests[0]


@pytest.fixture
def relay(monkeypatch):
    upstream = _Upstream()
    writer = _Recorder()
    monkeypatch.setattr(proxy, "_db_writer", writer)
    monkeypatch.setattr(
        proxy, "_client", httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url="http://upstream.test"),
    )
    monkeypatch.setattr(proxy, "_HISTORY_ENABLED", True)
    monkeypatch.setattr(proxy, "_cache_fix_available", True)
    monkeypatch.setattr(proxy, "_normalize_cache", cache_normalize, raising=False)
    monkeypatch.setattr(proxy, "log_cache_diagnostic", log_cache_diagnostic, raising=False)
    monkeypatch.setattr(proxy, "_effort_fix_available", True)
    monkeypatch.setattr(proxy, "_normalize_effort", effort_normalize, raising=False)
    return SimpleNamespace(client=TestClient(proxy.app), upstream=upstream, writer=writer)


def _post(relay, payload, session="sess-r92"):
    body = json.dumps(payload).encode("utf-8")
    resp = relay.client.post(
        "/v1/messages", content=body,
        headers={"content-type": "application/json", "x-claude-code-session-id": session},
    )
    assert resp.status_code == 200
    return body


def _stripped(messages):
    source = messages[0]["content"][1]["source"]
    return source["data"] == "" and source["omitted_bytes"] == len(IMAGE_B64)


def test_stream_parses_the_body_once_and_rewrites_it(relay, monkeypatch):
    payload = _request(stream=True)
    body_len = len(json.dumps(payload).encode("utf-8"))
    body_parses = []

    def counting_loads(s, *args, **kwargs):
        if isinstance(s, (bytes, bytearray)) and len(s) == body_len:
            body_parses.append(1)
        return json.loads(s, *args, **kwargs)

    monkeypatch.setattr(
        proxy, "json", SimpleNamespace(loads=counting_loads, dumps=json.dumps, JSONDecodeError=json.JSONDecodeError),
    )
    _post(relay, payload)

    assert len(body_parses) == 1
    sent = json.loads(relay.upstream.only.content)
    assert sent["messages"][0]["content"][1]["source"]["data"] == IMAGE_B64  # upstream still gets the image
    assert sent["system"][1]["cache_control"]["ttl"] == "1h"  # cache fix applied
    assert [t["name"] for t in sent["tools"]] == ["Bash", "Write"]
    assert sent["output_config"]["effort"] == "high"  # effort fix applied


def test_stream_history_gets_the_stripped_view_with_model_params(relay):
    payload = _request(stream=True)
    body = _post(relay, payload)

    assert relay.writer.names() == ["log_cache_diagnostic", "log_request", "capture_turn_streamed"]
    turn = relay.writer.kwargs("capture_turn_streamed")
    view = turn["req_json"]
    assert _stripped(view["messages"])
    assert IMAGE_B64 not in json.dumps(view)
    assert (view["model"], view["max_tokens"], view["temperature"]) == (MODEL, 64, 0.5)
    assert turn["accumulated_content"] == [{"type": "text", "text": "pong"}]
    assert turn["request_size"] == len(body)
    logged = relay.writer.kwargs("log_request")
    assert (logged["status_code"], logged["is_stream"], logged["request_body_bytes"]) == (200, True, len(body))
    assert logged["raw_usage"]["stream_end"] == "completed"


def test_non_stream_history_gets_the_stripped_view(relay):
    _post(relay, _request(stream=False))

    assert relay.writer.names() == ["log_cache_diagnostic", "log_request", "capture_turn"]
    turn = relay.writer.kwargs("capture_turn")
    assert _stripped(turn["req_json"]["messages"])
    assert turn["req_json"]["model"] == MODEL
    assert turn["resp_json"]["content"] == JSON_REPLY["content"]
    sent = json.loads(relay.upstream.only.content)
    assert sent["messages"][0]["content"][1]["source"]["data"] == IMAGE_B64
    assert relay.writer.kwargs("log_request")["raw_usage"] == JSON_REPLY["usage"]


def test_body_is_read_without_the_request_level_cache(relay, monkeypatch):
    def cached_body(self):
        raise AssertionError("request.body() would keep a copy of the bytes on the request")

    monkeypatch.setattr(proxy.Request, "body", cached_body)
    _post(relay, _request(stream=True))
    assert _stripped(relay.writer.kwargs("capture_turn_streamed")["req_json"]["messages"])


def test_read_body_joins_chunks_and_keeps_a_single_chunk_as_is():
    class _Req:
        def __init__(self, chunks):
            self._chunks = chunks

        async def stream(self):
            for chunk in self._chunks:
                yield chunk

    single = b"single chunk"
    assert asyncio.run(proxy._read_body(_Req([single, b""]))) is single
    assert asyncio.run(proxy._read_body(_Req([b"ab", b"", b"cd", b""]))) == b"abcd"
    assert asyncio.run(proxy._read_body(_Req([b""]))) == b""


def test_history_view_strips_without_touching_the_original():
    req = _request()
    view = history_view(req)

    assert view is not req
    assert _stripped(view["messages"])
    assert req["messages"][0]["content"][1]["source"]["data"] == IMAGE_B64
    assert view["model"] == MODEL and view["tools"] is req["tools"]
    again = history_view(view)
    assert again["messages"] is view["messages"]  # already stripped: nothing copied
