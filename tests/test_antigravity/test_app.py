"""The Antigravity relay app (llm_relay.antigravity.app): forwarding fidelity and capture.

The upstream is always an ``httpx.MockTransport`` (no network) and the store a
SQLite file under ``tmp_path``, written through a synchronous writer that runs
each capture job at once on one connection. Fixtures are the sanitized agy
traffic in fixtures/ (README there); .sse files are read in binary mode.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

import httpx
import pytest
from starlette.testclient import TestClient

from llm_relay.antigravity import PROVIDER, store
from llm_relay.antigravity import app as app_module
from llm_relay.antigravity.app import create_app, endpoint_name

FIXTURES = Path(__file__).parent / "fixtures"
CONV_ID = "00000000-0000-4000-8000-000000000001"
# A fake value without a real token prefix, so secret scanners do not flag the test file.
TOKEN = "agy-relay-test-bearer-7f3c9e1d"
AUTH = {"authorization": "Bearer " + TOKEN}
STREAM_PATH = "/v1internal:streamGenerateContent?alt=sse"
QUOTA_PATH = "/v1internal:retrieveUserQuotaSummary"
SSE_HEADERS = {"content-type": "text/event-stream"}
JSON_HEADERS = {"content-type": "application/json; charset=UTF-8"}
AGENT_TURNS = ("turn1", "turn1_after_tools", "turn2")
TABLES = ("provider_requests", "provider_turns", "provider_quota")


def _raw(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _chunks(data: bytes, size: int) -> List[bytes]:
    return [data[i:i + size] for i in range(0, len(data), size)]


class ChunkStream(httpx.AsyncByteStream):
    """An upstream body delivered in the given chunks (optionally stalling after the first)."""

    def __init__(self, chunks: List[bytes], stall_after_first: bool = False) -> None:
        self.chunks = chunks
        self.stall_after_first = stall_after_first
        self.closed = False

    async def __aiter__(self):
        for index, chunk in enumerate(self.chunks):
            yield chunk
            if self.stall_after_first and index == 0:
                await asyncio.sleep(30)

    async def aclose(self) -> None:
        self.closed = True


class Upstream:
    """Mock upstream: records every request, answers with ``responder(request)``."""

    def __init__(self, responder: Optional[Callable[[httpx.Request], httpx.Response]] = None) -> None:
        self.requests: List[httpx.Request] = []
        self.bodies: List[bytes] = []
        self.responder = responder or (lambda request: reply_json(200, {"ok": True}))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(request.read())
        return self.responder(request)


def reply(status: int, headers: Any, body: bytes) -> httpx.Response:
    # Always a stream: httpx pre-reads a Response built with content=, which a real transport never does.
    return httpx.Response(status, headers=headers, stream=ChunkStream([body]))


def reply_json(status: int, obj: Any) -> httpx.Response:
    return reply(status, JSON_HEADERS, json.dumps(obj).encode("utf-8"))


def sse_response(name: str, chunk_size: int = 97) -> httpx.Response:
    return httpx.Response(200, headers=SSE_HEADERS, stream=ChunkStream(_chunks(_raw(name), chunk_size)))


def agent_responder(request: httpx.Request) -> httpx.Response:
    """The fixture SSE answer that belongs to the fixture request body (matched by requestId)."""
    body = json.loads(request.read())
    for name in AGENT_TURNS + ("monitoring",):
        if json.loads(_raw("req_{}.json".format(name)))["requestId"] == body.get("requestId"):
            return sse_response("resp_{}.sse".format(name))
    return sse_response("resp_turn2.sse")


class SyncWriter:
    """Runs each job immediately on one SQLite connection (the DBWriter interface, without the thread)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.jobs = 0
        self.size_hints: List[int] = []
        self.started = False
        self.stopped = False

    def submit(self, fn: Callable[..., Any], *, size_hint: int = 0, **kwargs: Any) -> None:
        self.jobs += 1
        self.size_hints.append(size_hint)
        fn(self.conn, **kwargs)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def snapshot(self) -> Dict[str, Any]:
        return {"alive": self.started and not self.stopped, "jobs": self.jobs}


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(str(tmp_path / "agy.db"), check_same_thread=False)
    connection.row_factory = sqlite3.Row
    store.ensure_schema(connection)
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def _relay_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LLM_RELAY_AGY_UPSTREAM", "http://upstream.test")
    monkeypatch.setenv("LLM_RELAY_AGY_SPILL_DIR", str(tmp_path / "spill"))
    monkeypatch.delenv("LLM_RELAY_AGY_CAPTURE", raising=False)
    monkeypatch.delenv("LLM_RELAY_AGY_MAX_CAPTURE_BYTES", raising=False)


