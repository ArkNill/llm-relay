"""Proxy content-encoding path (2026-10-22, R9-14).

Claude Code advertises ``accept-encoding: gzip, deflate, br, zstd``. The relay used
to forward that list and strip ``content-encoding`` from every response, trusting
httpx to decode; without the optional brotli/zstandard packages httpx passes a
``br`` body through untouched, so compressed JSON reached the client labelled as
plain text (a 529 shown as garbage, a non-streaming 200 ending the turn with a
JSON parse error) and the non-streaming path logged nothing. Upstream is an
httpx MockTransport whose reply each test chooses; the background DB writer is a
recorder, so nothing touches a database.
"""

from __future__ import annotations

import gzip
import json
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from llm_relay.proxy import proxy

MODEL = "claude-haiku-4-5-20251001"
CC_ACCEPT_ENCODING = "gzip, deflate, br, zstd"

JSON_REPLY = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": MODEL,
    "content": [{"type": "text", "text": "pong"}], "stop_reason": "end_turn",
    "usage": {"input_tokens": 10, "output_tokens": 1, "cache_creation_input_tokens": 0,
              "cache_read_input_tokens": 0},
}


class _Recorder:
    """Stand-in for the background DB writer: keeps submitted jobs, runs nothing."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, size_hint=0, **kwargs):
        self.jobs.append((fn.__name__, kwargs))

    def names(self):
        return [name for name, _ in self.jobs]

    def kwargs(self, name):
        found = [job for job in self.jobs if job[0] == name]
        assert len(found) == 1, "expected one {} job, got {}".format(name, len(found))
        return found[0][1]


class _Upstream:
    """MockTransport handler: records what the relay sent, answers with ``reply``."""

    def __init__(self):
        self.requests = []
        self.reply = httpx.Response(200, json=JSON_REPLY)

    def __call__(self, request):
        self.requests.append(request)
        return self.reply

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
    # Only the encoding path is under test: no history capture, no body rewrite.
    monkeypatch.setattr(proxy, "_HISTORY_ENABLED", False)
    monkeypatch.setattr(proxy, "_cache_fix_available", False)
    monkeypatch.setattr(proxy, "_effort_fix_available", False)
    monkeypatch.setattr(proxy, "_tokpress_available", False)
    monkeypatch.setattr(proxy, "_MAX_REWRITE_BYTES", 0)
    return SimpleNamespace(client=TestClient(proxy.app), upstream=upstream, writer=writer)


def _post(relay, stream=False, session="sess-r914"):
    payload = {"model": MODEL, "max_tokens": 1, "stream": stream, "messages": [{"role": "user", "content": "ping"}]}
    return relay.client.post(
        "/v1/messages",
        content=json.dumps(payload).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "accept-encoding": CC_ACCEPT_ENCODING,
            "x-claude-code-session-id": session,
        },
    )


# ── a. the client's accept-encoding does not reach upstream ──


def test_upstream_sees_only_the_encodings_httpx_can_decode(relay):
    resp = _post(relay)

    assert resp.status_code == 200
    advertised = relay.upstream.only.headers["accept-encoding"]
    tokens = {t.strip() for t in advertised.split(",")}
    assert "br" not in tokens and "zstd" not in tokens
    assert "gzip" in tokens  # httpx's own default, not the client's list
    assert resp.json()["content"] == JSON_REPLY["content"]


# ── b. an encoding httpx cannot decode is passed through: header and bytes ──

# "x-test" is unknown to every httpx, so the test does not depend on whether the
# optional brotli/zstandard packages happen to be installed.
UNKNOWN_BODY = bytes(range(256)) * 2  # neither UTF-8 nor JSON: stands in for brotli bytes


def test_non_stream_body_httpx_did_not_decode_keeps_its_encoding(relay):
    relay.upstream.reply = httpx.Response(
        200, headers={"content-type": "application/json", "content-encoding": "x-test"}, content=UNKNOWN_BODY,
    )
    resp = _post(relay)

    assert resp.status_code == 200
    assert resp.headers["content-encoding"] == "x-test"
    assert resp.content == UNKNOWN_BODY  # byte for byte, the client decodes it


def test_stream_body_httpx_did_not_decode_keeps_its_encoding(relay):
    relay.upstream.reply = httpx.Response(
        200, headers={"content-type": "text/event-stream", "content-encoding": "x-test"}, content=UNKNOWN_BODY,
    )
    resp = _post(relay, stream=True)

    assert resp.status_code == 200
    assert resp.headers["content-encoding"] == "x-test"
    assert resp.content == UNKNOWN_BODY
    logged = relay.writer.kwargs("log_request")
    assert (logged["status_code"], logged["is_stream"], logged["model"]) == (200, True, None)
    assert logged["raw_usage"]["stream_end"] == "completed"
    assert logged["raw_usage"]["content_encoding"] == "x-test"  # why the row has no model


# ── c. the decoding path is unchanged: gzip is decoded and its header dropped ──


def test_gzip_body_is_decoded_and_the_header_dropped(relay):
    relay.upstream.reply = httpx.Response(
        200, headers={"content-type": "application/json", "content-encoding": "gzip"},
        content=gzip.compress(json.dumps(JSON_REPLY).encode("utf-8")),
    )
    resp = _post(relay)

    assert resp.status_code == 200
    assert "content-encoding" not in resp.headers
    assert resp.json() == JSON_REPLY
    logged = relay.writer.kwargs("log_request")
    assert (logged["status_code"], logged["is_stream"], logged["model"]) == (200, False, MODEL)
    assert logged["raw_usage"] == JSON_REPLY["usage"]


# ── d. a non-streaming reply whose body is not JSON is still recorded ──


def test_non_stream_reply_without_a_json_body_is_recorded_by_status(relay):
    relay.upstream.reply = httpx.Response(
        529,
        headers={
            "content-type": "application/json",
            "content-encoding": "x-test",
            "anthropic-ratelimit-unified-status": "allowed",
        },
        content=UNKNOWN_BODY,
    )
    resp = _post(relay)

    assert resp.status_code == 529
    assert resp.content == UNKNOWN_BODY and resp.headers["content-encoding"] == "x-test"
    assert relay.writer.names() == ["log_request"]  # used to be skipped: no row for the 529
    logged = relay.writer.kwargs("log_request")
    assert (logged["status_code"], logged["is_stream"], logged["model"]) == (529, False, None)
    assert logged["raw_usage"] == {
        "stream_end": "unparsed_body",
        "content_type": "application/json",
        "content_encoding": "x-test",
        "response_bytes": len(UNKNOWN_BODY),
    }
    assert logged["ratelimit_headers"] == {"anthropic-ratelimit-unified-status": "allowed"}


def test_non_stream_plain_error_body_that_is_not_json_is_recorded_too(relay):
    relay.upstream.reply = httpx.Response(
        502, headers={"content-type": "text/html"}, content=b"<html>bad gateway</html>",
    )
    resp = _post(relay)

    assert resp.status_code == 502
    assert resp.content == b"<html>bad gateway</html>" and "content-encoding" not in resp.headers
    raw = relay.writer.kwargs("log_request")["raw_usage"]
    assert (raw["stream_end"], raw["content_type"], raw["content_encoding"]) == ("unparsed_body", "text/html", None)
