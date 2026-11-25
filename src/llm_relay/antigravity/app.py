"""Transparent relay for Antigravity CLI (agy) Code Assist traffic, with capture on the side.

agy sends its backend calls (``/v1internal:<method>``, the user's OAuth bearer
token in ``Authorization``) to ``CLOUD_CODE_URL``. Pointed at this app, every
request is forwarded unchanged to the real backend (``LLM_RELAY_AGY_UPSTREAM``)
and, as a side effect, requests, conversation turns and quota snapshots are
recorded in the ``provider_*`` tables -- parsed by ``wire``, written by ``store``.

Isolation from the Claude Code proxy is the first priority: this runs as its own
process on its own port (``python -m llm_relay.antigravity``), keeps its own
``DBWriter`` and spill directory, does not import ``llm_relay.proxy.proxy`` and
writes only through ``store``. A bug here may break agy; it cannot reach the
Claude Code sessions behind the other relay.

Capture never changes, delays or fails a response:

* the event loop only feeds response chunks to a ``wire.SSEAccumulator`` (or
  collects a small JSON body), after each chunk has been handed to the client;
* request parsing and every DB call happen in ONE job per request, run on the
  writer thread. The job is submitted synchronously from the stream's teardown
  and never awaited there: Starlette tears a streaming generator down at its
  ``yield`` and an ``await`` in its ``finally`` can be cut short, which is how
  records were lost in the Claude Code proxy (see ``llm_relay.proxy.dbwriter``);
* every capture failure is counted (``capture_errors``) and logged once per
  kind, by exception type only.

Never written to logs or rows: header values (the bearer token above all) and
request/response bodies. Log lines name endpoints, statuses and exception types.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.cookiejar
import logging
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

import anyio
import httpx
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from llm_relay.antigravity import PROVIDER, store, wire
from llm_relay.proxy.dbwriter import DBWriter

logger = logging.getLogger("llm-relay.antigravity.app")

DEFAULT_UPSTREAM = "https://daily-cloudcode-pa.googleapis.com"
DEFAULT_MAX_CAPTURE_BYTES = 64 * 1024 * 1024
DEFAULT_SPILL_DIR = "~/.llm-relay/antigravity"
SESSION_STATE_MAX = 2048  # conversations whose delta state is kept in memory (LRU)
SCHEMA_RECHECK_S = 60.0  # while a provider table is missing, look again at most this often
QUOTA_BODY_MAX = 1024 * 1024  # retrieveUserQuotaSummary answers with a few KB of JSON
# Quota rows: agy polls the summary several times per call, unused buckets report a reset time
# that rolls with the clock, and replicas disagree in the third decimal. So a bucket is written
# again only on a real move (>= QUOTA_STEP) or, for smaller drift, at most every QUOTA_MIN_INTERVAL_S.
QUOTA_STEP = 0.01
QUOTA_MIN_INTERVAL_S = 300.0
CLOSE_TIMEOUT_S = 5.0  # bound on closing an upstream response during teardown

UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=30.0)
# agy runs a handful of concurrent calls (stream, monitoring side call, quota poll).
UPSTREAM_LIMITS = httpx.Limits(max_connections=64, max_keepalive_connections=16, keepalive_expiry=60.0)

GENERATE_ENDPOINTS = frozenset(("streamGenerateContent", "generateContent"))
QUOTA_ENDPOINT = "retrieveUserQuotaSummary"
RELAY_ERROR_STATUS = "RELAY_UPSTREAM_ERROR"

# Hop-by-hop headers (plus content-length, which httpx / the ASGI server recompute)
# are never relayed in either direction.
_HOP_BY_HOP = frozenset(
    (
        b"host", b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization",
        b"te", b"trailer", b"transfer-encoding", b"upgrade", b"content-length",
    )
)
# httpx.AsyncClient puts these on every request it builds; they are removed again
# unless agy sent them itself, so the upstream sees agy's headers and nothing else.
_CLIENT_DEFAULT_HEADERS = ("accept", "user-agent", "connection")
_COUNTERS = (
    "requests", "turns_captured", "quota_rows", "capture_errors", "capture_skipped_no_schema", "upstream_errors",
)
_MISSING = object()


# ── Small helpers ──


def endpoint_name(path: str) -> str:
    """Text after the last ``:`` of the path (``/v1internal:streamGenerateContent`` ->
    ``streamGenerateContent``); a path without ``:`` is its own endpoint name."""
    return path.rsplit(":", 1)[1] if ":" in path else path


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError:
        logger.warning("agy relay: %s is not an integer; using %d", name, default)
        return default


def _spill_dir() -> Path:
    """This app's spill directory; never the Claude Code relay's (its spill files are replayed there)."""
    path = Path(os.environ.get("LLM_RELAY_AGY_SPILL_DIR") or DEFAULT_SPILL_DIR).expanduser()
    cc_dir = Path(os.environ.get("LLM_RELAY_SPILL_DIR", str(Path.home() / ".llm-relay"))).expanduser()
    if path.resolve() == cc_dir.resolve():
        path = path / "antigravity"
        logger.warning("agy relay: LLM_RELAY_AGY_SPILL_DIR equals the Claude Code relay's; using %s", path)
    return path


def _display_url(url: str) -> str:
    """scheme://host[:port]/path of a URL -- no userinfo or query, which could carry credentials."""
    try:
        parsed = httpx.URL(url)
        port = ":{}".format(parsed.port) if parsed.port else ""
        return "{}://{}{}{}".format(parsed.scheme, parsed.host, port, parsed.path.rstrip("/"))
    except Exception:
        return "<unparsable>"


