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
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

import httpx
import pytest
from starlette.testclient import TestClient

from llm_relay.antigravity import PROVIDER, store, wire
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
    for name in ("LLM_RELAY_AGY_AUX_BODY_TYPES", "LLM_RELAY_AGY_BLOBS", "LLM_RELAY_AGY_BLOB_MAX_NEW_PER_DAY"):
        monkeypatch.delenv(name, raising=False)


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
    assert set(body) == {
        "status", "provider", "upstream", "capture", "in_flight", "counters", "schema", "schema_ext", "db_writer",
    }
    assert body["status"] == "ok"
    assert body["provider"] == PROVIDER
    assert body["upstream"] == "http://upstream.test"
    assert body["capture"] is True
    assert body["in_flight"] == 0
    assert set(body["counters"]) == {
        "requests", "turns_captured", "quota_rows", "capture_errors", "capture_skipped_no_schema", "upstream_errors",
        "aux_bodies", "blobs_written", "blobs_skipped_cap",
    }
    assert body["schema_ext"] is None  # not looked at before the first capture job
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


# ── Extended capture: answers on request rows, timing, blobs, older schemas ──

GENERATE_PATH = "/v1internal:generateContent"
MODELS_PATH = "/v1internal:fetchAvailableModels"
EXPERIMENTS_PATH = "/v1internal:listExperiments"
# Synthetic snapshot answers (the real ones were not sampled).
MODELS = {"models": {"gemini-synthetic": {"displayName": "Synthetic", "maxTokens": 1000, "tier": ["free"]}}}
EXPERIMENTS = {"experimentIds": [101, 102], "flags": [{"name": "flag-a", "boolValue": True}]}
WEB_SEARCH_BODY = json.dumps({
    "project": "project-fixture", "requestId": "web_search/00000000-0000-4000-8000-000000000005",
    "model": "gemini-synthetic", "requestType": "web_search",
    "request": {"contents": [{"role": "user", "parts": [{"text": "search: fixture query"}]}]},
}).encode("utf-8")
GROUNDING = {"webSearchQueries": ["fixture query"], "groundingChunks": [{"web": {"uri": "https://example.test/a"}}]}
BODY_COLUMNS = ("response_message", "thinking_blocks")


def make_db(path: Path, *, new_columns: bool = True, blobs: bool = True) -> sqlite3.Connection:
    """A store as an older release left it: optionally without the newer provider_requests columns
    and without provider_blobs (``ensure_schema`` would add both, so it is not called here)."""
    connection = sqlite3.connect(str(path), check_same_thread=False)
    connection.row_factory = sqlite3.Row
    tables = store.TABLES + (store.OPTIONAL_TABLES if blobs else ())
    for table in tables:
        defs = [
            "{} {}".format(name, sql_type) for name, sql_type in store.column_defs(table, "sqlite")
            if new_columns or table != "provider_requests" or name not in store.NEW_REQUEST_COLUMNS
        ]
        connection.execute("CREATE TABLE {} ({})".format(table, ", ".join(defs)))
    for name, table, columns in store.INDEXES:
        if table in tables:
            unique = "UNIQUE " if name in store.UNIQUE_INDEXES else ""
            connection.execute("CREATE {}INDEX {} ON {} ({})".format(unique, name, table, ", ".join(columns)))
    connection.commit()
    return connection


@pytest.fixture
def old_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = make_db(tmp_path / "old.db", new_columns=False, blobs=False)
    yield connection
    connection.close()


def sse(*events: Dict[str, Any]) -> bytes:
    return b"".join(b"data: " + json.dumps(event).encode("utf-8") + b"\r\n\r\n" for event in events)


def answer_event(parts: List[Dict[str, Any]], finish: Optional[str] = None, **candidate_keys: Any) -> Dict[str, Any]:
    candidate: Dict[str, Any] = {"content": {"role": "model", "parts": parts}}
    if finish:
        candidate["finishReason"] = finish
    candidate.update(candidate_keys)
    return {
        "response": {
            "candidates": [candidate],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2, "totalTokenCount": 12},
            "modelVersion": "gemini-synthetic",
            "responseId": "resp-synthetic",
        },
        "traceId": "trace-synthetic",
    }


def request_variant(name: str, **changes: Any) -> bytes:
    """A fixture request body with ``request_type``, ``request_id`` or ``system_text`` replaced."""
    body = json.loads(_raw("req_{}.json".format(name)))
    if "request_type" in changes:
        body["requestType"] = changes["request_type"]
    if "request_id" in changes:
        body["requestId"] = changes["request_id"]
    if "system_text" in changes:
        body["request"]["systemInstruction"] = {"role": "user", "parts": [{"text": changes["system_text"]}]}
    return json.dumps(body).encode("utf-8")


