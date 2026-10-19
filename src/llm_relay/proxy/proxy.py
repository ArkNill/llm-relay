"""Transparent reverse proxy -- forwards all requests to Anthropic API, logs usage."""

from __future__ import annotations

import asyncio
import codecs
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any


def _load_env_files(base: Path | None = None) -> None:
    """Load .env.local then .env.public (2-tier env) for native (non-Docker) runs.

    Only sets variables that are not already in the environment, so explicit
    env vars and Docker env_file both take precedence, and .env.local (loaded
    first) overrides .env.public. Before 2026-09-29 the files were loaded in
    the opposite order, so outside systemd (whose EnvironmentFile order was
    already right) a key present in both files kept the public value.
    """
    if base is None:
        base = Path(__file__).resolve().parent.parent.parent.parent  # repo root
    for name in (".env.local", ".env.public"):
        env_path = base / name
        if not env_path.is_file():
            continue
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                os.environ.setdefault(key, value)


_load_env_files()

import httpx  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.middleware import Middleware  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import Response, StreamingResponse  # noqa: E402
from starlette.routing import Mount, Route  # noqa: E402

from .db import get_conn, log_budget_event, log_microcompact, log_request  # noqa: E402
from .dbwriter import get_writer  # noqa: E402

# GrowthBook intercept (local-only, file excluded via .gitignore)
try:
    from .db import log_intercept_event
    from .growthbook_intercept import apply_overrides, is_intercept_enabled, should_intercept
    _gb_intercept_available = True
except ImportError:
    _gb_intercept_available = False

# CC cache fix (opt-in, LLM_RELAY_CACHE_FIX=1)
_cache_fix_available = False
if os.getenv("LLM_RELAY_CACHE_FIX", "0") == "1":
    try:
        from .cc_cache_fix import normalize_request as _normalize_cache
        from .db import log_cache_diagnostic
        _cache_fix_available = True
    except ImportError:
        pass

# CC effort fix (opt-out, LLM_RELAY_EFFORT_FIX=0)
_effort_fix_available = False
if os.getenv("LLM_RELAY_EFFORT_FIX", "1") == "1":
    try:
        from .cc_effort_fix import normalize_request as _normalize_effort
        _effort_fix_available = True
    except ImportError:
        pass

logger = logging.getLogger("llm-relay")
# Request start lines (see _RequestLog); INFO regardless of the main logger's level.
_access_log = logging.getLogger("llm-relay.access")


def _setup_logging() -> None:
    """Give the relay's loggers an output when nothing else has configured logging.

    Under uvicorn only uvicorn's own loggers get handlers. The "llm-relay"
    logger propagated to a bare root logger, so its INFO diagnostics (cache
    fix, compaction, client gone before headers) never reached the journal
    and its warnings came out through logging's last-resort handler
    (2026-09-29: 0 INFO lines in two days of journal). A handler is installed
    once, only when neither root nor "llm-relay" has one -- the CLI's
    basicConfig is left alone. "llm-relay" keeps inheriting WARNING unless
    LLM_RELAY_LOG_LEVEL names a level; "llm-relay.access" runs at INFO.
    """
    if not logging.getLogger().handlers and not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
        logger.addHandler(handler)
    level_name = os.getenv("LLM_RELAY_LOG_LEVEL", "").strip().upper()
    if level_name:
        level = logging.getLevelName(level_name)
        logger.setLevel(level if isinstance(level, int) else logging.WARNING)
    _access_log.setLevel(logging.INFO)


_setup_logging()

UPSTREAM = os.getenv("LLM_RELAY_UPSTREAM", "https://api.anthropic.com")
WARN_READ_RATIO = float(os.getenv("LLM_RELAY_WARN_RATIO", "50.0"))

# Upstream connection pool (2026-09-28). The old cap of 20 was exceeded in
# production (31 concurrent streams on 9/21 when one session fanned out
# sub-agents); requests beyond the cap queued inside httpx for up to the pool
# timeout while the client saw nothing. A short pool timeout turns a residual
# exhaustion into a visible 503 that the client retries instead of a hang.
_MAX_CONNECTIONS = int(os.getenv("LLM_RELAY_MAX_CONNECTIONS", "100"))
_MAX_KEEPALIVE = int(os.getenv("LLM_RELAY_MAX_KEEPALIVE", "20"))
_POOL_TIMEOUT_S = float(os.getenv("LLM_RELAY_POOL_TIMEOUT_S", "30"))
# How often to check that the client is still connected while we wait for
# upstream response headers (pool wait + first byte).
_DISCONNECT_POLL_S = float(os.getenv("LLM_RELAY_DISCONNECT_POLL_S", "1.0"))

# Performance toggles (default OFF for low-overhead proxying).
# Set =1 to re-enable the corresponding diagnostic/compression path.
_SCAN_ENABLED = os.getenv("LLM_RELAY_SCAN_ENABLED", "0") == "1"
_TOKPRESS_ENABLED = os.getenv("LLM_RELAY_TOKPRESS", "0") == "1"
_SSE_PARSE_USAGE = os.getenv("LLM_RELAY_SSE_PARSE_USAGE", "1") == "1"

# Session history (opt-in, LLM_RELAY_HISTORY=1)
_HISTORY_ENABLED = os.getenv("LLM_RELAY_HISTORY", "0") == "1"
_HISTORY_RAW = os.getenv("LLM_RELAY_HISTORY_RAW", "0") == "1"