def _no_cookies() -> http.cookiejar.CookieJar:
    """A jar that accepts nothing: httpx would otherwise store upstream Set-Cookie headers and
    add them to later requests, so the relay would no longer be transparent."""
    return http.cookiejar.CookieJar(policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))


def _forward_headers(raw: List[Tuple[bytes, bytes]]) -> List[Tuple[bytes, bytes]]:
    """Request headers for the upstream: hop-by-hop ones dropped, accept-encoding forced to identity.

    identity keeps the response readable without decompression (cf. ``_forward`` in
    ``llm_relay.proxy.proxy``); duplicates and order of the other headers are kept.
    """
    headers = [(name, value) for name, value in raw if name.lower() not in _HOP_BY_HOP]
    headers = [(name, value) for name, value in headers if name.lower() != b"accept-encoding"]
    headers.append((b"accept-encoding", b"identity"))
    return headers


def _response_headers(raw: List[Tuple[bytes, bytes]]) -> List[Tuple[bytes, bytes]]:
    return [(name.lower(), value) for name, value in raw if name.lower() not in _HOP_BY_HOP]


class CaptureConnectionError(Exception):
    """Raised by a capture job when its DB connection no longer answers, so the writer replaces it.

    Carries the original exception type only: database messages can quote row values.
    """


class _LRU:
    """Bounded mapping with least-recently-used eviction (writer thread only)."""

    def __init__(self, maxsize: int) -> None:
        self._data: OrderedDict = OrderedDict()
        self._maxsize = max(1, maxsize)

    def get(self, key: str) -> Any:
        value = self._data.get(key)
        if value is not None:
            self._data.move_to_end(key)
        return value

    def put(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)


class _Flight:
    """One forwarded request: what the event loop saw, handed to the capture job at the end.

    Holds the request body only for the generate endpoints within the capture limit.
    The writer thread fills the parse results and remembers which rows it already
    wrote, so a job retried by the writer does not write them twice. ``repr`` (what a
    spill record shows) names the endpoint only -- no payload, no headers.
    """

    __slots__ = (
        "endpoint", "kind", "t0", "request_body_bytes", "body", "body_too_large", "status_code", "is_stream",
        "encoded", "acc", "collect_limit", "collected", "collect_overflow", "response_bytes", "stream_end",
        "latency_ms", "error", "done", "parsed", "req", "resp", "quota", "row_written", "request_row_id",
        "turn_done", "quota_done",
    )

    def __init__(self, endpoint: str, t0: float, request_body_bytes: int) -> None:
        self.endpoint = endpoint
        if endpoint in GENERATE_ENDPOINTS:
            self.kind = "generate"
        elif endpoint == QUOTA_ENDPOINT:
            self.kind = "quota"
        else:
            self.kind = "other"
        self.t0 = t0
        self.request_body_bytes = request_body_bytes
        self.body: Optional[bytes] = None
        self.body_too_large = False
        self.status_code: Optional[int] = None
        self.is_stream = False
        self.encoded = False
        self.acc: Optional[wire.SSEAccumulator] = None
        self.collect_limit = 0  # > 0: keep up to this many response bytes for parsing
        self.collected: Optional[bytearray] = None
        self.collect_overflow = False
        self.response_bytes = 0
        self.stream_end: Optional[str] = None
        self.latency_ms: Optional[float] = None
        self.error: Optional[str] = None
        self.done = False
        # Writer thread side.
        self.parsed = False
        self.req: Optional[wire.AgyRequest] = None
        self.resp: Optional[wire.AgyResponse] = None
        self.quota: Optional[List[Dict[str, Any]]] = None
        self.row_written = False
        self.request_row_id: Optional[int] = None
        self.turn_done = False
        self.quota_done = False

    def held_bytes(self) -> int:
        """Bytes this capture keeps alive while it waits for the writer (the queue's byte cap counts them)."""
        held = len(self.body) if self.body is not None else 0
        if self.acc is not None or self.collected is not None:
            held += self.response_bytes
        return held

    def __repr__(self) -> str:
        return "<agy capture {} status={} end={}>".format(self.endpoint, self.status_code, self.stream_end)