def side_call_responder(request: httpx.Request) -> httpx.Response:
    """agent / agent_monitoring as in the fixtures; checkpoint streams a text answer; web_search answers JSON."""
    kind = json.loads(request.read()).get("requestType")
    if kind == "checkpoint":
        return sse_response("resp_turn2.sse")
    if kind == "web_search":
        return reply_json(200, answer_event([{"text": "found it"}], finish="STOP", groundingMetadata=GROUNDING))
    return agent_responder(request)


def snapshot_responder(answers: Dict[str, List[Any]]) -> Callable[[httpx.Request], httpx.Response]:
    """Per endpoint, the next answer: a JSON value, raw bytes, or (status, JSON value)."""
    def responder(request: httpx.Request) -> httpx.Response:
        answer = answers[endpoint_name(request.url.path)].pop(0)
        if isinstance(answer, bytes):
            return reply(200, JSON_HEADERS, answer)
        if isinstance(answer, tuple):
            return reply_json(answer[0], answer[1])
        return reply_json(200, answer)
    return responder


def _hash(value: Any) -> str:
    digest = wire.content_hash(value)
    assert digest is not None
    return digest[0]


def _fixture_request(name: str = "turn1") -> Dict[str, Any]:
    return json.loads(_raw("req_{}.json".format(name)))["request"]


@pytest.mark.parametrize(
    ("env", "with_body"),
    [
        (None, {"checkpoint", "web_search"}),
        ("*", {"checkpoint", "web_search", "agent_monitoring"}),
        ("", set()),
        ("agent_monitoring, agent", {"agent_monitoring"}),  # "agent" cannot put a turn's answer here twice
    ],
)
def test_answers_of_calls_without_turn_row_go_on_the_request_row(conn, monkeypatch, env, with_body):
    if env is not None:
        monkeypatch.setenv("LLM_RELAY_AGY_AUX_BODY_TYPES", env)
    checkpoint = request_variant("turn1_after_tools", request_type="checkpoint", request_id="checkpoint/fixture-1")
    with relay(Upstream(side_call_responder), SyncWriter(conn)) as client:
        assert client.post(STREAM_PATH, content=_raw("req_turn1.json"), headers=AUTH).status_code == 200
        assert client.post(STREAM_PATH, content=checkpoint, headers=AUTH).content == _raw("resp_turn2.sse")
        assert client.post(GENERATE_PATH, content=WEB_SEARCH_BODY, headers=AUTH).status_code == 200
        assert client.post(STREAM_PATH, content=_raw("req_monitoring.json"), headers=AUTH).status_code == 200
        counters = health(client)["counters"]

    found = {row["request_type"]: row for row in rows(conn, "provider_requests")}
    assert set(found) == {"agent", "checkpoint", "web_search", "agent_monitoring"}
    stored = {kind for kind, row in found.items() if any(row[c] is not None for c in BODY_COLUMNS)}
    assert stored == with_body
    assert counters["aux_bodies"] == len(with_body) and counters["capture_errors"] == 0
    if "checkpoint" in with_body:
        assert json.loads(found["checkpoint"]["response_message"]) == [{"type": "text", "text": "done"}]
        assert found["checkpoint"]["thinking_blocks"] is None  # no thought in that answer: NULL, not []
    if "web_search" in with_body:
        assert json.loads(found["web_search"]["response_message"]) == [{"type": "text", "text": "found it"}]
    if "agent_monitoring" in with_body:
        monitoring = found["agent_monitoring"]
        assert [b["type"] for b in json.loads(monitoring["response_message"])] == ["text"]
        assert [b["type"] for b in json.loads(monitoring["thinking_blocks"])] == ["thinking"]
    # Whatever the setting: candidate keys without a column of their own are kept on every generate call.
    assert json.loads(found["web_search"]["response_extra"]) == {"groundingMetadata": GROUNDING}
    # Only the agent call has a turn row, and its answer is stored there alone.
    assert len(rows(conn, "provider_turns")) == 1
    assert all(found["agent"][c] is None for c in BODY_COLUMNS)


def test_agent_answer_cut_short_goes_on_the_request_row(conn):
    events = _raw("resp_turn1.sse").split(b"\r\n\r\n")
    partial = b"\r\n\r\n".join(events[:2]) + b"\r\n\r\n"  # the thought and the first tool call

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield partial
            raise httpx.ReadError("connection reset")

    upstream = Upstream(lambda request: httpx.Response(200, headers=SSE_HEADERS, stream=BrokenStream()))
    app = create_app(upstream_transport=httpx.MockTransport(upstream), writer=SyncWriter(conn))
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post(STREAM_PATH, content=_raw("req_turn1.json"), headers=AUTH)
        counters = health(client)["counters"]
    (row,) = rows(conn, "provider_requests")
    assert row["request_type"] == "agent" and row["stream_end"] == "upstream_error"
    assert [b["type"] for b in json.loads(row["thinking_blocks"])] == ["thinking"]
    assert [(b["type"], b["name"]) for b in json.loads(row["response_message"])] == [("tool_use", "read_file")]
    assert rows(conn, "provider_turns") == []
    assert counters["aux_bodies"] == 1 and counters["turns_captured"] == 0 and counters["capture_errors"] == 0


