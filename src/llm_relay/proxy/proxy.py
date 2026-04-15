"""Transparent reverse proxy -- forwards all requests to Anthropic API, logs usage."""

from __future__ import annotations

import asyncio
import codecs
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Mount, Route

from .db import get_conn, log_budget_event, log_microcompact, log_request

# CC cache fix (opt-in, LLM_RELAY_CACHE_FIX=1)
_cache_fix_available = False
if os.getenv("LLM_RELAY_CACHE_FIX", "0") == "1":
    try:
        from .cc_cache_fix import normalize_request as _normalize_cache
        from .db import log_cache_diagnostic
        _cache_fix_available = True
    except ImportError:
        pass

logger = logging.getLogger("llm-relay")

UPSTREAM = os.getenv("LLM_RELAY_UPSTREAM", "https://api.anthropic.com")
WARN_READ_RATIO = float(os.getenv("LLM_RELAY_WARN_RATIO", "50.0"))

# Performance toggles (default OFF for low-overhead proxying).
# Set =1 to re-enable the corresponding diagnostic/compression path.
_SCAN_ENABLED = os.getenv("LLM_RELAY_SCAN_ENABLED", "0") == "1"
_TOKPRESS_ENABLED = os.getenv("LLM_RELAY_TOKPRESS", "0") == "1"
_SSE_PARSE_USAGE = os.getenv("LLM_RELAY_SSE_PARSE_USAGE", "1") == "1"

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
            log_budget_event(
                _get_conn(),
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
        log_microcompact(
            _get_conn(),
            session_id=session_id,
            cleared_count=len(cleared_indices),
            total_tool_results=total_tool_results,
            cleared_indices=cleared_indices,
            message_count=len(messages),
        )

_conn = None
_client = None


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
            timeout=httpx.Timeout(300.0, connect=30.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
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


async def _proxy(request: Request) -> Response:
    """Forward request to upstream, log usage, return response."""
    client = _get_client()
    path = request.url.path
    query = str(request.url.query)
    url = f"{path}?{query}" if query else path

    body = await request.body()
    headers = dict(request.headers)
    # Strip hop-by-hop headers only. Preserve accept-encoding so the upstream
    # leg (internet) stays gzip-compressed -- httpx auto-decompresses locally and
    # we strip content-encoding on the response to send plain bytes to the
    # client over loopback (bytes saved upstream, not re-encoded on loopback).
    for h in ("host", "transfer-encoding", "connection", "content-length"):
        headers.pop(h, None)

    is_stream = False
    req_json = None
    body_bytes = len(body) if body else 0
    if body:
        try:
            req_json = json.loads(body)
            is_stream = bool(req_json.get("stream", False))
            if req_json.get("messages"):
                sid = headers.get("x-claude-code-session-id") or headers.get("x-session-id")
                if _SCAN_ENABLED:
                    _scan_microcompact(req_json, sid)
                    _scan_budget_enforcement(req_json, sid)
                if _cache_fix_available and path.startswith("/v1/messages"):
                    cf_modified, cf_diag = _normalize_cache(req_json, headers)
                    if cf_modified:
                        body = json.dumps(req_json, ensure_ascii=False).encode("utf-8")
                    if cf_diag:
                        log_cache_diagnostic(_get_conn(), session_id=sid, **cf_diag)
                if _tokpress_available:
                    body = _try_compress(req_json, body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    t0 = time.time()

    if is_stream:
        return await _proxy_stream(client, request.method, url, headers, body, path, t0, body_bytes)

    # Non-streaming
    upstream_resp = await client.request(
        method=request.method,
        url=url,
        headers=headers,
        content=body,
    )

    latency_ms = (time.time() - t0) * 1000

    # Parse and log usage
    resp_body = upstream_resp.content
    rl_headers = _extract_ratelimit_headers(upstream_resp.headers)

    try:
        resp_json = json.loads(resp_body)
        usage = _extract_usage(resp_json)
        _warn_if_poor(usage, path)
        # Synchronous log -- WAL mode keeps this ~0.6ms. Moving to a worker
        # thread via asyncio.to_thread inside starlette response flow was
        # dropping records when the response task was torn down mid-await.
        log_request(
            _get_conn(),
            session_id=headers.get("x-claude-code-session-id") or headers.get("x-session-id"),
            model=usage["model"],
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cache_creation=usage["cache_creation"],
            cache_read=usage["cache_read"],
            status_code=upstream_resp.status_code,
            latency_ms=latency_ms,
            endpoint=path,
            is_stream=False,
            raw_usage=resp_json.get("usage"),
            request_body_bytes=body_bytes,
            ratelimit_headers=rl_headers,
        )
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


async def _proxy_stream(client, method, url, headers, body, path, t0, body_bytes=0):
    """Handle streaming responses -- true streaming proxy via client.stream().

    Uses an incremental UTF-8 decoder + line buffer so multibyte characters and
    SSE lines that span chunk boundaries are decoded correctly (C2 fix from
    audit-full-source-20260410.md -- mojibake root cause candidate).
    """
    req = client.build_request(method=method, url=url, headers=headers, content=body)
    upstream_resp = await client.send(req, stream=True)

    rl_headers = _extract_ratelimit_headers(upstream_resp.headers)

    usage_acc = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation": 0,
        "cache_read": 0,
        "model": None,
    }

    def _process_sse_line(line: str) -> None:
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
        if etype == "message_start":
            msg = event.get("message", {})
            u = _extract_usage(msg)
            for k in u:
                if k == "model":
                    usage_acc["model"] = u["model"]
                else:
                    usage_acc[k] += u[k]
        elif etype == "message_delta":
            delta_usage = event.get("usage", {})
            usage_acc["output_tokens"] = delta_usage.get(
                "output_tokens", usage_acc["output_tokens"]
            )

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

            # Step 2 -- LOG FIRST (sync, must run even if aclose/GC cancels the
            # generator). Starlette discards the async generator without calling
            # aclose(), so GeneratorExit fires at the yield site and any later
            # `await` in the finally chain can be cut short. Putting log_request
            # before the await guarantees the usage row lands in SQLite.
            latency_ms = (time.time() - t0) * 1000
            _warn_if_poor(usage_acc, path)
            try:
                log_request(
                    _get_conn(),
                    session_id=headers.get("x-claude-code-session-id") or headers.get("x-session-id"),
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
                )
            except Exception:
                logger.warning("log_request failed (stream path)", exc_info=True)

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


async def _health(request: Request) -> Response:
    return Response(json.dumps({"status": "ok", "upstream": UPSTREAM}), media_type="application/json")


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
    except Exception:
        logger.debug("watchdog loop exited")


@asynccontextmanager
async def _lifespan(app):
    asyncio.create_task(_watchdog_loop())
    yield


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

# Mount dashboard static files
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
except ImportError:
    logger.debug("Dashboard module not available, skipping")

# Catch-all proxy must be last
_routes.append(Route("/{path:path}", _proxy, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]))

app = Starlette(
    routes=_routes,
    lifespan=_lifespan,
)
