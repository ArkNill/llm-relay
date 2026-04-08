"""Transparent reverse proxy — forwards all requests to Anthropic API, logs usage."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import asyncio

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Mount, Route

from .db import get_conn, log_request, log_microcompact, log_budget_event

logger = logging.getLogger("llm-relay")

UPSTREAM = os.getenv("LLM_RELAY_UPSTREAM", "https://api.anthropic.com")
WARN_READ_RATIO = float(os.getenv("LLM_RELAY_WARN_RATIO", "50.0"))

CLEARED_MARKER = "[Old tool result content cleared]"

# FeatureFlags per-tool caps (from server_tool_caps)
TOOL_CAPS = {
    "global": 50_000,
    "Bash": 30_000,
    "Grep": 20_000,
    "Read": 50_000,
    "Glob": 20_000,
    "Snip": 1_000,
}
AGGREGATE_CAP = 200_000  # server_aggregate_cap

_RATELIMIT_PREFIXES = ("x-ratelimit-", "anthropic-ratelimit-", "retry-after")

_tokpress_available = False
try:
    from tokpress.integrations.proxy import compress_tool_results
    _tokpress_available = True
except ImportError:
    pass


def _try_compress(req_json: dict, body: bytes) -> bytes:
    """Compress tool results in-place if tokpress is available. Returns updated body."""
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
        cap = TOOL_CAPS.get(tool_name, TOOL_CAPS["global"])
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
            "📊 TOOL RESULT BUDGET: %d results, %d total chars (cap=%d) — session %s",
            len(tool_results),
            total_chars,
            AGGREGATE_CAP,
            session_id or "unknown",
        )
        if total_chars > AGGREGATE_CAP:
            logger.warning(
                "⚠ BUDGET EXCEEDED: %d chars > %d cap — budget enforcement likely active",
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
            "🔴 MICROCOMPACT DETECTED: %d/%d tool results cleared (msg indices: %s) — session %s",
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
                "⚠ LOW CACHE HIT: %.1f%% (read=%d, creation=%d) — %s",
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
    # Remove hop-by-hop and encoding headers
    for h in ("host", "transfer-encoding", "connection", "accept-encoding", "content-length"):
        headers.pop(h, None)

    is_stream = False
    req_json = None
    body_bytes = len(body) if body else 0
    if body:
        try:
            req_json = json.loads(body)
            is_stream = req_json.get("stream", False)
            # Scan for microcompact evidence in outgoing messages
            if req_json.get("messages"):
                sid = headers.get("x-claude-code-session-id") or headers.get("x-session-id")
                _scan_microcompact(req_json, sid)
                _scan_budget_enforcement(req_json, sid)
                # Compress Bash tool outputs before forwarding to API
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
    """Handle streaming responses — true streaming proxy via client.stream()."""
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

    async def _stream_and_log():
        try:
            async for chunk in upstream_resp.aiter_bytes():
                # Parse SSE lines for usage data
                for line in chunk.decode("utf-8", errors="replace").split("\n"):
                    if line.startswith("data: ") and line.strip() != "data: [DONE]":
                        try:
                            event = json.loads(line[6:])
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
                        except (json.JSONDecodeError, KeyError):
                            pass

                yield chunk
        finally:
            await upstream_resp.aclose()

            # Log after stream ends
            latency_ms = (time.time() - t0) * 1000
            _warn_if_poor(usage_acc, path)
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


from contextlib import asynccontextmanager

@asynccontextmanager
async def _lifespan(app):
    asyncio.create_task(_watchdog_loop())
    yield


# Build unified route list — API and dashboard before catch-all proxy
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

# Mount dashboard static files
try:
    from starlette.staticfiles import StaticFiles
    from llm_relay.dashboard import get_static_dir
    _dashboard_dir = get_static_dir()
    if _dashboard_dir.exists():
        _routes.append(Mount("/dashboard", app=StaticFiles(directory=str(_dashboard_dir), html=True)))
        logger.info("Dashboard mounted at /dashboard/")
except ImportError:
    logger.debug("Dashboard module not available, skipping")

# Catch-all proxy must be last
_routes.append(Route("/{path:path}", _proxy, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]))

app = Starlette(
    routes=_routes,
    lifespan=_lifespan,
)