def test_agent_turns_keep_request_row_bodies_empty_and_turn_rows_unchanged(conn, old_conn):
    """A turn's answer is stored once, on its turn row; the turn rows equal those of a store without the new columns."""
    for connection in (conn, old_conn):
        with relay(Upstream(agent_responder), SyncWriter(connection)) as client:
            _send_turns(client, AGENT_TURNS)
            assert health(client)["counters"]["aux_bodies"] == 0
    assert all(row[c] is None for row in rows(conn, "provider_requests") for c in BODY_COLUMNS)

    def comparable(connection: sqlite3.Connection) -> List[Dict[str, Any]]:
        return [{k: v for k, v in row.items() if k not in ("id", "ts")} for row in rows(connection, "provider_turns")]

    assert comparable(conn) == comparable(old_conn)
    assert [t["storage_mode"] for t in rows(conn, "provider_turns")] == ["full", "delta", "delta"]


def test_request_row_carries_ids_sizes_and_timing(conn):
    def responder(request):
        if request.url.path.endswith(":generateContent"):
            return reply_json(200, answer_event([{"text": "plain"}], finish="MAX_TOKENS"))
        return agent_responder(request)

    with relay(Upstream(responder), SyncWriter(conn)) as client:
        _send_turns(client, ("turn1",))
        assert client.post(GENERATE_PATH, content=_raw("req_turn1.json"), headers=AUTH).status_code == 200
    stream_row, json_row = rows(conn, "provider_requests")
    request = _fixture_request()
    assert stream_row["finish_reason"] == "STOP"
    assert stream_row["trace_id"] == "trace-fixture" and stream_row["response_id"] == "resp-fixture"
    assert stream_row["trajectory_id"] == request["labels"]["trajectory_id"]
    assert stream_row["system_chars"] == len(request["systemInstruction"]["parts"][0]["text"])
    assert stream_row["tools_count"] == 2
    # One clock for all: headers, then the first thought, then the first answer part, then the end.
    assert 0 <= stream_row["headers_ms"] <= stream_row["first_thought_ms"] <= stream_row["first_answer_ms"]
    assert stream_row["first_answer_ms"] <= stream_row["latency_ms"]
    assert stream_row["response_extra"] is None  # the fixture has no candidate keys beyond content / finishReason
    assert json_row["finish_reason"] == "MAX_TOKENS" and json_row["trace_id"] == "trace-synthetic"
    assert json_row["headers_ms"] is not None and json_row["headers_ms"] >= 0
    assert json_row["first_thought_ms"] is None and json_row["first_answer_ms"] is None  # not timed without a stream


def test_same_system_prompt_and_tools_are_stored_once(conn, monkeypatch):
    calls: List[str] = []
    real_log_blob = store.log_blob

    def counting_log_blob(connection, **kwargs):
        calls.append(kwargs["kind"])
        return real_log_blob(connection, **kwargs)

    monkeypatch.setattr(store, "log_blob", counting_log_blob)
    revised = request_variant("turn2", system_text="SYSTEM PROMPT PLACEHOLDER (fixture, revised)")
    with relay(Upstream(agent_responder), SyncWriter(conn)) as client:
        _send_turns(client, ("turn1", "turn1_after_tools"))
        assert client.post(STREAM_PATH, content=revised, headers=AUTH).status_code == 200
        counters = health(client)["counters"]

    first, second, third = rows(conn, "provider_requests")
    request = _fixture_request()
    system_hash, system_size = wire.content_hash(request["systemInstruction"])
    tools_hash = _hash(request["tools"])
    assert first["system_hash"] == second["system_hash"] == system_hash
    assert first["tools_hash"] == second["tools_hash"] == third["tools_hash"] == tools_hash
    assert third["system_hash"] not in (None, system_hash)
    blobs = {(b["kind"], b["sha256"]): b for b in rows(conn, "provider_blobs")}
    assert set(blobs) == {("system_instruction", system_hash), ("tools", tools_hash),
                          ("system_instruction", third["system_hash"])}
    original = blobs[("system_instruction", system_hash)]
    assert json.loads(original["body"]) == request["systemInstruction"]
    assert original["size_bytes"] == system_size and original["provider"] == PROVIDER
    assert json.loads(blobs[("tools", tools_hash)]["body"]) == request["tools"]
    assert calls == ["system_instruction", "tools", "system_instruction"]  # the repeat costs no write
    assert counters["blobs_written"] == 3 and counters["capture_errors"] == 0

    # A restarted relay does not know what is stored: it tries once, and the store keeps one row.
    with relay(Upstream(agent_responder), SyncWriter(conn)) as client:
        _send_turns(client, ("turn1",))
        assert health(client)["counters"]["blobs_written"] == 0
    assert len(rows(conn, "provider_blobs")) == 3