class _RelayResponse(StreamingResponse):
    """The upstream response relayed chunk by chunk, with a teardown that always runs.

    When the client goes away, Starlette stops iterating the body generator and leaves
    it suspended at its ``yield``; its ``finally`` would then only run whenever the
    generator is garbage collected. ``__call__`` closes it explicitly instead, and
    records the request itself if the generator never started.
    """

    def __init__(self, relay: _Relay, flight: _Flight, upstream: httpx.Response) -> None:
        super().__init__(relay._relay_body(flight, upstream), status_code=upstream.status_code)
        # raw_headers keeps repeated headers (set-cookie, ...), which a Mapping would merge.
        self.raw_headers = _response_headers(upstream.headers.raw)
        self._relay = relay
        self._flight = flight
        self._upstream = upstream

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except ClientDisconnect:
            pass  # the client is gone; the teardown below records it
        finally:
            with anyio.CancelScope(shield=True):
                with contextlib.suppress(Exception):
                    await self.body_iterator.aclose()  # type: ignore[union-attr]
            # No-op when the generator's own finally already ran (the usual case).
            self._relay._finish(self._flight, "client_disconnect")
            with anyio.move_on_after(CLOSE_TIMEOUT_S, shield=True):
                with contextlib.suppress(Exception):
                    await self._upstream.aclose()


