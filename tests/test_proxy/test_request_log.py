"""Request start lines and in-flight counters (2026-09-29, proxy._RequestLog)."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from starlette.testclient import TestClient

from llm_relay.proxy import proxy

ACCESS = "llm-relay.access"


def _lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == ACCESS]


def test_a_line_is_logged_when_a_request_starts(caplog):
    with caplog.at_level(logging.INFO, logger=ACCESS):
        resp = TestClient(proxy.app).get("/nope")

    assert resp.status_code == 404  # catch-all proxy: only /v1/ is forwarded
    assert _lines(caplog) == ["request start GET /nope -B client=testclient session=-"]


def test_proxy_line_carries_body_size_and_session(caplog, monkeypatch):
    def upstream(request):
        return httpx.Response(200, json={
            "id": "m", "type": "message", "role": "assistant", "model": "x", "content": [],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    monkeypatch.setattr(
        proxy, "_client", httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url="http://upstream.test"),
    )
    monkeypatch.setattr(proxy, "_db_writer", type("_NoWriter", (), {"submit": lambda self, fn, **kw: None})())
    body = json.dumps({"model": "x", "messages": [{"role": "user", "content": "hi"}]}).encode("utf-8")

    with caplog.at_level(logging.INFO, logger=ACCESS):
        resp = TestClient(proxy.app).post(
            "/v1/messages", content=body,
            headers={"content-type": "application/json", "x-claude-code-session-id": "abcdef12-3456-7890"},
        )

    assert resp.status_code == 200
    expected = "request start POST /v1/messages {}B client=testclient session=abcdef12".format(len(body))
    assert _lines(caplog) == [expected]


def test_health_is_not_logged_and_reports_counters_and_memory(caplog):
    with caplog.at_level(logging.INFO, logger=ACCESS):
        payload = TestClient(proxy.app).get("/_health").json()

    assert _lines(caplog) == []
    assert (payload["in_flight"], payload["proxy_in_flight"]) == (1, 0)  # the health call itself
    assert payload["memory_rss_mb"] > 0
    assert payload["memory_cgroup_mb"] is None or payload["memory_cgroup_mb"] >= 0
    assert proxy._in_flight == {"total": 0, "proxy": 0}


def test_env_off_disables_the_line(caplog, monkeypatch):
    monkeypatch.setattr(proxy, "_LOG_REQUEST_START", False)
    with caplog.at_level(logging.INFO, logger=ACCESS):
        TestClient(proxy.app).get("/nope")
    assert _lines(caplog) == []


def test_counters_cover_the_whole_request_and_are_released_on_failure():
    seen = []

    async def inner(scope, receive, send):
        seen.append(dict(proxy._in_flight))
        if scope.get("path") == "/v1/boom":
            raise RuntimeError("boom")

    middleware = proxy._RequestLog(inner)

    def http(path):
        return {"type": "http", "method": "POST", "path": path, "headers": [], "client": ("127.0.0.1", 1)}

    asyncio.run(middleware(http("/v1/messages"), None, None))
    asyncio.run(middleware(http("/api/v1/display"), None, None))
    with pytest.raises(RuntimeError):
        asyncio.run(middleware(http("/v1/boom"), None, None))
    asyncio.run(middleware({"type": "lifespan"}, None, None))  # passed through untouched

    assert seen == [
        {"total": 1, "proxy": 1},
        {"total": 1, "proxy": 0},
        {"total": 1, "proxy": 1},
        {"total": 0, "proxy": 0},
    ]
    assert proxy._in_flight == {"total": 0, "proxy": 0}