def test_old_schema_keeps_capturing_and_fills_new_columns_after_migration(old_conn, monkeypatch, caplog):
    probes: List[int] = []
    real_probe = store.request_columns_present

    def counting_probe(connection):
        probes.append(1)
        return real_probe(connection)

    monkeypatch.setattr(store, "request_columns_present", counting_probe)
    caplog.set_level(logging.WARNING, logger="llm-relay.antigravity.app")
    with relay(Upstream(agent_responder), SyncWriter(old_conn)) as client:
        _send_turns(client, ("turn1", "turn1_after_tools"))
        before = health(client)
        store.ensure_schema(old_conn)  # the migration lands while the relay runs
        _send_turns(client, ("turn2",))  # within SCHEMA_RECHECK_S: still the original columns
        monkeypatch.setattr(app_module, "SCHEMA_RECHECK_S", 0.0)
        assert client.post(STREAM_PATH, content=_raw("req_monitoring.json"), headers=AUTH).status_code == 200
        after = health(client)

    requests = rows(old_conn, "provider_requests")
    assert len(requests) == 4
    assert [t["turn_number"] for t in rows(old_conn, "provider_turns")] == [1, 2, 3]
    assert before["counters"]["capture_errors"] == 0 and after["counters"]["capture_errors"] == 0
    assert before["counters"]["turns_captured"] == 2
    assert before["schema_ext"] == {"request_columns_missing": list(store.NEW_REQUEST_COLUMNS), "provider_blobs": False}
    assert after["schema_ext"] == {"request_columns_missing": [], "provider_blobs": True}
    assert len(probes) == 2  # at the first job and after the interval -- not per request
    # Rows written before the columns were seen leave them NULL; the first one after fills them.
    assert all(row[c] is None for row in requests[:3] for c in ("finish_reason", "trace_id", "system_hash"))
    newest = requests[3]
    assert newest["request_type"] == "agent_monitoring"
    assert newest["finish_reason"] == "STOP" and newest["trace_id"] == "trace-fixture"
    assert newest["system_hash"] == _hash(_fixture_request("monitoring")["systemInstruction"])
    assert [b["kind"] for b in rows(old_conn, "provider_blobs")] == ["system_instruction"]
    messages = [r.getMessage() for r in caplog.records if r.name == "llm-relay.antigravity.app"]
    missing = [m for m in messages if "migrate-provider-columns.py" in m]
    assert len(missing) == 1
    assert "provider_blobs" in missing[0] and "response_hash" in missing[0]
    assert len([m for m in messages if "writing all columns" in m]) == 1


def test_failed_column_lookup_writes_original_columns_and_looks_again(conn, monkeypatch):
    real_probe = store.request_columns_present
    state = {"fail": True}

    def flaky_probe(connection):
        if state.pop("fail", False):
            raise sqlite3.OperationalError("database is locked")
        return real_probe(connection)

    monkeypatch.setattr(store, "request_columns_present", flaky_probe)
    with relay(Upstream(agent_responder), SyncWriter(conn)) as client:
        _send_turns(client, ("turn1", "turn1_after_tools"))
        body = health(client)
    first, second = rows(conn, "provider_requests")
    assert first["finish_reason"] is None and first["system_hash"] is None  # the safe side for that job
    assert second["finish_reason"] == "STOP" and second["system_hash"] is not None
    assert len(rows(conn, "provider_turns")) == 2
    assert body["counters"]["capture_errors"] == 1
    assert body["schema_ext"] == {"request_columns_missing": [], "provider_blobs": True}


@pytest.mark.parametrize("case", ["no_blob_table", "blobs_off"])
def test_hashes_are_recorded_without_blob_writes(tmp_path, monkeypatch, case):
    connection = make_db(tmp_path / "store.db", blobs=(case == "blobs_off"))
    if case == "blobs_off":
        monkeypatch.setenv("LLM_RELAY_AGY_BLOBS", "0")
    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(store, "log_blob", lambda *args, **kwargs: calls.append(kwargs) or True)

    def responder(request):
        if request.url.path.endswith(":fetchAvailableModels"):
            return reply_json(200, MODELS)
        return agent_responder(request)

    try:
        with relay(Upstream(responder), SyncWriter(connection)) as client:
            _send_turns(client, ("turn1", "turn1_after_tools"))
            assert client.post(MODELS_PATH, content=b"{}", headers=AUTH).status_code == 200
            body = health(client)
        requests = rows(connection, "provider_requests")
        request = _fixture_request()
        assert [r["system_hash"] for r in requests[:2]] == [_hash(request["systemInstruction"])] * 2
        assert [r["tools_hash"] for r in requests[:2]] == [_hash(request["tools"])] * 2
        assert requests[2]["response_hash"] == _hash(MODELS)
        assert calls == []
        assert len(rows(connection, "provider_turns")) == 2
        assert body["counters"]["capture_errors"] == 0 and body["counters"]["blobs_written"] == 0
        assert body["schema_ext"] == {"request_columns_missing": [], "provider_blobs": case == "blobs_off"}
        if case == "blobs_off":
            assert rows(connection, "provider_blobs") == []
    finally:
        connection.close()