@contextlib.contextmanager
def relay(upstream: Upstream, writer: Any) -> Iterator[TestClient]:
    app = create_app(upstream_transport=httpx.MockTransport(upstream), writer=writer)
    with TestClient(app) as client:
        yield client


def rows(conn: sqlite3.Connection, table: str) -> List[Dict[str, Any]]:
    return [dict(row) for row in conn.execute("SELECT * FROM {} ORDER BY id".format(table)).fetchall()]


def health(client: TestClient) -> Dict[str, Any]:
    response = client.get("/_health")
    assert response.status_code == 200
    return response.json()


# ── Forwarding ──


def test_endpoint_name():
    assert endpoint_name("/v1internal:streamGenerateContent") == "streamGenerateContent"
    assert endpoint_name("/v1internal:retrieveUserQuotaSummary") == "retrieveUserQuotaSummary"
    assert endpoint_name("/v1/models") == "/v1/models"


def test_forwarding_fidelity(conn):
    def responder(request):
        return reply(
            201,
            [("content-type", "application/json"), ("x-upstream-trace", "trace-123"),
             ("set-cookie", "a=1; Path=/"), ("set-cookie", "b=2; Path=/"), ("connection", "keep-alive")],
            b'{"answer": 42}',
        )

    upstream = Upstream(responder)
    body = b'{"project": "p", "metadata": {"x": [1, 2, 3]}}'
    headers = dict(AUTH)
    headers.update({
        "user-agent": "antigravity/1.5.13",
        "x-goog-api-client": "gl-node/22",
        "accept-encoding": "gzip, deflate, br",
        "keep-alive": "timeout=5",
        "te": "trailers",
        "trailer": "x-checksum",
        "proxy-authorization": "Basic cHJveHk6c2VjcmV0",
        "upgrade": "h2c",
        "content-type": "application/json",
    })
    with relay(upstream, SyncWriter(conn)) as client:
        response = client.post("/v1internal:loadCodeAssist?alt=json&x=a%2Fb", content=body, headers=headers)
        client.cookies.clear()
        other = client.get("/v1/models?pageSize=5", headers=AUTH)

    assert response.status_code == 201
    assert response.content == b'{"answer": 42}'
    assert response.headers["x-upstream-trace"] == "trace-123"
    assert response.headers.get_list("set-cookie") == ["a=1; Path=/", "b=2; Path=/"]

    sent = upstream.requests[0]
    assert sent.method == "POST"
    assert sent.url.raw_path == b"/v1internal:loadCodeAssist?alt=json&x=a%2Fb"
    assert sent.url.host == "upstream.test"
    assert upstream.bodies[0] == body
    assert sent.headers["authorization"] == "Bearer " + TOKEN
    assert sent.headers["accept-encoding"] == "identity"
    assert sent.headers["user-agent"] == "antigravity/1.5.13"
    assert sent.headers["x-goog-api-client"] == "gl-node/22"
    assert sent.headers["host"] == "upstream.test"
    assert sent.headers["content-length"] == str(len(body))
    for name in ("keep-alive", "te", "trailer", "proxy-authorization", "upgrade", "connection", "transfer-encoding"):
        assert name not in sent.headers, name

    # A path outside /v1internal: is forwarded too, method and query unchanged, and the
    # upstream's Set-Cookie from the first answer is not replayed by the relay.
    assert upstream.requests[1].method == "GET"
    assert upstream.requests[1].url.raw_path == b"/v1/models?pageSize=5"
    assert "cookie" not in upstream.requests[1].headers
    assert other.status_code == 201

    endpoints = [row["endpoint"] for row in rows(conn, "provider_requests")]
    assert endpoints == ["loadCodeAssist", "/v1/models"]