# Request bodies above this many bytes skip the cache-fix / effort-fix / tokpress
# rewrite: the original bytes go upstream unchanged and the request is flagged
# with rewrite_skipped in requests.raw_usage. 0 (default) = no limit. The
# rewrite reorders tools on most production requests (92% on 2026-09-29), so
# skipping it on one large request changes the prompt prefix and costs a cache
# miss; this is a valve for image-heavy bursts (379 requests over 8 MB in one
# day), not for normal traffic. History capture is never skipped.
_MAX_REWRITE_BYTES = int(os.getenv("LLM_RELAY_MAX_REWRITE_BYTES", "0"))

# One "request start" line per request (see _RequestLog); /_health is never logged.
_LOG_REQUEST_START = os.getenv("LLM_RELAY_LOG_REQUEST_START", "1") == "1"
# Requests currently inside the app (event-loop thread only, no lock needed):
# "total" = every HTTP request, "proxy" = /v1/ requests (a stream counts until it ends).
_in_flight = {"total": 0, "proxy": 0}

CLEARED_MARKER = "[Old tool result content cleared]"

# Per-tool result size caps (server-side configuration)
TOOL_CAPS = {
    "global": 50_000,
    "Bash": 30_000,
    "Grep": 20_000,
    "Read": 50_000,
    "Glob": 20_000,
    "Snip": 1_000,
}
AGGREGATE_CAP = 200_000  # aggregate tool result cap

_RATELIMIT_PREFIXES = ("x-ratelimit-", "anthropic-ratelimit-", "retry-after")

_tokpress_available = False
if _TOKPRESS_ENABLED:
    try:
        from tokpress.integrations.proxy import compress_tool_results
        _tokpress_available = True
    except ImportError:
        pass


def _try_compress(req_json: dict, body: bytes) -> bytes:
    """Compress tool results in-place if tokpress is enabled and available."""
    if not _tokpress_available:
        return body
    try:
        if compress_tool_results(req_json):
            return json.dumps(req_json).encode("utf-8")
    except Exception:
        logger.debug("tokpress compression failed, using original body", exc_info=True)
    return body