def test_snapshot_endpoints_hash_and_store_each_answer_once(conn):
    answers = {"fetchAvailableModels": [MODELS, copy.deepcopy(MODELS)], "listExperiments": [EXPERIMENTS]}
    with relay(Upstream(snapshot_responder(answers)), SyncWriter(conn)) as client:
        for path, answer in ((MODELS_PATH, MODELS), (MODELS_PATH, MODELS), (EXPERIMENTS_PATH, EXPERIMENTS)):
            response = client.post(path, content=b"{}", headers=AUTH)
            assert response.status_code == 200 and response.content == json.dumps(answer).encode("utf-8")
        counters = health(client)["counters"]
    first, second, third = rows(conn, "provider_requests")
    assert [r["endpoint"] for r in (first, second, third)] == ["fetchAvailableModels"] * 2 + ["listExperiments"]
    assert first["response_hash"] == second["response_hash"] == _hash(MODELS)
    assert third["response_hash"] == _hash(EXPERIMENTS)
    blobs = rows(conn, "provider_blobs")
    assert [(b["kind"], b["sha256"]) for b in blobs] == [("available_models", _hash(MODELS)),
                                                         ("experiments", _hash(EXPERIMENTS))]
    assert json.loads(blobs[0]["body"]) == MODELS and json.loads(blobs[1]["body"]) == EXPERIMENTS
    assert counters["blobs_written"] == 2 and counters["capture_errors"] == 0


def test_snapshot_without_a_complete_json_answer_has_no_hash(conn):
    oversized = json.dumps({"models": "x" * app_module.SNAPSHOT_BODY_MAX}).encode("utf-8")
    answers = {
        "fetchAvailableModels": [oversized, b"<html>not json</html>"],
        "listExperiments": [(503, {"error": {"code": 503, "message": "unavailable"}})],
    }
    with relay(Upstream(snapshot_responder(answers)), SyncWriter(conn)) as client:
        assert client.post(MODELS_PATH, content=b"{}", headers=AUTH).content == oversized
        assert client.post(MODELS_PATH, content=b"{}", headers=AUTH).content == b"<html>not json</html>"
        assert client.post(EXPERIMENTS_PATH, content=b"{}", headers=AUTH).status_code == 503
        counters = health(client)["counters"]
    requests = rows(conn, "provider_requests")
    assert len(requests) == 3 and all(r["response_hash"] is None for r in requests)
    assert requests[0]["response_bytes"] == len(oversized)
    assert rows(conn, "provider_blobs") == []
    assert counters["capture_errors"] == 0


def test_daily_cap_on_new_blobs_keeps_hashes(conn, monkeypatch, caplog):
    monkeypatch.setenv("LLM_RELAY_AGY_BLOB_MAX_NEW_PER_DAY", "1")
    caplog.set_level(logging.WARNING, logger="llm-relay.antigravity.app")
    bodies = [request_variant("turn1", system_text="SYSTEM PROMPT VARIANT {}".format(n)) for n in range(3)]
    hashes = [_hash(json.loads(body)["request"]["systemInstruction"]) for body in bodies]
    with relay(Upstream(agent_responder), SyncWriter(conn)) as client:
        for body in bodies + [bodies[2]]:
            assert client.post(STREAM_PATH, content=body, headers=AUTH).status_code == 200
        capped = health(client)["counters"]
        # A day later the window has room again: a body left out before is stored when it comes back.
        relay_state = client.app.state.relay
        relay_state._blob_new = {
            kind: deque(t - app_module.BLOB_CAP_WINDOW_S - 1 for t in times)
            for kind, times in relay_state._blob_new.items()
        }
        assert client.post(STREAM_PATH, content=bodies[1], headers=AUTH).status_code == 200
        later = health(client)["counters"]

    assert [r["system_hash"] for r in rows(conn, "provider_requests")] == hashes + [hashes[2], hashes[1]]
    blobs = rows(conn, "provider_blobs")
    assert [b["sha256"] for b in blobs if b["kind"] == "system_instruction"] == [hashes[0], hashes[1]]
    assert [b["kind"] for b in blobs].count("tools") == 1  # one distinct tool list: never capped
    assert capped["blobs_skipped_cap"] == 2  # two distinct bodies left out; the repeat is not counted again
    assert capped["blobs_written"] == 2  # the first system prompt and the tools
    assert later["blobs_written"] == 3 and later["blobs_skipped_cap"] == 2
    cap_warnings = [r.getMessage() for r in caplog.records if "LLM_RELAY_AGY_BLOB_MAX_NEW_PER_DAY" in r.getMessage()]
    assert len(cap_warnings) == 1 and "system_instruction" in cap_warnings[0]
    assert not any(h in message for h in hashes for message in cap_warnings)