def test_streaming_is_relayed_byte_identical(conn):
    data = _raw("resp_turn1.sse")
    chunks = _chunks(data, 13)
    assert len(chunks) > 10
    upstream = Upstream(lambda request: httpx.Response(200, headers=SSE_HEADERS, stream=ChunkStream(chunks)))
    with relay(upstream, SyncWriter(conn)) as client:
        response = client.post(STREAM_PATH, content=_raw("req_turn1.json"), headers=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    assert response.content == data
    (row,) = rows(conn, "provider_requests")
    assert row["is_stream"] == 1
    assert row["stream_end"] == "completed"
    assert row["response_bytes"] == len(data)
    assert row["request_body_bytes"] == len(_raw("req_turn1.json"))
    assert row["latency_ms"] is not None and row["latency_ms"] >= 0


def test_content_encoded_response_is_passed_through_unparsed(conn):
    payload = b"\x1f\x8b not really gzip but opaque bytes"
    upstream = Upstream(lambda request: reply(
        200, {"content-type": "text/event-stream", "content-encoding": "gzip"}, payload))
    with relay(upstream, SyncWriter(conn)) as client:
        # A raw stream so the test client does not try to gunzip the opaque bytes.
        with client.stream("POST", STREAM_PATH, content=_raw("req_turn1.json"), headers=AUTH) as response:
            relayed = b"".join(response.iter_raw())
    assert response.headers["content-encoding"] == "gzip"
    assert relayed == payload
    (row,) = rows(conn, "provider_requests")
    assert row["stream_end"] == "unparsed_encoding"
    assert row["session_id"] == CONV_ID  # the request side is still parsed
    assert row["model_version"] is None and row["total_tokens"] == 0
    assert rows(conn, "provider_turns") == []


# ── Capture of conversation turns ──


def _send_turns(client: TestClient, names) -> None:
    for name in names:
        response = client.post(STREAM_PATH, content=_raw("req_{}.json".format(name)), headers=AUTH)
        assert response.status_code == 200
        assert response.content == _raw("resp_{}.sse".format(name))


def test_agent_turns_full_then_delta(conn):
    upstream = Upstream(agent_responder)
    writer = SyncWriter(conn)
    with relay(upstream, writer) as client:
        _send_turns(client, AGENT_TURNS)
        counters = health(client)["counters"]

    requests = rows(conn, "provider_requests")
    assert [row["session_id"] for row in requests] == [CONV_ID] * 3
    assert all(row["provider"] == PROVIDER for row in requests)
    assert [row["endpoint"] for row in requests] == ["streamGenerateContent"] * 3
    assert [row["request_type"] for row in requests] == ["agent"] * 3
    assert [row["message_count"] for row in requests] == [1, 3, 6]
    assert [row["step_index"] for row in requests] == [0, 3, 6]
    assert all(row["parent_session_id"] is None for row in requests)  # root == conversation
    assert all(row["model"] == "gemini-4-hydrogen-low" and row["thinking_level"] == "LOW" for row in requests)
    after_tools = requests[1]
    assert after_tools["prompt_tokens"] == 10685
    assert after_tools["cache_read"] == 8225
    assert after_tools["input_tokens"] == 2460
    assert json.loads(after_tools["raw_usage"])["promptTokenCount"] == 10685
    assert all(row["error"] is None and row["stream_end"] == "completed" for row in requests)

    turns = rows(conn, "provider_turns")
    assert [t["turn_number"] for t in turns] == [1, 2, 3]
    assert [t["storage_mode"] for t in turns] == ["full", "delta", "delta"]
    assert [len(json.loads(t["request_messages"])) for t in turns] == [1, 2, 3]
    assert [t["previous_message_count"] for t in turns] == [0, 1, 3]
    assert [t["total_message_count"] for t in turns] == [1, 3, 6]
    assert [t["request_row_id"] for t in turns] == [row["id"] for row in requests]
    assert turns[2]["last_user_prompt"] == "Now reply with exactly: done"
    assert len({t["first_fingerprint"] for t in turns}) == 1
    first_response = json.loads(turns[0]["response_message"])
    assert [block["type"] for block in first_response] == ["tool_use", "tool_use"]
    assert json.loads(turns[0]["thinking_blocks"])
    assert counters["turns_captured"] == 3 and counters["requests"] == 3 and counters["capture_errors"] == 0
    assert writer.jobs == 3
    assert writer.size_hints[0] >= len(_raw("req_turn1.json"))


def test_fresh_app_seeds_session_state_from_store(conn):
    with relay(Upstream(agent_responder), SyncWriter(conn)) as client:
        _send_turns(client, AGENT_TURNS)

    body = json.loads(_raw("req_turn2.json"))
    body["request"]["contents"] = copy.deepcopy(body["request"]["contents"]) + [
        {"role": "model", "parts": [{"text": "done"}]},
        {"role": "user", "parts": [{"text": "<USER_REQUEST>\nOne more time\n</USER_REQUEST>"}]},
    ]
    # A new app = empty in-memory session state; it must continue from the stored turn 3.
    with relay(Upstream(agent_responder), SyncWriter(conn)) as client:
        response = client.post(STREAM_PATH, content=json.dumps(body).encode("utf-8"), headers=AUTH)
    assert response.status_code == 200

    fourth = rows(conn, "provider_turns")[-1]
    assert fourth["turn_number"] == 4
    assert fourth["storage_mode"] == "delta"
    assert fourth["previous_message_count"] == 6
    assert fourth["total_message_count"] == 8
    messages = json.loads(fourth["request_messages"])
    assert [m["role"] for m in messages] == ["assistant", "user"]
    assert fourth["last_user_prompt"] == "One more time"


def test_shrunk_history_is_stored_full(conn):
    with relay(Upstream(agent_responder), SyncWriter(conn)) as client:
        _send_turns(client, ("turn1", "turn1_after_tools", "turn2", "turn1_after_tools"))
    last = rows(conn, "provider_turns")[-1]
    assert last["turn_number"] == 4
    assert last["storage_mode"] == "full"
    assert last["previous_message_count"] == 0
    assert len(json.loads(last["request_messages"])) == 3


def test_monitoring_side_call_has_no_turn(conn):
    with relay(Upstream(agent_responder), SyncWriter(conn)) as client:
        response = client.post(STREAM_PATH, content=_raw("req_monitoring.json"), headers=AUTH)
    assert response.content == _raw("resp_monitoring.sse")
    (row,) = rows(conn, "provider_requests")
    assert row["request_type"] == "agent_monitoring"
    assert row["session_id"] is None
    assert row["model"] == "gemini-3.5-flash-lite"
    assert row["total_tokens"] == 2742
    assert rows(conn, "provider_turns") == []


def test_generate_content_json_response_is_parsed(conn):
    events = [json.loads(line[5:]) for line in _raw("resp_turn2.sse").split(b"\r\n\r\n") if line.startswith(b"data:")]
    upstream = Upstream(lambda request: reply_json(200, events[-1]))
    body = _raw("req_turn1.json")
    with relay(upstream, SyncWriter(conn)) as client:
        response = client.post("/v1internal:generateContent", content=body, headers=AUTH)
    assert response.status_code == 200
    (row,) = rows(conn, "provider_requests")
    assert row["endpoint"] == "generateContent" and row["is_stream"] == 0
    assert row["total_tokens"] == events[-1]["response"]["usageMetadata"]["totalTokenCount"]
    (turn,) = rows(conn, "provider_turns")
    assert turn["storage_mode"] == "full" and turn["turn_number"] == 1


# ── Quota ──


def test_quota_rows_ignore_rolling_reset_time_and_small_jitter(conn, monkeypatch):
    """Unused buckets report a reset time that moves with the clock and replicas disagree in the
    third decimal; neither is a change worth a row until the throttle interval has passed."""
    summary = json.loads(_raw("quota_summary.json"))
    rolled = copy.deepcopy(summary)
    for group in rolled["groups"]:
        for bucket in group["buckets"]:
            bucket["resetTime"] = "2099-01-01T00:00:00Z"
    jitter = copy.deepcopy(rolled)
    first_bucket = jitter["groups"][0]["buckets"][0]
    first_bucket["remainingFraction"] = first_bucket["remainingFraction"] - 0.004
    answers = [summary, rolled, jitter, jitter]

    upstream = Upstream(lambda request: reply_json(200, answers.pop(0)))
    counts = []
    with relay(upstream, SyncWriter(conn)) as client:
        for i in range(4):
            if i == 3:  # same small drift again, but the throttle interval has passed
                relay_app = client.app.state.relay
                relay_app._quota_last = {k: (v[0], v[1] - app_module.QUOTA_MIN_INTERVAL_S - 1)
                                         for k, v in relay_app._quota_last.items()}
            assert client.post(QUOTA_PATH, content=b"{}", headers=AUTH).status_code == 200
            counts.append(len(rows(conn, "provider_quota")))
    assert counts == [5, 5, 5, 6]


def test_quota_rows_only_on_change(conn):
    summary = json.loads(_raw("quota_summary.json"))
    answers = [summary, summary]
    changed = copy.deepcopy(summary)
    changed["groups"][0]["buckets"][0]["remainingFraction"] = 0.5
    answers.append(changed)

    upstream = Upstream(lambda request: reply_json(200, answers.pop(0)))
    counts = []
    with relay(upstream, SyncWriter(conn)) as client:
        for _ in range(3):
            response = client.post(QUOTA_PATH, content=b"{}", headers=AUTH)
            assert response.status_code == 200
            counts.append(len(rows(conn, "provider_quota")))
        counters = health(client)["counters"]
    assert counts == [5, 5, 6]
    newest = rows(conn, "provider_quota")[-1]
    assert newest["bucket_id"] == changed["groups"][0]["buckets"][0]["bucketId"]
    assert newest["remaining_fraction"] == 0.5
    assert counters["quota_rows"] == 6
    assert [row["endpoint"] for row in rows(conn, "provider_requests")] == ["retrieveUserQuotaSummary"] * 3


# ── Errors ──


def test_upstream_json_error_is_relayed_and_recorded(conn):
    error_body = json.dumps(
        {"error": {"code": 429, "message": "Resource has been exhausted", "status": "RESOURCE_EXHAUSTED"}}
    ).encode()
    upstream = Upstream(lambda request: reply(429, JSON_HEADERS, error_body))
    with relay(upstream, SyncWriter(conn)) as client:
        response = client.post(STREAM_PATH, content=_raw("req_turn1.json"), headers=AUTH)
    assert response.status_code == 429
    assert response.content == error_body
    (row,) = rows(conn, "provider_requests")
    assert row["status_code"] == 429
    assert row["error"] == "RESOURCE_EXHAUSTED: Resource has been exhausted"
    assert row["session_id"] == CONV_ID
    assert rows(conn, "provider_turns") == []


@pytest.mark.parametrize(
    ("exc_type", "status"),
    [(httpx.ConnectError, 502), (httpx.RemoteProtocolError, 502), (httpx.ConnectTimeout, 504)],
)
def test_upstream_failure_before_headers(conn, exc_type, status):
    def responder(request):
        raise exc_type("upstream unreachable", request=request)

    with relay(Upstream(responder), SyncWriter(conn)) as client:
        response = client.post(STREAM_PATH, content=_raw("req_turn1.json"), headers=AUTH)
        counters = health(client)["counters"]
        in_flight = health(client)["in_flight"]
    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == status
    assert error["status"] == "RELAY_UPSTREAM_ERROR"
    assert exc_type.__name__ in error["message"]
    (row,) = rows(conn, "provider_requests")
    assert row["status_code"] == status
    assert "RELAY_UPSTREAM_ERROR" in row["error"]
    assert row["stream_end"] == "upstream_error"
    assert counters["upstream_errors"] == 1
    assert in_flight == 0
    assert rows(conn, "provider_turns") == []


def test_upstream_failure_mid_stream(conn):
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield _raw("resp_turn1.sse")[:200]
            raise httpx.ReadError("connection reset")

    upstream = Upstream(lambda request: httpx.Response(200, headers=SSE_HEADERS, stream=BrokenStream()))
    app = create_app(upstream_transport=httpx.MockTransport(upstream), writer=SyncWriter(conn))
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post(STREAM_PATH, content=_raw("req_turn1.json"), headers=AUTH)
        counters = health(client)["counters"]
    (row,) = rows(conn, "provider_requests")
    assert row["stream_end"] == "upstream_error"
    assert row["response_bytes"] == 200
    assert counters["upstream_errors"] == 1


def test_non_json_request_body(conn):
    upstream = Upstream(lambda request: reply_json(400, {"error": {"code": 400, "message": "Invalid JSON payload"}}))
    with relay(upstream, SyncWriter(conn)) as client:
        response = client.post(STREAM_PATH, content=b"\xff\xfenot json at all", headers=AUTH)
        counters = health(client)["counters"]
    assert response.status_code == 400
    assert upstream.bodies == [b"\xff\xfenot json at all"]
    (row,) = rows(conn, "provider_requests")
    assert row["model"] is None and row["session_id"] is None and row["request_type"] is None
    assert row["error"] == "400: Invalid JSON payload"
    assert counters["capture_errors"] == 0


def test_body_over_capture_limit_is_forwarded_without_turn(conn, monkeypatch):
    monkeypatch.setenv("LLM_RELAY_AGY_MAX_CAPTURE_BYTES", "100")
    upstream = Upstream(agent_responder)
    writer = SyncWriter(conn)
    with relay(upstream, writer) as client:
        response = client.post(STREAM_PATH, content=_raw("req_turn1.json"), headers=AUTH)
    assert response.content == _raw("resp_turn1.sse")
    assert upstream.bodies == [_raw("req_turn1.json")]
    (row,) = rows(conn, "provider_requests")
    assert row["error"] == "capture skipped: body too large"
    assert row["request_body_bytes"] == len(_raw("req_turn1.json"))
    assert rows(conn, "provider_turns") == []
    assert writer.size_hints[0] < len(_raw("req_turn1.json"))  # the body is not kept for the job


# ── Capture switched off, schema missing, capture failures ──


def test_capture_off_forwards_and_writes_nothing(conn, monkeypatch):
    monkeypatch.setenv("LLM_RELAY_AGY_CAPTURE", "0")
    writer = SyncWriter(conn)
    upstream = Upstream(agent_responder)
    with relay(upstream, writer) as client:
        response = client.post(STREAM_PATH, content=_raw("req_turn1.json"), headers=AUTH)
        body = health(client)
    assert response.content == _raw("resp_turn1.sse")
    assert len(upstream.requests) == 1
    assert writer.jobs == 0
    assert all(rows(conn, table) == [] for table in TABLES)
    assert body["capture"] is False


def test_missing_schema_skips_capture(conn, monkeypatch, caplog):
    calls = []

    def tables_present(connection):
        calls.append(1)
        return {"provider_requests": True, "provider_turns": False, "provider_quota": True}

    monkeypatch.setattr(store, "tables_present", tables_present)
    caplog.set_level(logging.WARNING, logger="llm-relay.antigravity.app")
    with relay(Upstream(agent_responder), SyncWriter(conn)) as client:
        _send_turns(client, ("turn1", "turn1_after_tools"))
        body = health(client)
    assert all(rows(conn, table) == [] for table in TABLES)
    assert body["counters"]["capture_skipped_no_schema"] == 2
    assert body["schema"] == {"provider_requests": True, "provider_turns": False, "provider_quota": True}
    assert len(calls) == 1  # re-checked at most once a minute
    paused = [r for r in caplog.records if "capture paused" in r.getMessage()]
    assert len(paused) == 1 and "provider_turns" in paused[0].getMessage()


def test_capture_job_exception_does_not_change_response(conn, monkeypatch, caplog):
    def explode(*args, **kwargs):
        raise RuntimeError("payload-looking text " + TOKEN)

    monkeypatch.setattr(store, "log_request", explode)
    caplog.set_level(logging.DEBUG)
    with relay(Upstream(agent_responder), SyncWriter(conn)) as client:
        first = client.post(STREAM_PATH, content=_raw("req_turn1.json"), headers=AUTH)
        second = client.post(STREAM_PATH, content=_raw("req_turn1_after_tools.json"), headers=AUTH)
        counters = health(client)["counters"]
    assert first.status_code == 200 and first.content == _raw("resp_turn1.sse")
    assert second.status_code == 200 and second.content == _raw("resp_turn1_after_tools.sse")
    assert counters["capture_errors"] == 2
    assert all(rows(conn, table) == [] for table in TABLES)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "agy capture" in r.getMessage()]
    assert len(warnings) == 1  # once per kind
    assert "RuntimeError" in warnings[0].getMessage()
    assert all("payload-looking" not in r.getMessage() and not r.exc_info for r in caplog.records)