def _content_chars(content: Any) -> int:
    """Estimate character count of a tool result content block."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(str(b.get("text", ""))) if isinstance(b, dict) else len(str(b)) for b in content)
    return 0


def _scan_budget_enforcement(req_json: dict, session_id: str | None) -> None:
    """Scan request for tool result budget enforcement evidence."""
    messages = req_json.get("messages", [])
    if not messages:
        return

    tool_results = []  # (msg_index, tool_name, chars)

    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        content = msg.get("content")

        if role == "tool":
            chars = _content_chars(content)
            tool_results.append((i, "tool_response", chars))
        elif isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result":
                    result_content = block.get("content", "")
                    chars = _content_chars(result_content)
                    tool_results.append((i, "tool_result", chars))

    if not tool_results:
        return

    total_chars = sum(c for _, _, c in tool_results)

    for idx, tool_name, chars in tool_results:
        _cap = TOOL_CAPS.get(tool_name, TOOL_CAPS["global"])  # noqa: F841
        # Detect: very small results that likely were truncated,
        # or results ending with summary patterns
        truncated = False
        marker = ""

        if chars == 0:
            truncated = True
            marker = "empty"
        elif chars < 50 and total_chars > AGGREGATE_CAP:
            truncated = True
            marker = "suspiciously_small"

        if truncated:
            _db_writer.submit(
                log_budget_event,
                session_id=session_id,
                msg_index=idx,
                tool_name=tool_name,
                content_chars=chars,
                truncated=True,
                marker=marker,
            )

    if total_chars > 0:
        logger.debug(
            "📊 TOOL RESULT BUDGET: %d results, %d total chars (cap=%d) -- session %s",
            len(tool_results),
            total_chars,
            AGGREGATE_CAP,
            session_id or "unknown",
        )
        if total_chars > AGGREGATE_CAP:
            logger.warning(
                "⚠ BUDGET EXCEEDED: %d chars > %d cap -- budget enforcement likely active",
                total_chars,
                AGGREGATE_CAP,
            )


def _scan_microcompact(req_json: dict, session_id: str | None) -> None:
    """Scan request messages for signs of microcompact (cleared tool results)."""
    messages = req_json.get("messages", [])
    if not messages:
        return

    cleared_indices = []
    total_tool_results = 0

    for i, msg in enumerate(messages):
        if msg.get("role") != "tool":
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "tool_result":
                        total_tool_results += 1
                        result_content = block.get("content", "")
                        if isinstance(result_content, str) and CLEARED_MARKER in result_content:
                            cleared_indices.append(i)
                        elif isinstance(result_content, list):
                            for sub in result_content:
                                if isinstance(sub, dict) and CLEARED_MARKER in str(sub.get("text", "")):
                                    cleared_indices.append(i)
            continue

        # role == "tool"
        total_tool_results += 1
        content = msg.get("content", "")
        if isinstance(content, str) and CLEARED_MARKER in content:
            cleared_indices.append(i)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and CLEARED_MARKER in str(block.get("text", "")):
                    cleared_indices.append(i)

    if cleared_indices:
        logger.warning(
            "🔴 MICROCOMPACT DETECTED: %d/%d tool results cleared (msg indices: %s) -- session %s",
            len(cleared_indices),
            total_tool_results,
            cleared_indices[:10],
            session_id or "unknown",
        )
        _db_writer.submit(
            log_microcompact,
            session_id=session_id,
            cleared_count=len(cleared_indices),
            total_tool_results=total_tool_results,
            cleared_indices=cleared_indices,
            message_count=len(messages),
        )

_conn = None  # main-thread connection: read-only use (/_stats, /_recent, API routes)
_client = None
_db_writer = get_writer()  # every write goes through the background writer (dbwriter.py)


def _get_conn():
    global _conn
    if _conn is None:
        _conn = get_conn()
    return _conn


def _get_client():
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=UPSTREAM,
            timeout=httpx.Timeout(300.0, connect=30.0, pool=_POOL_TIMEOUT_S),
            limits=httpx.Limits(
                max_connections=_MAX_CONNECTIONS,
                max_keepalive_connections=_MAX_KEEPALIVE,
            ),
        )
    return _client


def _extract_usage(data: dict) -> dict:
    """Extract cache/token usage from API response."""
    usage = data.get("usage", {})
    return {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_creation": usage.get("cache_creation_input_tokens", 0),
        "cache_read": usage.get("cache_read_input_tokens", 0),
        "model": data.get("model"),
    }


def _apply_delta_usage(usage_acc: dict, delta_usage: dict) -> None:
    """Merge a streaming ``message_delta`` usage block into the accumulator.

    message_delta usage is cumulative for the whole response. output_tokens is
    always present. When the response used server-side tools (web_search,
    web_fetch), the input and cache counts grow with every tool iteration and
    only the final message_delta carries the totals -- message_start shows the
    first iteration only. Counts never decrease, so a present value replaces
    the accumulator only when it is larger. The tool call counts go to
    raw_usage (no schema change).
    """
    if not isinstance(delta_usage, dict):
        return
    out = delta_usage.get("output_tokens")
    if isinstance(out, int):
        usage_acc["output_tokens"] = out
    for src, dst in (
        ("input_tokens", "input_tokens"),
        ("cache_creation_input_tokens", "cache_creation"),
        ("cache_read_input_tokens", "cache_read"),
    ):
        val = delta_usage.get(src)
        if isinstance(val, int) and val > usage_acc.get(dst, 0):
            usage_acc[dst] = val
    detail = delta_usage.get("cache_creation")
    if isinstance(detail, dict):
        for src, dst in (
            ("ephemeral_1h_input_tokens", "ephemeral_1h_tokens"),
            ("ephemeral_5m_input_tokens", "ephemeral_5m_tokens"),
        ):
            val = detail.get(src)
            if isinstance(val, int) and val > usage_acc.get(dst, 0):
                usage_acc[dst] = val
    stu = delta_usage.get("server_tool_use")
    if isinstance(stu, dict):
        for key in ("web_search_requests", "web_fetch_requests"):
            val = stu.get(key)
            if isinstance(val, int):
                usage_acc[key] = val


def _extract_ratelimit_headers(headers) -> dict[str, str] | None:
    """Extract rate limit related headers from API response."""
    rl = {k: v for k, v in headers.items() if k.lower().startswith(_RATELIMIT_PREFIXES)}
    return rl or None


def _warn_if_poor(usage: dict, endpoint: str) -> None:
    total = usage["cache_creation"] + usage["cache_read"]
    if total > 0:
        ratio = usage["cache_read"] / total * 100
        if ratio < WARN_READ_RATIO:
            logger.warning(
                "⚠ LOW CACHE HIT: %.1f%% (read=%d, creation=%d) -- %s",
                ratio,
                usage["cache_read"],
                usage["cache_creation"],
                endpoint,
            )


class _ClientGone(Exception):
    """The client closed its connection before upstream response headers arrived."""


def _session_of(headers: dict) -> str | None:
    return headers.get("x-claude-code-session-id") or headers.get("x-session-id")


async def _read_body(request: Request) -> bytes:
    """Read the request body without leaving a cached copy on the request.

    ``request.body()`` stores the bytes in ``request._body`` for the lifetime
    of the request, which for a streamed response is the whole upstream
    stream. Once the cache/effort fix has rewritten the body that copy is dead
    weight: with it, five concurrent 24 MB image requests peaked at 510 MB
    (2026-09-29 isolated measurement). Reading the stream directly leaves the
    proxy's local variable as the only reference, so the original is released
    as soon as the rewritten body replaces it.

    A large upload arrives as hundreds of 64 KB chunks. They are appended to
    one growing buffer as they come instead of being collected in a list and
    joined at the end: the list held a second copy of the body until the join,
    and its freed pieces fragmented the heap. A body that arrives in a single
    chunk is returned as is.
    """
    first = b""
    buf = None
    async for chunk in request.stream():
        if not chunk:
            continue
        if buf is not None:
            buf += chunk
        elif first:
            buf = bytearray(first)
            buf += chunk
        else:
            first = chunk
    if buf is None:
        return first
    return bytes(buf)


async def _send_watching_client(client: httpx.AsyncClient, req: httpx.Request, request: Request) -> httpx.Response:
    """``client.send(stream=True)`` that is cancelled if the client disconnects first.

    Before 2026-09-28 a request waiting in the httpx pool or for the upstream
    first byte kept running after the client gave up and retried, so the
    original and the retry both held pool slots and both reached upstream
    (isolated test: client left at 2s, relay held the upstream connection until
    the 12s response). Polling ``request.is_disconnected()`` costs nothing on
    the happy path because ``asyncio.wait`` returns as soon as headers arrive.
    """
    send_task = asyncio.ensure_future(client.send(req, stream=True))
    try:
        while True:
            done, _pending = await asyncio.wait({send_task}, timeout=_DISCONNECT_POLL_S)
            if done:
                return send_task.result()
            if await request.is_disconnected():
                send_task.cancel()
                try:
                    await send_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
                raise _ClientGone()
    except asyncio.CancelledError:
        send_task.cancel()
        raise


def _client_gone_response(headers: dict, path: str, t0: float, body_bytes: int, is_stream: bool) -> Response:
    """Record a client disconnect that happened before upstream headers; answer 499."""
    latency_ms = (time.time() - t0) * 1000
    sid = _session_of(headers)
    logger.info(
        "client disconnected before upstream headers after %.0f ms -- %s session=%s",
        latency_ms, path, sid or "unknown",
    )
    _db_writer.submit(
        log_request,
        session_id=sid,
        model=None,
        status_code=499,
        latency_ms=latency_ms,
        endpoint=path,
        is_stream=is_stream,
        raw_usage={"stream_end": "client_disconnect_before_headers"},
        request_body_bytes=body_bytes,
    )
    return Response(status_code=499)


def _upstream_error_response(
    exc: Exception, headers: dict, path: str, t0: float, body_bytes: int, is_stream: bool,
) -> Response:
    """Turn an httpx transport failure into a structured API error the client retries.

    These used to surface as an opaque 500 and were never recorded.
    """
    if isinstance(exc, httpx.PoolTimeout):
        status = 503
        msg = "connection pool exhausted (waited {:.0f}s for a free upstream slot)".format(_POOL_TIMEOUT_S)
    elif isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError)):
        status, msg = 502, "could not connect to upstream"
    elif isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout)):
        status, msg = 504, "upstream did not respond in time"
    else:
        status, msg = 502, "upstream request failed ({})".format(type(exc).__name__)
    latency_ms = (time.time() - t0) * 1000
    sid = _session_of(headers)
    logger.warning("upstream %s on %s after %.0f ms -- %s: %s", type(exc).__name__, path, latency_ms, msg, exc)
    _db_writer.submit(
        log_request,
        session_id=sid,
        model=None,
        status_code=status,
        latency_ms=latency_ms,
        endpoint=path,
        is_stream=is_stream,
        raw_usage={"stream_end": "upstream_error", "error": type(exc).__name__},
        request_body_bytes=body_bytes,
    )
    body = json.dumps({"type": "error", "error": {"type": "api_error", "message": "llm-relay: " + msg}})
    return Response(body, status_code=status, media_type="application/json")


async def _proxy(request: Request) -> Response:
    """Forward request to upstream, log usage, return response."""
    path = request.url.path

    # Only proxy Anthropic API paths (/v1/messages, /v1/complete, etc.)
    # Reject everything else to prevent Chrome devtools, favicon, and other
    # spurious requests from being forwarded upstream (causes ConnectTimeout
    # hangs when upstream is unreachable).
    if not path.startswith("/v1/"):
        return Response(
            json.dumps({"error": "Not found", "detail": "Only /v1/ API paths are proxied"}),
            status_code=404,
            media_type="application/json",
        )

    client = _get_client()
    query = str(request.url.query)
    url = f"{path}?{query}" if query else path

    body = await _read_body(request)
    headers = dict(request.headers)
    # Strip hop-by-hop headers only. Preserve accept-encoding so the upstream
    # leg (internet) stays gzip-compressed -- httpx auto-decompresses locally and
    # we strip content-encoding on the response to send plain bytes to the
    # client over loopback (bytes saved upstream, not re-encoded on loopback).
    for h in ("host", "transfer-encoding", "connection", "content-length"):
        headers.pop(h, None)

    # The body is parsed exactly once (2026-09-29). Stream detection, the
    # cache/effort rewrite and history capture all work from this parse: history
    # gets a copy with inline base64 replaced by size markers, and the full
    # parse is dropped before the upstream request starts, so a streamed
    # request's closure no longer holds a 25 MB image request for the whole
    # stream (the stream path used to parse the body a second time for that).
    is_stream = False
    req_json = None
    history_json = None
    history_bytes = 0
    rewrite_skipped = False
    body_bytes = len(body) if body else 0
    if body:
        try:
            req_json = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            req_json = None
    if isinstance(req_json, dict):
        is_stream = bool(req_json.get("stream", False))
        if req_json.get("messages"):
            sid = headers.get("x-claude-code-session-id") or headers.get("x-session-id")
            if _SCAN_ENABLED:
                _scan_microcompact(req_json, sid)
                _scan_budget_enforcement(req_json, sid)
            if _MAX_REWRITE_BYTES and body_bytes > _MAX_REWRITE_BYTES:
                rewrite_skipped = True
                logger.info(
                    "rewrite skipped: %d B body over LLM_RELAY_MAX_REWRITE_BYTES=%d -- %s session=%s",
                    body_bytes, _MAX_REWRITE_BYTES, path, (sid or "unknown")[:8],
                )
            else:
                if _cache_fix_available and path.startswith("/v1/messages"):
                    cf_modified, cf_diag = _normalize_cache(req_json, headers)
                    if cf_modified:
                        body = json.dumps(req_json, ensure_ascii=False).encode("utf-8")
                    if cf_diag:
                        _db_writer.submit(log_cache_diagnostic, session_id=sid, **cf_diag)
                if _effort_fix_available and path.startswith("/v1/messages"):
                    ef_modified, _ef_original = _normalize_effort(req_json)
                    if ef_modified:
                        body = json.dumps(req_json, ensure_ascii=False).encode("utf-8")
                if _tokpress_available:
                    body = _try_compress(req_json, body)
            if _HISTORY_ENABLED and path.startswith("/v1/messages"):
                from .history import history_view
                history_json, history_bytes = history_view(req_json, body_bytes)
    req_json = None

    t0 = time.time()

    if is_stream:
        return await _proxy_stream(
            client, request, url, headers, body, path, t0, body_bytes,
            history_json=history_json, history_bytes=history_bytes, rewrite_skipped=rewrite_skipped,
        )

    # Non-streaming
    req = client.build_request(method=request.method, url=url, headers=headers, content=body)
    try:
        upstream_resp = await _send_watching_client(client, req, request)
    except _ClientGone:
        return _client_gone_response(headers, path, t0, body_bytes, is_stream=False)
    except httpx.HTTPError as exc:
        return _upstream_error_response(exc, headers, path, t0, body_bytes, is_stream=False)
    try:
        resp_body = await upstream_resp.aread()
    except httpx.HTTPError as exc:
        return _upstream_error_response(exc, headers, path, t0, body_bytes, is_stream=False)
    finally:
        await upstream_resp.aclose()

    latency_ms = (time.time() - t0) * 1000

    # Parse and log usage
    rl_headers = _extract_ratelimit_headers(upstream_resp.headers)

    # Feature flag routing (local-only extension -- no-op if module absent)
    if _gb_intercept_available and is_intercept_enabled() and should_intercept(path):
        try:
            gb_data = json.loads(resp_body)
            modified, touched = apply_overrides(gb_data)
            if modified:
                resp_body = json.dumps(gb_data, ensure_ascii=False).encode("utf-8")
                sid = headers.get("x-claude-code-session-id") or headers.get("x-session-id")
                logger.info(
                    "\U0001f6e1 INTERCEPTED %s -- %d flags: %s",
                    path, len(touched), ", ".join(touched),
                )
                _db_writer.submit(
                    log_intercept_event,
                    session_id=sid,
                    endpoint=path,
                    flags_overridden=touched,
                )
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            pass

    try:
        resp_json = json.loads(resp_body)
        usage = _extract_usage(resp_json)
        _warn_if_poor(usage, path)
        # Writes go to the background DB writer: a synchronous enqueue (no
        # await) that response teardown cannot cut short -- see dbwriter.py.
        sid = headers.get("x-claude-code-session-id") or headers.get("x-session-id")
        # Extract ephemeral TTL tier tokens for non-streaming path
        resp_usage = resp_json.get("usage", {})
        cc_detail = resp_usage.get("cache_creation", {})
        eph_1h = cc_detail.get("ephemeral_1h_input_tokens", 0) if isinstance(cc_detail, dict) else 0
        eph_5m = cc_detail.get("ephemeral_5m_input_tokens", 0) if isinstance(cc_detail, dict) else 0
        raw_usage = resp_json.get("usage")
        if rewrite_skipped:
            raw_usage = dict(raw_usage if isinstance(raw_usage, dict) else {}, rewrite_skipped=True)
        _db_writer.submit(
            log_request,
            session_id=sid,
            model=usage["model"],
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cache_creation=usage["cache_creation"],
            cache_read=usage["cache_read"],
            status_code=upstream_resp.status_code,
            latency_ms=latency_ms,
            endpoint=path,
            is_stream=False,
            raw_usage=raw_usage,
            request_body_bytes=body_bytes,
            ratelimit_headers=rl_headers,
            ephemeral_1h_tokens=eph_1h,
            ephemeral_5m_tokens=eph_5m,
        )
        # Session history capture (opt-in): history_json is the stripped view
        # built after the single parse above, None when capture is off.
        if history_json is not None:
            try:
                from .history import capture_turn
                _db_writer.submit(
                    capture_turn,
                    size_hint=history_bytes + len(resp_body),
                    session_id=sid,
                    req_json=history_json,
                    resp_json=resp_json,
                    input_tokens=usage["input_tokens"],
                    request_size=body_bytes,
                    response_size=len(resp_body),
                    raw_mode=_HISTORY_RAW,
                )
            except Exception:
                logger.debug("history capture failed (non-stream)", exc_info=True)
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass

    # Strip encoding headers (httpx auto-decompresses, so lengths/encoding change)
    resp_headers = {
        k: v for k, v in upstream_resp.headers.items()
        if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
    }
    return Response(
        content=resp_body,
        status_code=upstream_resp.status_code,
        headers=resp_headers,
    )


async def _proxy_stream(
    client, request, url, headers, body, path, t0, body_bytes=0,
    history_json=None, history_bytes=0, rewrite_skipped=False,
):
    """Handle streaming responses -- true streaming proxy via client.stream().

    Uses an incremental UTF-8 decoder + line buffer so multibyte characters and
    SSE lines that span chunk boundaries are decoded correctly (C2 fix from
    audit-full-source-20260410.md -- mojibake root cause candidate).

    ``history_json`` is the caller's single parse of the request with inline
    binary payloads already stripped (None = no history capture for this
    request) and ``history_bytes`` the size it keeps alive (the writer's queue
    byte cap counts it); the body is not parsed again here. ``rewrite_skipped``
    marks a body sent upstream unchanged because it exceeded
    ``LLM_RELAY_MAX_REWRITE_BYTES``; it is recorded in ``raw_usage``.
    """
    req = client.build_request(method=request.method, url=url, headers=headers, content=body)
    try:
        upstream_resp = await _send_watching_client(client, req, request)
    except _ClientGone:
        return _client_gone_response(headers, path, t0, body_bytes, is_stream=True)
    except httpx.HTTPError as exc:
        return _upstream_error_response(exc, headers, path, t0, body_bytes, is_stream=True)

    rl_headers = _extract_ratelimit_headers(upstream_resp.headers)

    usage_acc = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation": 0,
        "cache_read": 0,
        "model": None,
        "ephemeral_1h_tokens": 0,
        "ephemeral_5m_tokens": 0,
        # Stall-vs-long-generation diagnostics (2026-09-28). Persisted through
        # raw_usage JSON, so no schema change is needed. output_tokens alone
        # cannot tell an aborted stream from a stalled one (it is only updated
        # on message_delta); these fields can.
        "content_deltas": 0,
        "first_delta_ms": None,
        "last_delta_ms": None,
        "message_stop": False,
        "stream_end": "unknown",
    }
    if rewrite_skipped:
        usage_acc["rewrite_skipped"] = True

    # History: accumulate response content blocks from SSE events. The request
    # side is history_json (stripped view from _proxy); nothing is re-parsed.
    _history_content = []  # list of completed content blocks
    _history_current_block = {}  # block being accumulated
    _history_active = history_json is not None

    def _process_sse_line(line: str) -> None:
        nonlocal _history_current_block
        if not line.startswith("data: "):
            return
        payload = line[6:]
        if payload.strip() == "[DONE]":
            return
        try:
            event = json.loads(payload)
        except (json.JSONDecodeError, KeyError):
            return
        etype = event.get("type", "")
        # Diagnostics: count content deltas for every stream, independent of
        # the history feature below.
        if etype == "content_block_delta":
            now_ms = round((time.time() - t0) * 1000, 1)
            usage_acc["content_deltas"] += 1
            if usage_acc["first_delta_ms"] is None:
                usage_acc["first_delta_ms"] = now_ms
            usage_acc["last_delta_ms"] = now_ms
        elif etype == "message_stop":
            usage_acc["message_stop"] = True
        if etype == "message_start":
            msg = event.get("message", {})
            u = _extract_usage(msg)
            for k in u:
                if k == "model":
                    usage_acc["model"] = u["model"]
                else:
                    usage_acc[k] += u[k]
            # Extract ephemeral TTL tier tokens from cache_creation breakdown
            msg_usage = msg.get("usage", {})
            cache_creation_detail = msg_usage.get("cache_creation", {})
            if isinstance(cache_creation_detail, dict):
                usage_acc["ephemeral_1h_tokens"] += cache_creation_detail.get(
                    "ephemeral_1h_input_tokens", 0
                )
                usage_acc["ephemeral_5m_tokens"] += cache_creation_detail.get(
                    "ephemeral_5m_input_tokens", 0
                )
        elif etype == "message_delta":
            _apply_delta_usage(usage_acc, event.get("usage", {}))
        # History: accumulate content blocks
        elif _history_active and etype == "content_block_start":
            cb = event.get("content_block", {})
            block_type = cb.get("type", "text")
            _history_current_block = {"type": block_type}
            if block_type == "text":
                _history_current_block["text"] = ""
            elif block_type == "thinking":
                _history_current_block["thinking"] = ""
            elif block_type == "tool_use":
                _history_current_block["id"] = cb.get("id", "")
                _history_current_block["name"] = cb.get("name", "")
                _history_current_block["input"] = {}
                _history_current_block["_input_json"] = ""
        elif _history_active and etype == "content_block_delta":
            delta = event.get("delta", {})
            delta_type = delta.get("type", "")
            if delta_type == "text_delta" and "text" in _history_current_block:
                _history_current_block["text"] += delta.get("text", "")
            elif delta_type == "thinking_delta" and "thinking" in _history_current_block:
                _history_current_block["thinking"] += delta.get("thinking", "")
            elif delta_type == "input_json_delta" and "_input_json" in _history_current_block:
                _history_current_block["_input_json"] += delta.get("partial_json", "")
        elif _history_active and etype == "content_block_stop":
            if _history_current_block:
                # Finalize tool_use input JSON
                if _history_current_block.get("type") == "tool_use" and "_input_json" in _history_current_block:
                    raw = _history_current_block.pop("_input_json", "")
                    try:
                        _history_current_block["input"] = json.loads(raw) if raw else {}
                    except (json.JSONDecodeError, ValueError):
                        _history_current_block["input"] = raw
                _history_content.append(_history_current_block)
                _history_current_block = {}

    async def _stream_and_log():
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        line_buf = ""
        try:
            async for chunk in upstream_resp.aiter_bytes():
                # Always forward bytes immediately -- optional usage parsing is
                # piggybacked on the decoded copy, not on the forwarded bytes.
                yield chunk

                if not _SSE_PARSE_USAGE:
                    continue

                # Incremental UTF-8 decode -- handles multibyte chars that span
                # chunk boundaries. Accumulate into a line buffer and only process
                # complete lines (terminated by \n).
                text = decoder.decode(chunk, final=False)
                if not text:
                    continue
                line_buf += text
                if "\n" not in line_buf:
                    continue
                lines = line_buf.split("\n")
                # Keep the trailing partial line for the next chunk.
                line_buf = lines[-1]
                for line in lines[:-1]:
                    _process_sse_line(line)
            usage_acc["stream_end"] = "completed"
        except (GeneratorExit, asyncio.CancelledError):
            # Client went away (Starlette drops the generator at the yield site).
            usage_acc["stream_end"] = "client_disconnect"
            raise
        except Exception:
            usage_acc["stream_end"] = "upstream_error"
            raise
        finally:
            # Step 1 -- flush the decoder (sync, safe under GeneratorExit).
            if _SSE_PARSE_USAGE:
                try:
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        line_buf += tail
                    if line_buf:
                        for line in line_buf.split("\n"):
                            _process_sse_line(line)
                except Exception:
                    logger.debug("SSE decoder flush failed", exc_info=True)

            # Step 2 -- ENQUEUE FIRST (sync, must run even if aclose/GC cancels the
            # generator). Starlette discards the async generator without calling
            # aclose(), so GeneratorExit fires at the yield site and any later
            # `await` in the finally chain can be cut short. submit() never awaits,
            # so the usage row reaches the background writer before that can
            # happen; the writer retries and spills to disk rather than drop it.
            latency_ms = (time.time() - t0) * 1000
            _warn_if_poor(usage_acc, path)
            sid = headers.get("x-claude-code-session-id") or headers.get("x-session-id")
            _db_writer.submit(
                log_request,
                session_id=sid,
                model=usage_acc["model"],
                input_tokens=usage_acc["input_tokens"],
                output_tokens=usage_acc["output_tokens"],
                cache_creation=usage_acc["cache_creation"],
                cache_read=usage_acc["cache_read"],
                status_code=upstream_resp.status_code,
                latency_ms=latency_ms,
                endpoint=path,
                is_stream=True,
                raw_usage=dict(usage_acc),
                request_body_bytes=body_bytes,
                ratelimit_headers=rl_headers,
                ephemeral_1h_tokens=usage_acc.get("ephemeral_1h_tokens", 0),
                ephemeral_5m_tokens=usage_acc.get("ephemeral_5m_tokens", 0),
            )

            # Step 2b -- session history capture (same sync enqueue as log_request)
            if _history_active:
                try:
                    from .history import capture_turn_streamed, content_bytes
                    response_bytes = content_bytes(_history_content)
                    _db_writer.submit(
                        capture_turn_streamed,
                        size_hint=history_bytes + response_bytes,
                        session_id=sid,
                        req_json=history_json,
                        accumulated_content=_history_content,
                        model=usage_acc["model"],
                        input_tokens=usage_acc["input_tokens"],
                        request_size=body_bytes,
                        response_size=response_bytes,
                        raw_mode=_HISTORY_RAW,
                    )
                except Exception:
                    logger.debug("history capture failed (stream)", exc_info=True)

            # Step 3 -- close the upstream response last. Wrap in try/except
            # because an async generator being finalised via GC may cancel the
            # await; we still want the resource closed and any error surfaced
            # at debug level, but not propagated.
            try:
                await upstream_resp.aclose()
            except Exception:
                logger.debug("upstream aclose() failed", exc_info=True)

    stream_headers = {
        k: v for k, v in upstream_resp.headers.items()
        if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
    }
    return StreamingResponse(
        _stream_and_log(),
        status_code=upstream_resp.status_code,
        headers=stream_headers,
    )


class _RequestLog:
    """Pure ASGI middleware: one line when a request *starts*, plus in-flight counters.

    uvicorn's access log is written when a response completes, so a request
    that takes the process down (the dashboard poll behind the 2026-09-29 OOM
    loop) leaves no trace. The start line goes to the "llm-relay.access"
    logger; /_health (polled) is skipped. The counters feed /_health:
    ``in_flight`` is every HTTP request including the /_health call reporting
    it, ``proxy_in_flight`` the /v1/ requests, where a stream counts until it
    ends -- 0 means a restart cuts no client stream. Not BaseHTTPMiddleware:
    that one buffers streaming responses through a background task.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        is_proxy = path.startswith("/v1/")
        _in_flight["total"] += 1
        if is_proxy:
            _in_flight["proxy"] += 1
        if _LOG_REQUEST_START and path != "/_health":
            size = "-"
            sid = "-"
            for name, value in scope.get("headers") or ():
                if name == b"content-length":
                    size = value.decode("latin-1", "replace")
                elif name == b"x-claude-code-session-id" or (name == b"x-session-id" and sid == "-"):
                    sid = value.decode("latin-1", "replace")[:8]
            client = scope.get("client")
            _access_log.info(
                "request start %s %s %sB client=%s session=%s",
                scope.get("method", "-"), path, size, client[0] if client else "-", sid,
            )
        try:
            await self.app(scope, receive, send)
        finally:
            _in_flight["total"] -= 1
            if is_proxy:
                _in_flight["proxy"] -= 1