def test_blob_failure_does_not_block_request_or_turn_rows(conn, monkeypatch, caplog):
    def explode(*args, **kwargs):
        raise RuntimeError("payload-looking text " + TOKEN)

    monkeypatch.setattr(store, "log_blob", explode)
    caplog.set_level(logging.DEBUG)
    with relay(Upstream(agent_responder), SyncWriter(conn)) as client:
        _send_turns(client, ("turn1",))
        counters = health(client)["counters"]
    (row,) = rows(conn, "provider_requests")
    assert row["system_hash"] is not None and row["tools_hash"] is not None  # hashed before the write failed
    assert len(rows(conn, "provider_turns")) == 1
    # One error per blob (system_instruction, tools): each is tried on its own.
    assert counters["capture_errors"] == 2 and counters["turns_captured"] == 1 and counters["blobs_written"] == 0
    assert rows(conn, "provider_blobs") == []
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno == logging.WARNING and "agy capture" in r.getMessage()]
    assert len(warnings) == 1 and "blob" in warnings[0] and "RuntimeError" in warnings[0]
    assert all("payload-looking" not in r.getMessage() and not r.exc_info for r in caplog.records)


def test_one_failing_blob_costs_only_itself_and_is_not_retried(conn, monkeypatch):
    calls: List[str] = []
    real_log_blob = store.log_blob

    def picky_log_blob(connection, **kwargs):
        calls.append(kwargs["kind"])
        if kwargs["kind"] == "system_instruction":
            raise sqlite3.DataError("value refused")
        return real_log_blob(connection, **kwargs)

    monkeypatch.setattr(store, "log_blob", picky_log_blob)
    with relay(Upstream(agent_responder), SyncWriter(conn)) as client:
        _send_turns(client, ("turn1",))
        first = health(client)["counters"]
        _send_turns(client, ("turn1_after_tools",))  # the same system prompt and tools again
        second = health(client)["counters"]
    assert [b["kind"] for b in rows(conn, "provider_blobs")] == ["tools"]
    assert first["capture_errors"] == 1 and first["blobs_written"] == 1
    # The connection was fine, so the refused value is marked: no second try, no second count.
    assert second["capture_errors"] == 1 and calls == ["system_instruction", "tools"]
    request = _fixture_request()
    hashes = (_hash(request["systemInstruction"]), _hash(request["tools"]))
    assert [(r["system_hash"], r["tools_hash"]) for r in rows(conn, "provider_requests")] == [hashes] * 2
    assert len(rows(conn, "provider_turns")) == 2


def test_blob_failure_on_a_dead_connection_is_tried_again_by_the_next_request(conn, monkeypatch):
    calls: List[str] = []
    real_log_blob = store.log_blob
    real_probe = store.tables_present
    state = {"dead": True}

    def flaky_log_blob(connection, **kwargs):
        calls.append(kwargs["kind"])
        if state["dead"]:
            raise sqlite3.OperationalError("connection lost")
        return real_log_blob(connection, **kwargs)

    def probe(connection, tables=store.TABLES):
        if state["dead"]:
            raise sqlite3.OperationalError("connection lost")
        return real_probe(connection, tables)

    class HealingWriter(SyncWriter):
        def submit(self, fn, *, size_hint=0, **kwargs):
            try:
                fn(self.conn, **kwargs)
            finally:
                state["dead"] = False  # the writer's next connection works; this job is not retried here

    monkeypatch.setattr(store, "log_blob", flaky_log_blob)
    with relay(Upstream(agent_responder), HealingWriter(conn)) as client:
        # Fill the schema caches first with a call that carries no system prompt or tools.
        state["dead"] = False
        assert client.post(QUOTA_PATH, content=b"{}", headers=AUTH).status_code == 200
        monkeypatch.setattr(store, "tables_present", probe)
        state["dead"] = True
        _send_turns(client, ("turn1",))  # the capture job fails for the writer; the answer is relayed regardless
        _send_turns(client, ("turn1_after_tools",))
        counters = health(client)["counters"]
    assert calls == ["system_instruction", "tools", "system_instruction", "tools"]
    assert sorted(b["kind"] for b in rows(conn, "provider_blobs")) == ["system_instruction", "tools"]
    assert counters["blobs_written"] == 2