def test_capture_job_retry_after_lost_connection_writes_rows_once(conn, monkeypatch):
    """A failure that leaves the connection unusable is raised to the writer; the retry does not duplicate."""
    flights = []
    real_log_turn = store.log_turn
    state = {"fail": False}

    def flaky_log_turn(connection, **kwargs):
        if state["fail"]:
            raise sqlite3.OperationalError("disk I/O error")
        return real_log_turn(connection, **kwargs)

    def broken_probe(connection):
        if state["fail"]:
            raise sqlite3.OperationalError("connection lost")
        return {table: True for table in TABLES}

    class RetryingWriter(SyncWriter):
        def submit(self, fn, *, size_hint=0, **kwargs):
            flights.append(kwargs["flight"])
            try:
                fn(self.conn, **kwargs)
            except app_module.CaptureConnectionError as exc:
                assert "disk I/O" not in str(exc)
                state["fail"] = False
                fn(self.conn, **kwargs)  # what DBWriter does on a fresh connection

    monkeypatch.setattr(store, "log_turn", flaky_log_turn)
    with relay(Upstream(agent_responder), RetryingWriter(conn)) as client:
        client.post(STREAM_PATH, content=_raw("req_turn1.json"), headers=AUTH)
        # The schema cache already says "present", so only the probe after the failed write sees the break.
        monkeypatch.setattr(store, "tables_present", broken_probe)
        state["fail"] = True
        client.post(STREAM_PATH, content=_raw("req_turn1_after_tools.json"), headers=AUTH)
    assert len(rows(conn, "provider_requests")) == 2
    turns = rows(conn, "provider_turns")
    assert [t["turn_number"] for t in turns] == [1, 2]
    # What a DBWriter spill record would show for the job: the endpoint, no payload.
    assert repr(flights[1]) == "<agy capture streamGenerateContent status=200 end=completed>"