_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def _rss_mb():
    """Resident set size of this process in MiB (Linux /proc), None elsewhere."""
    try:
        with open("/proc/self/statm", encoding="ascii") as fh:
            return int(fh.read().split()[1]) * _PAGE_SIZE // (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return None


def _cgroup_mb():
    """memory.current of this process's cgroup in MiB (the systemd unit's figure), None if unreadable."""
    try:
        with open("/proc/self/cgroup", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("0::"):
                    path = "/sys/fs/cgroup" + line[3:].strip() + "/memory.current"
                    with open(path, encoding="ascii") as current:
                        return int(current.read().strip()) // (1024 * 1024)
    except (OSError, ValueError):
        pass
    return None


async def _health(request: Request) -> Response:
    from .composition import cache_stats

    payload = {
        "status": "ok",
        "upstream": UPSTREAM,
        "in_flight": _in_flight["total"],
        "proxy_in_flight": _in_flight["proxy"],
        "memory_rss_mb": _rss_mb(),
        "memory_cgroup_mb": _cgroup_mb(),
        "pool": {
            "max_connections": _MAX_CONNECTIONS,
            "max_keepalive": _MAX_KEEPALIVE,
            "pool_timeout_s": _POOL_TIMEOUT_S,
        },
        "db_writer": _db_writer.snapshot(),
        "composition_cache": cache_stats(),
    }
    return Response(json.dumps(payload), media_type="application/json")


async def _stats(request: Request) -> Response:
    """Return recent session summaries as JSON."""
    from .db import get_session_summary
    summaries = get_session_summary(_get_conn())
    return Response(json.dumps(summaries, default=str), media_type="application/json")


async def _recent(request: Request) -> Response:
    """Return recent requests as JSON."""
    from .db import get_recent
    limit = int(request.query_params.get("limit", "20"))
    rows = get_recent(_get_conn(), limit=limit)
    return Response(json.dumps(rows, default=str), media_type="application/json")


async def _watchdog_loop():
    """Ping systemd watchdog every 60s (half of WatchdogSec=120)."""
    try:
        import socket
        notify_socket = os.environ.get("NOTIFY_SOCKET")
        if not notify_socket or not os.environ.get("WATCHDOG_USEC"):
            return
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        if notify_socket.startswith("@"):
            notify_socket = "\0" + notify_socket[1:]
        while True:
            sock.sendto(b"WATCHDOG=1", notify_socket)
            await asyncio.sleep(60)
    except Exception as exc:
        logger.debug("watchdog loop exited: %s", exc)


@asynccontextmanager
async def _lifespan(app):
    _db_writer.start()
    asyncio.create_task(_watchdog_loop())
    yield
    # Drain pending DB writes before the process exits (unit TimeoutStopSec=10;
    # anything still unwritten after the drain timeout is spilled to disk).
    await asyncio.to_thread(_db_writer.stop, 8.0)


# Build unified route list -- API and dashboard before catch-all proxy
_routes = [
    Route("/_health", _health, methods=["GET"]),
    Route("/_stats", _stats, methods=["GET"]),
    Route("/_recent", _recent, methods=["GET"]),
]

# Mount API routes (requires starlette, already available here)
try:
    from llm_relay.api.routes import get_api_routes
    _routes.extend(get_api_routes())
    logger.info("API routes mounted at /api/v1/")
except ImportError:
    logger.debug("API module not available, skipping")

# Redirect /dashboard and /display (no trailing slash) to their canonical URLs.
# Without these, the catch-all proxy route would forward them to Anthropic → 404.

def _redirect_to_trailing_slash(prefix: str):
    async def _redirect(request):
        from starlette.responses import RedirectResponse
        return RedirectResponse(url=prefix + "/", status_code=301)
    return _redirect

_routes.append(Route("/dashboard", _redirect_to_trailing_slash("/dashboard")))
_routes.append(Route("/display", _redirect_to_trailing_slash("/display")))
_routes.append(Route("/history", _redirect_to_trailing_slash("/history")))

# Mount dashboard, display, and history static files
try:
    from starlette.staticfiles import StaticFiles

    from llm_relay.dashboard import get_static_dir
    _dashboard_dir = get_static_dir()
    if _dashboard_dir.exists():
        _routes.append(Mount("/dashboard", app=StaticFiles(directory=str(_dashboard_dir), html=True)))
        logger.info("Dashboard mounted at /dashboard/")

    from llm_relay.display import get_static_dir as get_display_dir
    _display_dir = get_display_dir()
    if _display_dir.exists():
        _routes.append(Mount("/display", app=StaticFiles(directory=str(_display_dir), html=True)))
        logger.info("Display page mounted at /display/")

    from llm_relay.history import get_static_dir as get_history_dir
    _history_dir = get_history_dir()
    if _history_dir.exists():
        _routes.append(Mount("/history", app=StaticFiles(directory=str(_history_dir), html=True)))
        logger.info("History page mounted at /history/")
except ImportError:
    logger.debug("Dashboard module not available, skipping")

# Catch-all proxy must be last
_routes.append(Route("/{path:path}", _proxy, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]))

app = Starlette(
    routes=_routes,
    lifespan=_lifespan,
    middleware=[Middleware(_RequestLog)],
)
