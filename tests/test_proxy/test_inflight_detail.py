"""Per-request detail behind /_health ``proxy_in_flight`` (2026-10-23, R9-15).

On 2026-10-22 the counter read 2 for six seconds while the journal showed one
unfinished request, and the restart gate (``proxy_in_flight == 0``) kept missing
its window. The counter itself is unchanged; /_health now lists the requests it
counts, which phase each is in (``open``, or ``ended`` once the relay has
finished the response and only the ASGI call is still to return), and logs one
WARNING per request that stays counted more than 2 s after it ended.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
import pytest
from starlette.testclient import TestClient
from test_proxy.test_proxy_encoding import JSON_REPLY, MODEL, _Recorder, _Upstream

from llm_relay.proxy import proxy


@pytest.fixture(autouse=True)
def _clean_detail():
    proxy._in_flight_detail.clear()
    yield
    proxy._in_flight_detail.clear()


def _scope(path="/v1/messages"):
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [(b"content-length", b"2"), (b"x-claude-code-session-id", b"sess-r915-abcdef")],
        "client": ("127.0.0.1", 50000),
    }


async def _receive():
    return {"type": "http.request", "body": b"{}", "more_body": False}


async def _send(message):
    return None


def test_detail_follows_a_request_from_open_to_ended_to_gone():
    async def scenario():
        opened, body_done, marked, release = (asyncio.Event() for _ in range(4))

        async def app(scope, receive, send):
            assert scope["state"]["relay_inflight_id"] in proxy._in_flight_detail
            opened.set()
            await body_done.wait()
            proxy._mark_in_flight_ended(scope)  # what the proxy does once the response is complete
            marked.set()
            await release.wait()

        task = asyncio.ensure_future(proxy._RequestLog(app)(_scope(), _receive, _send))
        await opened.wait()
        detail, ended = proxy._in_flight_snapshot()
        assert ended == 0 and len(detail) == 1
        assert (detail[0]["phase"], detail[0]["path"], detail[0]["session"]) == ("open", "/v1/messages", "sess-r91")
        assert detail[0]["ended_s"] is None and detail[0]["age_s"] >= 0

        body_done.set()
        await marked.wait()
        detail, ended = proxy._in_flight_snapshot()
        assert ended == 1 and detail[0]["phase"] == "ended" and detail[0]["ended_s"] is not None

        release.set()
        await task
        assert proxy._in_flight_detail == {}

    asyncio.run(scenario())
    assert proxy._in_flight["proxy"] == 0


def test_non_proxy_paths_are_not_listed():
    seen = {}

    async def app(scope, receive, send):
        seen["listed"] = bool(proxy._in_flight_detail)
        seen["state"] = scope.get("state", {}).get("relay_inflight_id")

    asyncio.run(proxy._RequestLog(app)(_scope("/api/v1/display"), _receive, _send))
    assert seen == {"listed": False, "state": None}


def test_lag_warning_is_logged_once_per_request(caplog):
    now = time.monotonic()

    def entry(i, started, session, ended=None):
        e = {"id": i, "started": now - started, "path": "/v1/messages", "session": session, "phase": "open"}
        if ended is not None:
            e.update(phase="ended", ended=now - ended)
        proxy._in_flight_detail[i] = e

    entry(7, 9.0, "sess-lag", ended=5.0)
    entry(8, 3.0, "sess-ok", ended=0.5)
    entry(9, 1.0, "s")

    with caplog.at_level(logging.WARNING, logger="llm-relay"):
        detail, ended = proxy._in_flight_snapshot()
        proxy._in_flight_snapshot()

    lines = [r.getMessage() for r in caplog.records if "proxy_in_flight lag" in r.getMessage()]
    assert len(lines) == 1
    assert lines[0].startswith("proxy_in_flight lag: id=7 path=/v1/messages session=sess-lag ended 5.")
    assert ended == 2
    assert [d["id"] for d in detail] == [7, 8, 9]  # oldest first


def test_health_reports_the_detail_fields():
    payload = TestClient(proxy.app).get("/_health").json()
    assert payload["proxy_in_flight_detail"] == [] and payload["proxy_in_flight_ended"] == 0


@pytest.fixture
def relay(monkeypatch):
    upstream = _Upstream()
    monkeypatch.setattr(proxy, "_db_writer", _Recorder())
    monkeypatch.setattr(
        proxy, "_client", httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url="http://upstream.test"),
    )
    monkeypatch.setattr(proxy, "_HISTORY_ENABLED", False)
    monkeypatch.setattr(proxy, "_cache_fix_available", False)
    monkeypatch.setattr(proxy, "_effort_fix_available", False)
    monkeypatch.setattr(proxy, "_tokpress_available", False)
    phases = []
    original = proxy._mark_in_flight_ended

    def spy(scope):
        original(scope)
        entry = proxy._in_flight_detail.get(scope["state"]["relay_inflight_id"])
        phases.append(entry["phase"] if entry else None)

    monkeypatch.setattr(proxy, "_mark_in_flight_ended", spy)
    return upstream, phases


@pytest.mark.parametrize("stream", [True, False])
def test_proxy_marks_the_request_ended_before_the_asgi_call_returns(relay, stream):
    upstream, phases = relay
    if stream:
        upstream.reply = httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"data: {}\n\n")
    payload = {"model": MODEL, "max_tokens": 1, "stream": stream, "messages": [{"role": "user", "content": "x"}]}

    resp = TestClient(proxy.app).post("/v1/messages", content=json.dumps(payload).encode("utf-8"))

    assert resp.status_code == 200
    if not stream:
        assert resp.json() == JSON_REPLY
    assert phases == ["ended"]
    assert proxy._in_flight_detail == {} and proxy._in_flight["proxy"] == 0