class _Relay:
    """Configuration, shared client and writer, counters and the writer-thread capture state."""

    def __init__(
        self,
        *,
        upstream_transport: Optional[httpx.AsyncBaseTransport],
        writer: Any,
        conn_factory: Optional[Callable[[], Any]],
    ) -> None:
        self.upstream = os.environ.get("LLM_RELAY_AGY_UPSTREAM") or DEFAULT_UPSTREAM
        self.capture = _env_flag("LLM_RELAY_AGY_CAPTURE", True)
        self.max_capture_bytes = _env_int("LLM_RELAY_AGY_MAX_CAPTURE_BYTES", DEFAULT_MAX_CAPTURE_BYTES)
        self.spill_dir = _spill_dir()
        self._transport = upstream_transport
        self._writer = writer
        self._conn_factory = conn_factory or _default_conn_factory
        self._client: Optional[httpx.AsyncClient] = None
        self._in_flight = 0  # event loop only
        # Counters are bumped from the event loop and the writer thread.
        self._lock = threading.Lock()
        self._counters: Dict[str, int] = {name: 0 for name in _COUNTERS}
        self._error_kinds: set = set()
        # Writer-thread state (the job is the only reader and writer).
        self._sessions = _LRU(SESSION_STATE_MAX)  # conversation id -> (turn_number, total_messages, fingerprint)
        self._quota_last: Dict[str, Tuple[Any, float]] = {}  # bucket id -> (remaining_fraction, monotonic) written
        self._schema: Optional[Dict[str, bool]] = None
        self._schema_checked = 0.0

    # -- lifecycle --

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            kwargs: Dict[str, Any] = {
                "base_url": self.upstream,
                "timeout": UPSTREAM_TIMEOUT,
                "limits": UPSTREAM_LIMITS,
                "cookies": _no_cookies(),
            }
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    def _get_writer(self) -> Any:
        if self._writer is None:
            self._writer = DBWriter(conn_factory=self._conn_factory, spill_dir=self.spill_dir)
        return self._writer

    def startup(self) -> None:
        self._get_client()
        if self.capture:
            self._get_writer().start()
        logger.info("agy relay: upstream %s, capture %s", _display_url(self.upstream), "on" if self.capture else "off")

    async def shutdown(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()
        if self.capture and self._writer is not None:
            try:
                self._writer.stop()  # drains the queue; leftovers go to this app's spill directory
            except Exception as exc:
                logger.warning("agy relay: writer stop failed (%s)", type(exc).__name__)

    # -- counters --

    def _count(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counters[name] += n

    def _capture_error(self, stage: str, exc: BaseException) -> None:
        kind = "{}:{}".format(stage, type(exc).__name__)
        with self._lock:
            self._counters["capture_errors"] += 1
            first = kind not in self._error_kinds
            self._error_kinds.add(kind)
        if first:
            # Type only: exception messages can quote bodies or row values.
            logger.warning(
                "agy capture: %s failed with %s (counted in capture_errors; further ones of this kind are not logged)",
                stage, type(exc).__name__,
            )

    # -- ASGI entry (the router's default: every path and method) --

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1000})
            return
        if scope["method"] == "GET" and scope["path"] == "/_health":
            response: Response = self.health()
        else:
            response = await self.forward(Request(scope, receive))
        await response(scope, receive, send)

    def health(self) -> JSONResponse:
        with self._lock:
            counters = dict(self._counters)
        snapshot: Any = None
        if self._writer is not None:
            try:
                snapshot = self._writer.snapshot()
            except Exception as exc:
                snapshot = {"error": type(exc).__name__}
        schema = self._schema
        return JSONResponse(
            {
                "status": "ok",
                "provider": PROVIDER,
                "upstream": _display_url(self.upstream),
                "capture": self.capture,
                "in_flight": self._in_flight,
                "counters": counters,
                "schema": dict(schema) if schema is not None else None,
                "db_writer": snapshot,
            }
        )

    # -- forwarding (event loop) --

    async def forward(self, request: Request) -> Response:
        t0 = time.monotonic()
        try:
            body = await request.body()
        except ClientDisconnect:
            return Response(status_code=400)  # gone before its body arrived; nothing was forwarded
        self._count("requests")
        flight = _Flight(endpoint_name(request.scope["path"]), t0, len(body))
        self._in_flight += 1
        if self.capture:
            if len(body) > self.max_capture_bytes:
                flight.body_too_large = True
                flight.error = "capture skipped: body too large"
            elif flight.kind == "generate":
                flight.body = body
        client = self._get_client()
        try:
            upstream = await client.send(self._upstream_request(client, request.scope, body), stream=True)
        except httpx.TimeoutException as exc:
            return self._upstream_failure(flight, 504, "timed out", exc)
        except asyncio.CancelledError:
            self._finish(flight, "client_disconnect")
            raise
        except Exception as exc:  # connect / protocol / any other failure before response headers
            return self._upstream_failure(flight, 502, "request failed", exc)
        del body
        try:
            self._observe_headers(flight, upstream)
            return _RelayResponse(self, flight, upstream)
        except BaseException:
            self._finish(flight, "relay_error")
            with anyio.move_on_after(CLOSE_TIMEOUT_S, shield=True):
                with contextlib.suppress(Exception):
                    await upstream.aclose()
            raise

    def _upstream_request(self, client: httpx.AsyncClient, scope: Scope, body: bytes) -> httpx.Request:
        """Same method, raw path and query string on the upstream base URL; headers per ``_forward_headers``."""
        raw_path = (scope.get("raw_path") or scope["path"].encode("utf-8")).split(b"?", 1)[0]
        query = scope.get("query_string") or b""
        target = client.base_url.raw_path.rstrip(b"/") + raw_path + (b"?" + query if query else b"")
        # An absolute URL: httpx does not re-merge it with base_url, so the path reaches the upstream as sent.
        url = client.base_url.copy_with(raw_path=target)
        headers = _forward_headers(scope["headers"])
        request = client.build_request(scope["method"], url, headers=headers, content=body)
        sent = {name.lower() for name, _ in headers}
        for name in _CLIENT_DEFAULT_HEADERS:
            if name.encode("ascii") not in sent and name in request.headers:
                del request.headers[name]
        return request

    def _upstream_failure(self, flight: _Flight, status: int, what: str, exc: BaseException) -> Response:
        self._count("upstream_errors")
        logger.warning("agy relay: upstream %s for %s (%s); answering %d", what, flight.endpoint,
                       type(exc).__name__, status)
        message = "relay: upstream {} ({})".format(what, type(exc).__name__)
        flight.status_code = status
        flight.error = "{}: {}".format(RELAY_ERROR_STATUS, message) if not flight.error else "{}; {}: {}".format(
            flight.error, RELAY_ERROR_STATUS, message)
        self._finish(flight, "upstream_error")
        return JSONResponse(
            {"error": {"code": status, "message": message, "status": RELAY_ERROR_STATUS}}, status_code=status
        )

    def _observe_headers(self, flight: _Flight, upstream: httpx.Response) -> None:
        flight.status_code = upstream.status_code
        content_type = upstream.headers.get("content-type", "")
        flight.is_stream = content_type.split(";", 1)[0].strip().lower() == "text/event-stream"
        encoding = upstream.headers.get("content-encoding", "").strip().lower()
        flight.encoded = encoding not in ("", "identity")
        if not self.capture or flight.encoded:
            return  # compressed despite accept-encoding: identity -- relayed as is, not parsed
        if flight.kind == "generate":
            if flight.is_stream:
                flight.acc = wire.SSEAccumulator()
            else:
                flight.collect_limit = self.max_capture_bytes
        elif flight.kind == "quota" and upstream.status_code == 200:
            flight.collect_limit = QUOTA_BODY_MAX
        if flight.collect_limit:
            flight.collected = bytearray()

    def _observe_chunk(self, flight: _Flight, chunk: bytes) -> None:
        flight.response_bytes += len(chunk)
        try:
            if flight.acc is not None:
                flight.acc.feed(chunk)
            elif flight.collected is not None:
                if len(flight.collected) + len(chunk) > flight.collect_limit:
                    flight.collected = None
                    flight.collect_overflow = True
                else:
                    flight.collected += chunk
        except Exception as exc:
            self._capture_error("observe", exc)

    async def _relay_body(self, flight: _Flight, upstream: httpx.Response) -> AsyncIterator[bytes]:
        """Yield upstream chunks as they arrive; record the request when the stream ends, however it ends."""
        end = "client_disconnect"  # GeneratorExit at the yield, or cancellation while waiting upstream
        try:
            async for chunk in upstream.aiter_raw():
                if not chunk:
                    continue
                yield chunk
                if self.capture:
                    self._observe_chunk(flight, chunk)
            end = "completed"
        except (GeneratorExit, asyncio.CancelledError):
            raise
        except Exception as exc:
            end = "upstream_error"
            self._count("upstream_errors")
            logger.warning("agy relay: upstream failed mid-stream for %s (%s)", flight.endpoint, type(exc).__name__)
            raise  # the client must see a broken stream, not a short one that looks complete
        finally:
            # Submit first, synchronously: an await here can be cut short during teardown.
            self._finish(flight, end)
            with anyio.move_on_after(CLOSE_TIMEOUT_S, shield=True):
                with contextlib.suppress(Exception):
                    await upstream.aclose()

    def _finish(self, flight: _Flight, end: str) -> None:
        """Once per request: stop the clock and hand the capture to the writer. Sync; never raises."""
        if flight.done:
            return
        flight.done = True
        self._in_flight -= 1
        if end == "completed" and flight.encoded:
            end = "unparsed_encoding"
        flight.stream_end = end
        flight.latency_ms = (time.monotonic() - flight.t0) * 1000.0
        if not self.capture:
            return
        try:
            self._get_writer().submit(self._capture_job, size_hint=flight.held_bytes(), flight=flight)
        except Exception as exc:
            self._capture_error("submit", exc)

    # -- capture job (writer thread) --

    def _capture_job(self, conn: Any, *, flight: _Flight) -> None:
        """Parse the request and write its rows: request, then turn or quota. Runs on the writer thread."""
        stage = "schema"
        try:
            if not self._schema_ready(conn):
                return
            stage = "parse"
            self._parse(flight)
            stage = "request"
            if not flight.row_written:
                flight.request_row_id = store.log_request(conn, **self._request_columns(flight))
                flight.row_written = True
            if flight.kind == "generate" and not flight.turn_done:
                stage = "turn"
                if self._turn_wanted(flight):
                    self._write_turn(conn, flight)
                flight.turn_done = True
            if flight.kind == "quota" and not flight.quota_done:
                stage = "quota"
                self._write_quota(conn, flight)
                flight.quota_done = True
        except Exception as exc:
            self._capture_error(stage, exc)
            if not self._connection_usable(conn):
                # The writer replaces its connection and retries the job; the flight remembers
                # which rows are already written. The original exception is not attached to the
                # message because database errors can quote row values.
                raise CaptureConnectionError(
                    "agy capture: database connection unusable ({})".format(type(exc).__name__)
                ) from None

    def _schema_ready(self, conn: Any) -> bool:
        cached = self._schema
        stale = cached is None or (
            not all(cached.values()) and time.monotonic() - self._schema_checked >= SCHEMA_RECHECK_S
        )
        if stale:
            self._note_schema(store.tables_present(conn))
        if self._schema is not None and all(self._schema.values()):
            return True
        self._count("capture_skipped_no_schema")
        return False

    def _note_schema(self, present: Dict[str, bool]) -> None:
        previous = None if self._schema is None else all(self._schema.values())
        self._schema = dict(present)
        self._schema_checked = time.monotonic()
        ok = all(present.values())
        if ok == previous:
            return
        if not ok:
            missing = ", ".join(sorted(name for name, found in present.items() if not found))
            logger.warning("agy capture paused: provider tables missing (%s); forwarding continues", missing)
        elif previous is False:
            logger.warning("agy capture resumed: provider tables present")

    def _connection_usable(self, conn: Any) -> bool:
        """After a failed write: does the connection still answer? Refreshes the schema cache on the way."""
        try:
            self._note_schema(store.tables_present(conn))
            return True
        except Exception:
            return False

    def _parse(self, flight: _Flight) -> None:
        """Parse once (a retried job reuses the result) and drop the raw bytes."""
        if flight.parsed:
            return
        flight.parsed = True
        try:
            if flight.kind == "generate":
                if flight.body is not None:
                    flight.req = wire.parse_request(flight.body, flight.endpoint)
                if flight.acc is not None:
                    flight.resp = flight.acc.finish()
                elif flight.collected is not None:
                    flight.resp = wire.parse_json_response(bytes(flight.collected))
            elif flight.kind == "quota" and flight.collected is not None and flight.status_code == 200:
                flight.quota = wire.parse_quota_summary(bytes(flight.collected))
        finally:
            flight.body = None
            flight.acc = None
            flight.collected = None

    @staticmethod
    def _parent(req: wire.AgyRequest) -> Optional[str]:
        root = req.root_conversation_id
        return root if root and root != req.conversation_id else None

    def _request_columns(self, flight: _Flight) -> Dict[str, Any]:
        columns: Dict[str, Any] = {
            "provider": PROVIDER,
            "endpoint": flight.endpoint,
            "status_code": flight.status_code,
            "latency_ms": flight.latency_ms,
            "is_stream": flight.is_stream,
            "stream_end": flight.stream_end,
            "request_body_bytes": flight.request_body_bytes,
            "response_bytes": flight.response_bytes,
        }
        errors = [flight.error] if flight.error else []
        req = flight.req
        if req is not None:
            columns.update(
                session_id=req.conversation_id,
                parent_session_id=self._parent(req),
                request_type=req.request_type,
                request_id=req.request_id,
                model=req.model,
                thinking_level=req.thinking_level,
                message_count=req.message_count,
                step_index=req.step_index,
                client_session_id=req.client_session_id,
            )
        resp = flight.resp
        if resp is not None:
            columns.update(wire.usage_columns(resp.usage))
            columns["model_version"] = resp.model_version
            columns["raw_usage"] = resp.usage
            if resp.error:
                errors.append(resp.error)
        if errors:
            columns["error"] = "; ".join(errors)
        return columns

    @staticmethod
    def _turn_wanted(flight: _Flight) -> bool:
        req, resp = flight.req, flight.resp
        return (
            req is not None
            and req.request_type == "agent"
            and bool(req.conversation_id)
            and flight.status_code == 200
            and resp is not None
            and resp.error is None
            and not flight.body_too_large
            # A response cut short (client left, upstream failed) is not a turn: agy resends
            # the same history afterwards and the partial answer would be stored as its own turn.
            and flight.stream_end == "completed"
        )

    def _write_turn(self, conn: Any, flight: _Flight) -> None:
        req, resp = flight.req, flight.resp
        assert req is not None and resp is not None and req.conversation_id
        session_id = req.conversation_id
        contents = req.contents
        fingerprint = wire.first_message_fingerprint(contents)
        previous = self._sessions.get(session_id)
        if previous is None:
            seeded = store.get_last_turn_state(conn, PROVIDER, session_id)
            if seeded is not None:
                previous = (seeded["turn_number"], seeded["total_message_count"], seeded["first_fingerprint"])
        count = req.message_count
        if previous is not None and previous[2] == fingerprint and count >= previous[1] > 0:
            mode, previous_total = "delta", previous[1]
        else:
            # First turn, another conversation under the same id, or history shrunk by a compaction.
            mode, previous_total = "full", 0
        turn_number = (previous[0] if previous is not None else 0) + 1
        store.log_turn(
            conn,
            provider=PROVIDER,
            session_id=session_id,
            turn_number=turn_number,
            storage_mode=mode,
            # normalize_contents maps contents one to one, so this equals normalize_contents(contents)[n:].
            request_messages=wire.normalize_contents(contents[previous_total:]),
            response_message=resp.blocks,
            thinking_blocks=resp.thinking,
            model=req.model,
            parent_session_id=self._parent(req),
            request_row_id=flight.request_row_id,
            request_id=req.request_id,
            total_message_count=count,
            previous_message_count=previous_total,
            first_fingerprint=fingerprint,
            step_index=req.step_index,
            request_size_bytes=flight.request_body_bytes,
            response_size_bytes=flight.response_bytes,
            last_user_prompt=wire.last_user_prompt(contents),
        )
        # Only after the row is in: a failed write leaves the previous state for the next turn.
        self._sessions.put(session_id, (turn_number, count, fingerprint))
        self._count("turns_captured")

    def _quota_changed(self, bucket: Dict[str, Any], now: float) -> bool:
        last = self._quota_last.get(bucket["bucket_id"], _MISSING)
        if last is _MISSING:
            return True  # first sight of the bucket by this process
        last_fraction, last_written = last
        fraction = bucket.get("remaining_fraction")
        if fraction == last_fraction:
            return False  # reset_time alone rolls with the clock on unused buckets
        if not isinstance(fraction, (int, float)) or not isinstance(last_fraction, (int, float)):
            return True  # a value appeared or disappeared
        if abs(fraction - last_fraction) >= QUOTA_STEP:
            return True
        return now - last_written >= QUOTA_MIN_INTERVAL_S

    def _write_quota(self, conn: Any, flight: _Flight) -> None:
        """Write the buckets whose remaining fraction really moved since this process last wrote them."""
        now = time.monotonic()
        changed = [bucket for bucket in flight.quota or () if self._quota_changed(bucket, now)]
        if not changed:
            return
        inserted = store.log_quota(conn, provider=PROVIDER, buckets=changed)
        for bucket in changed:
            self._quota_last[bucket["bucket_id"]] = (bucket.get("remaining_fraction"), now)
        self._count("quota_rows", inserted)


def _default_conn_factory() -> Any:
    """The relay database connection with the provider tables ensured (SQLite only; PG is migrated)."""
    from llm_relay.proxy.db import get_conn  # lazy: importing it loads the repo env files

    conn = get_conn()
    store.ensure_schema(conn)
    return conn


def create_app(
    *,
    upstream_transport: Optional[httpx.AsyncBaseTransport] = None,
    writer: Any = None,
    conn_factory: Optional[Callable[[], Any]] = None,
) -> Starlette:
    """Build the relay app; configuration comes from the ``LLM_RELAY_AGY_*`` environment, read now.

    ``upstream_transport`` replaces the network (tests pass ``httpx.MockTransport``);
    ``writer`` replaces the private ``DBWriter`` (``submit`` / ``start`` / ``stop`` /
    ``snapshot``); ``conn_factory`` is what the default writer connects with.
    """
    relay = _Relay(upstream_transport=upstream_transport, writer=writer, conn_factory=conn_factory)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        relay.startup()
        try:
            yield
        finally:
            await relay.shutdown()

    app = Starlette(routes=[], lifespan=lifespan)
    # No routes: the router hands every request (any path, any method) to its default app.
    app.router.default = relay
    app.state.relay = relay
    return app


app = create_app()
