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