def test_request_row_rejected_with_body_columns_is_written_without_them(conn, monkeypatch):
    real_log_request = store.log_request

    def picky_log_request(connection, **kwargs):
        if any(kwargs.get(name) is not None for name in app_module._REQUEST_BODY_COLUMNS):
            raise sqlite3.DataError("value refused")
        return real_log_request(connection, **kwargs)

    monkeypatch.setattr(store, "log_request", picky_log_request)
    citations = {"citations": [{"uri": "https://example.test/c", "startIndex": 0}]}
    body = sse(answer_event([{"text": "answer"}]), answer_event([{"text": ""}], finish="STOP",
                                                                citationMetadata=citations))
    upstream = Upstream(lambda request: httpx.Response(200, headers=SSE_HEADERS, stream=ChunkStream(_chunks(body, 50))))
    with relay(upstream, SyncWriter(conn)) as client:
        assert client.post(STREAM_PATH, content=_raw("req_turn1.json"), headers=AUTH).content == body
        checkpoint = request_variant("turn1", request_type="checkpoint", request_id="checkpoint/fixture-2")
        assert client.post(STREAM_PATH, content=checkpoint, headers=AUTH).status_code == 200
        counters = health(client)["counters"]
    agent_row, checkpoint_row = rows(conn, "provider_requests")
    for row in (agent_row, checkpoint_row):
        assert all(row[c] is None for c in app_module._REQUEST_BODY_COLUMNS)
        assert row["finish_reason"] == "STOP"  # the rest of the row is there
    assert agent_row["request_type"] == "agent" and checkpoint_row["request_type"] == "checkpoint"
    assert len(rows(conn, "provider_turns")) == 1  # the agent turn behind its request row
    assert counters["capture_errors"] == 2 and counters["aux_bodies"] == 0 and counters["turns_captured"] == 1


def test_request_row_failure_on_a_dead_connection_is_not_retried_without_body_columns(conn, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="llm-relay.antigravity.app")
    calls: List[Dict[str, Any]] = []

    def failing_log_request(connection, **kwargs):
        calls.append(kwargs)
        raise sqlite3.OperationalError("connection lost")

    def dead_probe(connection, tables=store.TABLES):
        raise sqlite3.OperationalError("connection lost")

    with relay(Upstream(side_call_responder), SyncWriter(conn)) as client:
        assert client.post(QUOTA_PATH, content=b"{}", headers=AUTH).status_code == 200  # fills the schema caches
        monkeypatch.setattr(store, "log_request", failing_log_request)
        monkeypatch.setattr(store, "tables_present", dead_probe)
        # The capture job raises CaptureConnectionError for the writer; the answer is relayed regardless.
        assert client.post(GENERATE_PATH, content=WEB_SEARCH_BODY, headers=AUTH).status_code == 200
    assert len(calls) == 1 and calls[0]["response_extra"] is not None  # no second try without the body columns
    assert rows(conn, "provider_requests")[-1]["endpoint"] == QUOTA_PATH.rsplit(":", 1)[1]
    assert not any("request_ext" in r.getMessage() for r in caplog.records)


def test_star_includes_generate_calls_without_request_type(conn, monkeypatch):
    untyped = json.loads(WEB_SEARCH_BODY)
    del untyped["requestType"]
    untyped_body = json.dumps(untyped).encode("utf-8")

    def responder(request):
        return reply_json(200, answer_event([{"text": "untyped"}], finish="STOP"))

    def run(connection):
        with relay(Upstream(responder), SyncWriter(connection)) as client:
            assert client.post(GENERATE_PATH, content=untyped_body, headers=AUTH).status_code == 200
            counters = health(client)["counters"]
        (row,) = rows(connection, "provider_requests")
        assert row["request_type"] is None
        return row, counters

    row, counters = run(conn)  # the default list names types only
    assert all(row[c] is None for c in BODY_COLUMNS) and counters["aux_bodies"] == 0
    monkeypatch.setenv("LLM_RELAY_AGY_AUX_BODY_TYPES", "*")
    other = make_db(conn_path(conn).with_name("star.db"))
    try:
        row, counters = run(other)
        assert json.loads(row["response_message"]) == [{"type": "text", "text": "untyped"}]
        assert counters["aux_bodies"] == 1 and counters["capture_errors"] == 0
        assert rows(other, "provider_turns") == []
    finally:
        other.close()


def conn_path(connection: sqlite3.Connection) -> Path:
    return Path(connection.execute("PRAGMA database_list").fetchone()["file"])


def test_snapshot_cut_short_has_no_hash_and_no_blob(conn):
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield json.dumps(MODELS).encode("utf-8")  # a complete JSON value, then the upstream fails
            raise httpx.ReadError("connection reset")

    upstream = Upstream(lambda request: httpx.Response(200, headers=JSON_HEADERS, stream=BrokenStream()))
    app = create_app(upstream_transport=httpx.MockTransport(upstream), writer=SyncWriter(conn))
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post(MODELS_PATH, content=b"{}", headers=AUTH)
        counters = health(client)["counters"]
    (row,) = rows(conn, "provider_requests")
    assert row["stream_end"] == "upstream_error"
    assert row["response_hash"] is None
    assert rows(conn, "provider_blobs") == []
    assert counters["blobs_written"] == 0 and counters["capture_errors"] == 0