# ── Health, logs ──


def test_health_is_local_and_documented(conn):
    upstream = Upstream()
    writer = SyncWriter(conn)
    with relay(upstream, writer) as client:
        body = health(client)
    assert upstream.requests == []
    assert set(body) == {"status", "provider", "upstream", "capture", "in_flight", "counters", "schema", "db_writer"}
    assert body["status"] == "ok"
    assert body["provider"] == PROVIDER
    assert body["upstream"] == "http://upstream.test"
    assert body["capture"] is True
    assert body["in_flight"] == 0
    assert set(body["counters"]) == {
        "requests", "turns_captured", "quota_rows", "capture_errors", "capture_skipped_no_schema", "upstream_errors",
    }
    assert body["db_writer"] == {"alive": True, "jobs": 0}
    assert writer.started and writer.stopped


def test_post_to_health_path_is_forwarded(conn):
    upstream = Upstream()
    with relay(upstream, SyncWriter(conn)) as client:
        client.post("/_health", content=b"{}")
    assert len(upstream.requests) == 1


def test_bearer_token_never_logged_or_stored(conn, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    calls = {"n": 0}

    def responder(request):
        calls["n"] += 1
        if calls["n"] == 4:
            raise httpx.ConnectError("refused", request=request)
        return agent_responder(request)

    with relay(Upstream(responder), SyncWriter(conn)) as client:
        _send_turns(client, AGENT_TURNS)
        client.post(STREAM_PATH, content=_raw("req_turn1.json"), headers=AUTH)  # connect failure
        client.post(QUOTA_PATH, content=b"{}", headers=AUTH)
        monkeypatch.setattr(store, "log_turn", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
        client.post(STREAM_PATH, content=_raw("req_turn2.json"), headers=AUTH)  # capture error
    assert caplog.records, "expected some log records to inspect"
    for record in caplog.records:
        text = record.getMessage() + repr(record.args) + (record.exc_text or "")
        assert TOKEN not in text
    for table in TABLES:
        for row in rows(conn, table):
            assert TOKEN not in json.dumps(row, default=str)


def test_default_writer_thread_end_to_end(tmp_path):
    """The private DBWriter (its own thread and spill directory) writes the same rows; stop() drains it."""
    db_path = tmp_path / "threaded.db"

    def factory():
        connection = sqlite3.connect(str(db_path), check_same_thread=False)
        connection.row_factory = sqlite3.Row
        store.ensure_schema(connection)
        return connection

    app = create_app(upstream_transport=httpx.MockTransport(Upstream(agent_responder)), conn_factory=factory)
    with TestClient(app) as client:
        _send_turns(client, AGENT_TURNS)
        snapshot = health(client)["db_writer"]
    assert snapshot["alive"] is True
    check = factory()
    try:
        assert [t["storage_mode"] for t in rows(check, "provider_turns")] == ["full", "delta", "delta"]
        assert len(rows(check, "provider_requests")) == 3
    finally:
        check.close()
    assert app.state.relay.spill_dir == tmp_path / "spill"
    assert not (tmp_path / "spill").exists()  # nothing had to be spilled


def test_default_conn_factory_uses_isolated_sqlite():
    from llm_relay.proxy import db

    assert not db._USE_PG  # conftest forces a temporary SQLite file
    connection = app_module._default_conn_factory()
    try:
        assert store.tables_present(connection) == {table: True for table in TABLES}
    finally:
        connection.close()


def test_app_module_stays_isolated_from_the_claude_code_proxy():
    """No import of llm_relay.proxy.proxy; llm_relay.proxy.db only lazily; no SQL of its own."""
    import ast

    tree = ast.parse(Path(app_module.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.update("{}.{}".format(node.module, alias.name) for alias in node.names)
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not any(name.startswith("llm_relay.proxy.proxy") for name in imported)
    top_level = {node.module for node in tree.body if isinstance(node, ast.ImportFrom)}
    assert "llm_relay.proxy.db" not in top_level
    source = Path(app_module.__file__).read_text(encoding="utf-8")
    assert "INSERT" not in source and "conversation_turns" not in source


def test_main_runs_uvicorn_on_configured_address(monkeypatch):
    import uvicorn

    from llm_relay.antigravity import __main__ as entry

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda target, **kwargs: seen.update(target=target, **kwargs))
    monkeypatch.setenv("LLM_RELAY_AGY_HOST", "127.0.0.2")
    monkeypatch.setenv("LLM_RELAY_AGY_PORT", "18086")
    entry.main()
    assert seen["target"] == "llm_relay.antigravity.app:app"
    assert (seen["host"], seen["port"]) == ("127.0.0.2", 18086)


# ── Client disconnect (optional item) ──


def test_client_disconnect_mid_stream_is_recorded(conn):
    stream = ChunkStream(_chunks(_raw("resp_turn1.sse"), 300), stall_after_first=True)
    upstream = Upstream(lambda request: httpx.Response(200, headers=SSE_HEADERS, stream=stream))
    app = create_app(upstream_transport=httpx.MockTransport(upstream), writer=SyncWriter(conn))
    relay_state = app.state.relay
    received: List[bytes] = []

    async def run() -> None:
        disconnected = asyncio.Event()
        request_sent = False

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": _raw("req_turn1.json"), "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                received.append(message["body"])
                disconnected.set()  # the client leaves after the first chunk

        path = "/v1internal:streamGenerateContent"
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
            "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"alt=sse", "root_path": "",
            "headers": [(b"authorization", AUTH["authorization"].encode())],
            "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8086),
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=10)
        await relay_state.shutdown()

    asyncio.run(run())
    assert received == [_raw("resp_turn1.sse")[:300]]
    (row,) = rows(conn, "provider_requests")
    assert row["stream_end"] == "client_disconnect"
    assert row["response_bytes"] == 300
    assert rows(conn, "provider_turns") == []  # a response cut short is not stored as a turn
    assert stream.closed  # the upstream response was closed
    assert relay_state._in_flight == 0
