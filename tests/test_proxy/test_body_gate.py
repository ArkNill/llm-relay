"""Request body gate and one-shot upload (2026-10-22).

Large request bodies are admitted against a byte budget before they are read
(bodygate.BodyGate): over budget they wait in arrival order, and they give
their bytes back as soon as upstream response headers arrive rather than when a
streamed response ends. The body goes upstream from a one-shot generator with
an explicit content-length. Upstream is an httpx MockTransport and the DB writer
a recorder, as in test_proxy_body.py; tests that need requests in flight at the
same time drive the app through httpx.ASGITransport on one event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import ClientDisconnect
from starlette.testclient import TestClient
from test_proxy.test_proxy_body import JSON_REPLY, SSE_REPLY, _Recorder, _request

from llm_relay.proxy import proxy
from llm_relay.proxy.bodygate import (
    FALLBACK_MAX_BYTES,
    BodyGate,
    BodyGateTimeout,
    cgroup_memory_max,
    default_max_bytes,
    env_number,
)
from llm_relay.proxy.cc_cache_fix import normalize_request as cache_normalize
from llm_relay.proxy.cc_effort_fix import normalize_request as effort_normalize
from llm_relay.proxy.db import log_cache_diagnostic

GATE_LOG = "llm-relay.bodygate"
SIZE = len(json.dumps(_request(stream=True)).encode("utf-8"))
GATE_MAX = SIZE + SIZE // 2  # one test body fits, two do not


def _immediate(coro):
    """Run a coroutine that must finish without suspending and return its result."""
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    coro.close()
    raise AssertionError("the coroutine suspended")


async def _settle():
    """Let every ready task run up to its next wait."""
    for _ in range(10):
        await asyncio.sleep(0)


async def _until(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "condition not reached"
        await asyncio.sleep(0.001)


def _gate_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == GATE_LOG]


# ── BodyGate ──


def test_gate_off_and_small_bodies_pass_uncounted_and_silent(caplog):
    off = BodyGate(max_bytes=0, min_bytes=0)
    gate = BodyGate(max_bytes=100, min_bytes=10)
    count_all = BodyGate(max_bytes=100, min_bytes=0)
    with caplog.at_level(logging.INFO, logger=GATE_LOG):
        huge = _immediate(off.acquire(10 ** 9, label="off"))
        hog = _immediate(gate.acquire(100, label="hog"))  # the budget is now full
        small = _immediate(gate.acquire(9, label="small"))  # below min_bytes: not delayed all the same
        lone = _immediate(count_all.acquire(500, label="lone"))  # over the budget, alone
        bodyless = _immediate(count_all.acquire(0, label="bodyless"))  # no content-length: nothing to count

    assert (count_all.snapshot()["inflight_bytes"], count_all.snapshot()["inflight_requests"]) == (500, 1)
    assert bodyless.waited_ms == 0
    lone.release()
    assert off.snapshot()["enabled"] is False
    assert (off.snapshot()["inflight_bytes"], off.snapshot()["inflight_requests"]) == (0, 0)
    assert (gate.snapshot()["inflight_bytes"], gate.snapshot()["inflight_requests"]) == (100, 1)
    assert huge.waited_ms == small.waited_ms == 0
    huge.release()
    small.release()
    assert off.snapshot()["inflight_bytes"] == 0
    assert gate.snapshot()["inflight_bytes"] == 100  # releasing an uncounted token takes nothing away
    hog.release()
    assert _gate_lines(caplog) == []


def test_bodies_within_the_budget_are_admitted_together():
    gate = BodyGate(max_bytes=100, min_bytes=1)
    first = _immediate(gate.acquire(40))
    second = _immediate(gate.acquire(60))

    snap = gate.snapshot()
    assert (snap["inflight_bytes"], snap["inflight_requests"], snap["waiting"], snap["waited_total"]) == (100, 2, 0, 0)
    assert first.waited_ms == second.waited_ms == 0


def test_over_the_budget_a_body_waits_for_a_release_in_arrival_order(caplog):
    async def scenario():
        gate = BodyGate(max_bytes=100, min_bytes=1, timeout_s=5)
        first = await gate.acquire(60, label="first")
        admitted = []

        async def wait(n, name):
            token = await gate.acquire(n, label=name)
            admitted.append(name)
            return token

        second = asyncio.ensure_future(wait(60, "second"))
        await _settle()
        third = asyncio.ensure_future(wait(50, "third"))
        await _settle()
        queued = gate.snapshot()
        first.release()  # second fits now; third (60 + 50) still does not
        await _settle()
        after_first = (list(admitted), gate.snapshot())
        (await second).release()
        third_token = await third
        return queued, after_first, admitted, third_token, gate

    with caplog.at_level(logging.INFO, logger=GATE_LOG):
        queued, (admitted_then, snap_then), admitted, third, gate = asyncio.run(scenario())

    assert (queued["inflight_bytes"], queued["waiting"], queued["waited_total"]) == (60, 2, 2)
    assert admitted_then == ["second"]
    assert (snap_then["inflight_bytes"], snap_then["waiting"]) == (60, 1)
    assert admitted == ["second", "third"]
    assert third.waited_ms >= 1
    assert (gate.snapshot()["inflight_bytes"], gate.snapshot()["waiting"]) == (50, 0)
    lines = _gate_lines(caplog)
    assert lines[0] == "body gate: waiting for 60 B (inflight 60 B / max 100 B, 1 waiting) -- second"
    assert lines[1] == "body gate: waiting for 50 B (inflight 60 B / max 100 B, 2 waiting) -- third"
    assert lines[2].startswith("body gate: admitted after ") and lines[2].endswith("-- second")
    assert lines[3].startswith("body gate: admitted after ") and lines[3].endswith("-- third")


def test_a_newcomer_queues_behind_waiters_even_when_it_would_fit():
    async def scenario():
        gate = BodyGate(max_bytes=100, min_bytes=1, timeout_s=5)
        first = await gate.acquire(60)
        admitted = []

        async def wait(n, name):
            token = await gate.acquire(n)
            admitted.append(name)
            return token

        second = asyncio.ensure_future(wait(60, "second"))
        await _settle()
        third = asyncio.ensure_future(wait(30, "third"))  # 60 + 30 fits, but second came first
        await _settle()
        jumped = third.done()
        first.release()
        await asyncio.gather(second, third)
        return jumped, admitted, gate.snapshot()

    jumped, admitted, snap = asyncio.run(scenario())
    assert jumped is False
    assert admitted == ["second", "third"]
    assert (snap["inflight_bytes"], snap["inflight_requests"]) == (90, 2)


def test_a_body_alone_is_admitted_even_over_the_budget():
    async def scenario():
        gate = BodyGate(max_bytes=100, min_bytes=1, timeout_s=5)
        huge = _immediate(gate.acquire(500))
        alone = gate.snapshot()
        other = asyncio.ensure_future(gate.acquire(10))  # anything else waits for it
        await _settle()
        blocked = other.done()
        huge.release()
        token = await other
        return alone, blocked, token, gate.snapshot()

    alone, blocked, token, snap = asyncio.run(scenario())
    assert (alone["inflight_bytes"], alone["inflight_requests"]) == (500, 1)
    assert blocked is False
    assert token.waited_ms >= 1
    assert snap["inflight_bytes"] == 10


def test_a_wait_past_the_timeout_raises_and_leaves_the_queue():
    async def scenario():
        gate = BodyGate(max_bytes=100, min_bytes=1, timeout_s=0.05)
        holder = await gate.acquire(100)
        with pytest.raises(BodyGateTimeout) as info:
            await gate.acquire(30, label="late")
        return gate, holder, info.value

    gate, holder, exc = asyncio.run(scenario())
    assert (exc.n, exc.max_bytes) == (30, 100)
    assert exc.waited_s >= 0.04
    snap = gate.snapshot()
    assert (snap["waiting"], snap["waited_total"], snap["timeouts"], snap["inflight_bytes"]) == (0, 1, 1, 100)
    holder.release()
    assert gate.snapshot()["inflight_bytes"] == 0


def test_release_is_idempotent():
    gate = BodyGate(max_bytes=100, min_bytes=1)
    first = _immediate(gate.acquire(40))
    second = _immediate(gate.acquire(30))

    first.release()
    first.release()
    assert (gate.snapshot()["inflight_bytes"], gate.snapshot()["inflight_requests"]) == (30, 1)
    second.release()
    second.release()
    assert (gate.snapshot()["inflight_bytes"], gate.snapshot()["inflight_requests"]) == (0, 0)


def test_a_cancelled_waiter_leaves_the_queue_and_unblocks_the_next():
    async def scenario():
        gate = BodyGate(max_bytes=100, min_bytes=1, timeout_s=5)
        await gate.acquire(60)
        big = asyncio.ensure_future(gate.acquire(60))  # the head: does not fit
        small = asyncio.ensure_future(gate.acquire(30))  # would fit, kept behind big
        await _settle()
        queued = gate.snapshot()["waiting"]
        big.cancel()
        await _settle()
        return queued, big, small, gate.snapshot()

    queued, big, small, snap = asyncio.run(scenario())
    assert queued == 2
    assert big.cancelled()
    assert small.done() and small.result().waited_ms >= 1
    assert (snap["waiting"], snap["inflight_bytes"], snap["inflight_requests"]) == (0, 90, 2)


def test_a_waiter_cancelled_right_after_admission_gives_its_bytes_back():
    async def scenario():
        gate = BodyGate(max_bytes=100, min_bytes=1, timeout_s=5)
        first = await gate.acquire(60)
        second = asyncio.ensure_future(gate.acquire(60))
        await _settle()
        first.release()  # admits second: its bytes count before the task resumes
        counted = gate.snapshot()["inflight_bytes"]
        second.cancel()  # ...and it is cancelled before it gets to run
        with pytest.raises(asyncio.CancelledError):
            await second
        return counted, gate.snapshot()

    counted, snap = asyncio.run(scenario())
    assert counted == 60
    assert (snap["inflight_bytes"], snap["inflight_requests"], snap["waiting"]) == (0, 0, 0)


# ── default budget ──


def _budget(tmp_path, memory_max, env=None):
    proc = tmp_path / "cgroup"
    proc.write_text("0::/user.slice/llm-relay-api.service\n")
    if memory_max is not None:
        unit = tmp_path / "fs" / "user.slice" / "llm-relay-api.service"
        unit.mkdir(parents=True)
        (unit / "memory.max").write_text(memory_max + "\n")
    return default_max_bytes(env, cgroup_memory_max(str(proc), str(tmp_path / "fs")))


@pytest.mark.parametrize("memory_max, env, expected", [
    ("4294967296", None, 1024 ** 3),  # MemoryMax=4G -> a quarter
    ("max", None, FALLBACK_MAX_BYTES),  # no limit
    (None, None, FALLBACK_MAX_BYTES),  # no memory.max to read
    ("4294967296", "", 1024 ** 3),  # empty = unset
    ("4294967296", "1000", 1000),  # the env value wins
    ("max", "0", 0),  # 0 = gate off
    ("4294967296", "1G", 1024 ** 3),  # malformed env falls through to the derived budget
])
def test_default_budget_comes_from_env_then_the_cgroup_limit(tmp_path, memory_max, env, expected):
    assert _budget(tmp_path, memory_max, env) == expected


def test_malformed_knobs_warn_and_fall_back_instead_of_raising(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=GATE_LOG)
    monkeypatch.setenv("LLM_RELAY_BODY_GATE_MIN_BYTES", "4M")
    monkeypatch.setenv("LLM_RELAY_BODY_GATE_TIMEOUT_S", "two minutes")
    monkeypatch.setenv("LLM_RELAY_BODY_GATE_EMPTY", "   ")

    assert env_number("LLM_RELAY_BODY_GATE_MIN_BYTES", 7) == 7
    assert env_number("LLM_RELAY_BODY_GATE_TIMEOUT_S", 120.0, float) == 120.0
    assert env_number("LLM_RELAY_BODY_GATE_EMPTY", 5) == 5  # blank = unset, no warning
    assert env_number("LLM_RELAY_BODY_GATE_UNSET", 9) == 9
    assert _budget(tmp_path, "4294967296", "1G") == 1024 ** 3
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 3
    assert any("LLM_RELAY_BODY_GATE_MIN_BYTES='4M'" in w for w in warnings)
    assert any("LLM_RELAY_MAX_INFLIGHT_BODY_BYTES='1G'" in w for w in warnings)


def test_well_formed_knobs_parse_with_the_requested_type(monkeypatch):
    monkeypatch.setenv("LLM_RELAY_BODY_GATE_MIN_BYTES", " 1048576 ")
    monkeypatch.setenv("LLM_RELAY_BODY_GATE_TIMEOUT_S", "0.5")
    assert env_number("LLM_RELAY_BODY_GATE_MIN_BYTES", 7) == 1048576
    assert env_number("LLM_RELAY_BODY_GATE_TIMEOUT_S", 120.0, float) == 0.5


def test_cgroup_memory_max_without_a_proc_file_is_none(tmp_path):
    assert cgroup_memory_max(str(tmp_path / "missing"), str(tmp_path)) is None
    assert FALLBACK_MAX_BYTES == 256 * 1024 * 1024


# ── through the proxy ──


def _headers(session):
    return {"content-type": "application/json", "x-claude-code-session-id": session}


def _reply(request):
    if request.content and json.loads(request.content).get("stream"):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=SSE_REPLY)
    return httpx.Response(200, json=JSON_REPLY)


def _rows(writer):
    """log_request calls by session id."""
    return {kw["session_id"]: kw for name, kw, _ in writer.jobs if name == "log_request"}


def _relay_client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://relay.test")


async def _apost(client, payload, session):
    return await client.post("/v1/messages", content=json.dumps(payload).encode("utf-8"), headers=_headers(session))


@pytest.fixture
def relay(monkeypatch):
    """The relay with a small gate, a recording DB writer and a scriptable upstream.

    ``respond`` (async, request -> httpx.Response) replaces the default reply;
    ``seen`` collects what the scripted parts observe.
    """
    state = SimpleNamespace(
        writer=_Recorder(), sent=[], respond=None, seen=[], client=TestClient(proxy.app),
        gate=BodyGate(max_bytes=GATE_MAX, min_bytes=1024, timeout_s=5),
    )

    async def upstream(request):
        state.sent.append(request)
        if state.respond is None:
            return _reply(request)
        return await state.respond(request)

    monkeypatch.setattr(proxy, "_body_gate", state.gate)
    monkeypatch.setattr(proxy, "_db_writer", state.writer)
    monkeypatch.setattr(
        proxy, "_client", httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url="http://upstream.test"),
    )
    monkeypatch.setattr(proxy, "_HISTORY_ENABLED", True)
    monkeypatch.setattr(proxy, "_cache_fix_available", True)
    monkeypatch.setattr(proxy, "_normalize_cache", cache_normalize, raising=False)
    monkeypatch.setattr(proxy, "log_cache_diagnostic", log_cache_diagnostic, raising=False)
    monkeypatch.setattr(proxy, "_effort_fix_available", True)
    monkeypatch.setattr(proxy, "_normalize_effort", effort_normalize, raising=False)
    monkeypatch.setattr(proxy, "_MAX_REWRITE_BYTES", 0)
    return state


def _post(relay, payload, session="sess-gate"):
    body = json.dumps(payload).encode("utf-8")
    return body, relay.client.post("/v1/messages", content=body, headers=_headers(session))


def _idle(gate):
    snap = gate.snapshot()
    return (snap["inflight_bytes"], snap["inflight_requests"], snap["waiting"]) == (0, 0, 0)


def test_a_stream_gives_its_bytes_back_once_upstream_headers_arrive(relay):
    seen = {}

    async def scenario():
        streaming = asyncio.Event()
        finish = asyncio.Event()

        class _HeldBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                seen["streaming"] = relay.gate.snapshot()
                streaming.set()
                await finish.wait()
                yield SSE_REPLY

        async def respond(request):
            if request.headers["x-claude-code-session-id"] != "held":
                return _reply(request)
            seen["before_headers"] = relay.gate.snapshot()
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_HeldBody())

        relay.respond = respond
        async with _relay_client() as client:
            held = asyncio.ensure_future(_apost(client, _request(stream=True), "held"))
            await asyncio.wait_for(streaming.wait(), 5)
            seen["held"] = relay.gate.snapshot()
            # Together the two bodies are over the budget, yet the second one
            # gets in while the first response is still streaming.
            other = await asyncio.wait_for(_apost(client, _request(stream=False), "other"), 5)
            finish.set()
            return await asyncio.wait_for(held, 5), other

    held, other = asyncio.run(scenario())

    assert (seen["before_headers"]["inflight_bytes"], seen["before_headers"]["inflight_requests"]) == (SIZE, 1)
    for moment in ("streaming", "held"):
        assert (seen[moment]["inflight_bytes"], seen[moment]["inflight_requests"]) == (0, 0)
    assert (held.status_code, held.content) == (200, SSE_REPLY)
    assert other.status_code == 200
    assert relay.gate.snapshot()["waited_total"] == 0
    rows = _rows(relay.writer)
    assert rows["held"]["raw_usage"]["stream_end"] == "completed"
    assert "body_gate_wait_ms" not in rows["held"]["raw_usage"]
    assert "body_gate_wait_ms" not in rows["other"]["raw_usage"]
    assert _idle(relay.gate)


@pytest.mark.parametrize("stream", [True, False])
def test_the_gate_is_released_when_upstream_cannot_be_reached(relay, stream):
    async def respond(request):
        relay.seen.append(relay.gate.snapshot())
        raise httpx.ConnectError("connection refused")

    relay.respond = respond
    body, resp = _post(relay, _request(stream=stream))

    assert resp.status_code == 502
    assert relay.seen[0]["inflight_bytes"] == len(body)  # counted while the request was out
    assert _idle(relay.gate)
    row = relay.writer.kwargs("log_request")
    assert (row["status_code"], row["raw_usage"]["stream_end"]) == (502, "upstream_error")


@pytest.mark.parametrize("stream", [True, False])
def test_the_gate_is_released_when_the_client_leaves_before_headers(relay, monkeypatch, stream):
    async def respond(request):
        relay.seen.append(relay.gate.snapshot())
        await asyncio.sleep(30)  # cancelled when the relay sees the client gone

    async def disconnected(self):
        return True

    relay.respond = respond
    monkeypatch.setattr(proxy, "_DISCONNECT_POLL_S", 0.01)
    monkeypatch.setattr(proxy.Request, "is_disconnected", disconnected)
    body, resp = _post(relay, _request(stream=stream))

    assert resp.status_code == 499
    assert relay.seen[0]["inflight_bytes"] == len(body)
    assert _idle(relay.gate)
    assert relay.writer.kwargs("log_request")["raw_usage"] == {"stream_end": "client_disconnect_before_headers"}


def test_the_gate_is_released_when_the_request_is_cancelled(relay):
    async def scenario():
        reached = asyncio.Event()

        async def respond(request):
            reached.set()
            await asyncio.sleep(30)

        relay.respond = respond
        async with _relay_client() as client:
            request = asyncio.ensure_future(_apost(client, _request(stream=True), "cancelled"))
            await asyncio.wait_for(reached.wait(), 5)
            counted = relay.gate.snapshot()["inflight_bytes"]
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        return counted

    assert asyncio.run(scenario()) == SIZE
    assert _idle(relay.gate)
    assert proxy._in_flight == {"total": 0, "proxy": 0}


def test_the_gate_is_released_when_reading_the_body_fails(relay, monkeypatch):
    async def broken(request):
        relay.seen.append(relay.gate.snapshot())
        raise ClientDisconnect()

    monkeypatch.setattr(proxy, "_read_body", broken)
    body = json.dumps(_request(stream=True)).encode("utf-8")
    resp = TestClient(proxy.app, raise_server_exceptions=False).post(
        "/v1/messages", content=body, headers=_headers("broken"),
    )

    assert resp.status_code == 500
    assert relay.seen[0]["inflight_bytes"] == len(body)
    assert _idle(relay.gate)


@pytest.mark.parametrize("rewrite", [True, False])
@pytest.mark.parametrize("stream", [True, False])
def test_the_upload_is_byte_exact_with_content_length_and_not_chunked(relay, monkeypatch, stream, rewrite):
    handed = []
    one_shot = proxy._one_shot

    def spy(data):
        handed.append(data)
        return one_shot(data)

    monkeypatch.setattr(proxy, "_one_shot", spy)
    monkeypatch.setattr(proxy, "_MAX_REWRITE_BYTES", 0 if rewrite else 1024)
    body, resp = _post(relay, _request(stream=stream))

    assert resp.status_code == 200
    assert len(handed) == 1 and len(relay.sent) == 1
    sent = relay.sent[0]
    assert sent.content == handed[0]
    assert sent.headers["content-length"] == str(len(sent.content))
    assert "transfer-encoding" not in sent.headers
    if rewrite:
        assert sent.content != body
        assert json.loads(sent.content)["output_config"]["effort"] == "high"  # the rewritten body went up
    else:
        assert sent.content == body
    assert relay.writer.kwargs("log_request")["request_body_bytes"] == len(body)  # the client's size


def test_a_bodyless_request_is_sent_as_before(relay):
    resp = relay.client.get("/v1/models")

    assert resp.status_code == 200
    sent = relay.sent[0]
    assert sent.content == b""
    assert "content-length" not in sent.headers and "transfer-encoding" not in sent.headers


def test_a_gate_timeout_answers_503_and_is_recorded(relay, monkeypatch, caplog):
    gate = BodyGate(max_bytes=1000, min_bytes=1, timeout_s=0.05)
    monkeypatch.setattr(proxy, "_body_gate", gate)
    holder = _immediate(gate.acquire(1000))
    try:
        with caplog.at_level(logging.WARNING, logger="llm-relay"):
            body, resp = _post(relay, _request(stream=True), session="late-session-id")
    finally:
        holder.release()

    assert resp.status_code == 503
    assert resp.json() == {"type": "error", "error": {
        "type": "api_error",
        "message": "llm-relay: request body gate timeout (waited 0s for {} B, max 1000 B)".format(len(body)),
    }}
    assert relay.sent == []  # never read, never sent
    assert relay.writer.names() == ["log_request"]
    row = relay.writer.kwargs("log_request")
    assert (row["status_code"], row["raw_usage"], row["request_body_bytes"]) == (
        503, {"stream_end": "body_gate_timeout"}, len(body),
    )
    assert (row["session_id"], row["endpoint"]) == ("late-session-id", "/v1/messages")
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        "request body gate timeout (waited 0s for {} B, max 1000 B) -- /v1/messages session=late-ses".format(
            len(body)),
    ]
    snap = gate.snapshot()
    assert (snap["timeouts"], snap["waiting"], snap["inflight_bytes"]) == (1, 0, 0)


def test_requests_that_waited_record_the_wait(relay, caplog):
    async def scenario():
        first_in = asyncio.Event()
        first_go = asyncio.Event()

        async def respond(request):
            if request.headers["x-claude-code-session-id"] == "first":
                first_in.set()
                await first_go.wait()  # hold the first body's admission (no headers yet)
            return _reply(request)

        relay.respond = respond
        async with _relay_client() as client:
            first = asyncio.ensure_future(_apost(client, _request(stream=False), "first"))
            await asyncio.wait_for(first_in.wait(), 5)
            stream = asyncio.ensure_future(_apost(client, _request(stream=True), "stream"))
            await _until(lambda: relay.gate.snapshot()["waiting"] == 1)
            plain = asyncio.ensure_future(_apost(client, _request(stream=False), "plain"))
            await _until(lambda: relay.gate.snapshot()["waiting"] == 2)
            first_go.set()
            return await asyncio.wait_for(asyncio.gather(first, stream, plain), 5)

    with caplog.at_level(logging.INFO, logger=GATE_LOG):
        responses = asyncio.run(scenario())

    assert [r.status_code for r in responses] == [200, 200, 200]
    rows = _rows(relay.writer)
    assert "body_gate_wait_ms" not in rows["first"]["raw_usage"]
    assert isinstance(rows["stream"]["raw_usage"]["body_gate_wait_ms"], int)
    assert rows["stream"]["raw_usage"]["body_gate_wait_ms"] >= 1
    assert rows["stream"]["raw_usage"]["stream_end"] == "completed"
    assert rows["plain"]["raw_usage"]["body_gate_wait_ms"] >= 1
    assert rows["plain"]["raw_usage"]["input_tokens"] == JSON_REPLY["usage"]["input_tokens"]
    snap = relay.gate.snapshot()
    assert (snap["waited_total"], snap["timeouts"]) == (2, 0)
    assert _idle(relay.gate)
    lines = _gate_lines(caplog)
    assert [line.split(" -- ")[1] for line in lines if "waiting for" in line] == [
        "/v1/messages session=stream", "/v1/messages session=plain",
    ]
    assert len([line for line in lines if "admitted after" in line]) == 2


def test_health_reports_the_gate(monkeypatch):
    gate = BodyGate(max_bytes=GATE_MAX, min_bytes=1024, timeout_s=5)
    monkeypatch.setattr(proxy, "_body_gate", gate)
    held = _immediate(gate.acquire(SIZE))
    try:
        payload = TestClient(proxy.app).get("/_health").json()
    finally:
        held.release()

    assert payload["body_gate"] == {
        "enabled": True, "max_bytes": GATE_MAX, "min_bytes": 1024, "timeout_s": 5,
        "inflight_bytes": SIZE, "inflight_requests": 1, "waiting": 0, "waited_total": 0, "timeouts": 0,
    }


def test_the_module_gate_follows_the_knobs():
    snap = TestClient(proxy.app).get("/_health").json()["body_gate"]

    assert snap["max_bytes"] == proxy._MAX_INFLIGHT_BODY_BYTES
    assert snap["min_bytes"] == proxy._BODY_GATE_MIN_BYTES
    assert snap["timeout_s"] == proxy._BODY_GATE_TIMEOUT_S
    assert snap["enabled"] is (proxy._MAX_INFLIGHT_BODY_BYTES > 0)