def test_daily_cap_counts_only_blobs_new_to_the_store(conn, monkeypatch):
    monkeypatch.setenv("LLM_RELAY_AGY_BLOB_MAX_NEW_PER_DAY", "1")
    known = dict(MODELS, version="already-stored")
    fresh = dict(MODELS, version="new")
    digest = wire.content_hash(known)
    assert digest is not None
    # Stored before this process started (another process, or before a restart).
    assert store.log_blob(conn, provider=PROVIDER, kind="available_models", sha256=digest[0], size_bytes=digest[1],
                          body=known)
    answers = {"fetchAvailableModels": [known, fresh]}
    with relay(Upstream(snapshot_responder(answers)), SyncWriter(conn)) as client:
        for _ in range(2):
            assert client.post(MODELS_PATH, content=b"{}", headers=AUTH).status_code == 200
        counters = health(client)["counters"]
    assert [b["sha256"] for b in rows(conn, "provider_blobs")] == [_hash(known), _hash(fresh)]
    assert counters["blobs_written"] == 1 and counters["blobs_skipped_cap"] == 0


def test_retried_job_writes_blobs_and_request_row_once(conn, monkeypatch):
    """The writer retries a job after a lost connection; blobs and the request row are not written again."""
    flights = []
    calls: List[str] = []
    real_log_blob = store.log_blob
    real_log_turn = store.log_turn
    state = {"fail": True}

    def counting_log_blob(connection, **kwargs):
        calls.append(kwargs["kind"])
        return real_log_blob(connection, **kwargs)

    def flaky_log_turn(connection, **kwargs):
        if state["fail"]:
            raise sqlite3.OperationalError("disk I/O error")
        return real_log_turn(connection, **kwargs)

    def broken_probe(connection, tables=store.TABLES):
        if state["fail"]:
            raise sqlite3.OperationalError("connection lost")
        return {table: True for table in tables}

    class RetryingWriter(SyncWriter):
        def submit(self, fn, *, size_hint=0, **kwargs):
            flights.append(kwargs["flight"])
            try:
                fn(self.conn, **kwargs)
            except app_module.CaptureConnectionError:
                state["fail"] = False
                fn(self.conn, **kwargs)

    monkeypatch.setattr(store, "log_blob", counting_log_blob)
    monkeypatch.setattr(store, "log_turn", flaky_log_turn)
    with relay(Upstream(agent_responder), RetryingWriter(conn)) as client:
        state["fail"] = False
        # Fills both schema caches, so the break shows only after the writes; its own prompt, so that
        # turn1's system prompt is still new afterwards (the fixtures share one placeholder).
        priming = request_variant("monitoring", system_text="MONITORING PROMPT PLACEHOLDER (fixture)")
        assert client.post(STREAM_PATH, content=priming, headers=AUTH).status_code == 200
        calls.clear()
        monkeypatch.setattr(store, "tables_present", broken_probe)
        state["fail"] = True
        _send_turns(client, ("turn1",))
        counters = health(client)["counters"]
    turn_flight = flights[1]
    assert turn_flight.blob_done and turn_flight.row_written and turn_flight.turn_done
    assert len(rows(conn, "provider_requests")) == 2
    assert len(rows(conn, "provider_turns")) == 1
    assert sorted(b["kind"] for b in rows(conn, "provider_blobs")) == ["system_instruction"] * 2 + ["tools"]
    assert calls == ["system_instruction", "tools"]  # the retry wrote none of them again
    assert counters["capture_errors"] == 1 and counters["blobs_written"] == 3


def test_response_extra_is_kept_on_agent_turn_request_rows(conn):
    citations = {"citations": [{"uri": "https://example.test/c", "startIndex": 0}]}
    body = sse(answer_event([{"text": "answer"}]), answer_event([{"text": ""}], finish="STOP",
                                                                citationMetadata=citations))
    upstream = Upstream(lambda request: httpx.Response(200, headers=SSE_HEADERS, stream=ChunkStream(_chunks(body, 50))))
    with relay(upstream, SyncWriter(conn)) as client:
        assert client.post(STREAM_PATH, content=_raw("req_turn1.json"), headers=AUTH).content == body
    (row,) = rows(conn, "provider_requests")
    assert json.loads(row["response_extra"]) == {"citationMetadata": citations}
    assert all(row[c] is None for c in BODY_COLUMNS)  # a turn: the answer is on the turn row
    (turn,) = rows(conn, "provider_turns")
    assert json.loads(turn["response_message"]) == [{"type": "text", "text": "answer"}]
