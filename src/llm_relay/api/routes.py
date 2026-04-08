"""Starlette API routes for dashboard data — reuses existing proxy dependencies."""

from __future__ import annotations

import json
import logging
from typing import Any, List

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

logger = logging.getLogger(__name__)


def _json_response(data: Any, status: int = 200) -> Response:
    return Response(
        json.dumps(data, ensure_ascii=False, default=str),
        status_code=status,
        media_type="application/json",
    )


# ── GET /api/v1/cli/status ──

async def _api_cli_status(request: Request) -> Response:
    """Return installation and auth status for all registered CLIs."""
    from llm_relay.orch.discovery import discover_all

    statuses = discover_all()
    return _json_response([
        {
            "cli_id": s.cli_id,
            "binary_name": s.binary_name,
            "installed": s.installed,
            "authenticated": s.cli_authenticated,
            "api_key_available": s.api_key_available,
            "preferred_auth": s.preferred_auth.value,
            "version": s.version,
            "usable": s.is_usable(),
        }
        for s in statuses
    ])


# ── GET /api/v1/delegations ──

async def _api_delegations(request: Request) -> Response:
    """Return recent delegation history."""
    from llm_relay.orch.db import get_delegation_history, get_orch_conn

    limit = int(request.query_params.get("limit", "50"))
    try:
        conn = get_orch_conn()
        history = get_delegation_history(conn, limit=limit)
        conn.close()
        return _json_response({"count": len(history), "delegations": history})
    except Exception as e:
        logger.error("Failed to get delegation history: %s", e)
        return _json_response({"error": str(e), "delegations": []}, status=500)


# ── GET /api/v1/delegations/stats ──

async def _api_delegation_stats(request: Request) -> Response:
    """Return aggregate delegation statistics."""
    from llm_relay.orch.db import get_delegation_stats, get_orch_conn

    window = float(request.query_params.get("window", "24"))
    try:
        conn = get_orch_conn()
        stats = get_delegation_stats(conn, window_hours=window)
        conn.close()
        return _json_response(stats)
    except Exception as e:
        logger.error("Failed to get delegation stats: %s", e)
        return _json_response({"error": str(e)}, status=500)


# ── GET /api/v1/sessions ──

async def _api_sessions(request: Request) -> Response:
    """Return proxy session summaries (from existing proxy DB)."""
    try:
        from llm_relay.proxy.db import get_conn, get_session_summary

        window = float(request.query_params.get("window", "8"))
        conn = get_conn()
        summaries = get_session_summary(conn, window_hours=window)
        return _json_response({"count": len(summaries), "sessions": summaries})
    except ImportError:
        return _json_response({"error": "Proxy module not available", "sessions": []}, status=501)
    except Exception as e:
        logger.error("Failed to get sessions: %s", e)
        return _json_response({"error": str(e), "sessions": []}, status=500)


# ── GET /api/v1/cost ──

async def _api_cost(request: Request) -> Response:
    """Return cost breakdown from proxy DB."""
    try:
        from llm_relay.proxy.db import get_conn

        window = float(request.query_params.get("window", "24"))
        import time
        cutoff = time.time() - (window * 3600)
        conn = get_conn()
        rows = conn.execute(
            """SELECT model,
                      COUNT(*) as requests,
                      SUM(input_tokens) as total_input,
                      SUM(output_tokens) as total_output,
                      SUM(cache_read) as total_cache_read,
                      SUM(estimated_cost_usd) as total_cost_usd
               FROM requests
               WHERE ts >= ?
               GROUP BY model
               ORDER BY total_cost_usd DESC""",
            (cutoff,),
        ).fetchall()
        models = [dict(r) for r in rows]

        total_cost = sum(m.get("total_cost_usd") or 0 for m in models)
        return _json_response({
            "window_hours": window,
            "total_cost_usd": round(total_cost, 4),
            "per_model": models,
        })
    except ImportError:
        return _json_response({"error": "Proxy module not available"}, status=501)
    except Exception as e:
        logger.error("Failed to get cost data: %s", e)
        return _json_response({"error": str(e)}, status=500)


# ── GET /api/v1/health ──

async def _api_health(request: Request) -> Response:
    """Combined health check — CLI status + proxy status."""
    from llm_relay.orch.discovery import discover_all

    statuses = discover_all()
    cli_health = {
        "total": len(statuses),
        "installed": sum(1 for s in statuses if s.installed),
        "authenticated": sum(1 for s in statuses if s.cli_authenticated),
        "usable": sum(1 for s in statuses if s.is_usable()),
    }

    proxy_ok = False
    try:
        from llm_relay.proxy.db import get_conn
        conn = get_conn()
        conn.execute("SELECT 1").fetchone()
        proxy_ok = True
    except Exception:
        pass

    orch_ok = False
    try:
        from llm_relay.orch.db import get_orch_conn
        conn = get_orch_conn()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        orch_ok = True
    except Exception:
        pass

    return _json_response({
        "status": "ok" if cli_health["usable"] > 0 else "degraded",
        "cli": cli_health,
        "proxy_db": proxy_ok,
        "orch_db": orch_ok,
    })


def get_api_routes() -> List[Route]:
    """Return all API routes for mounting into the main Starlette app."""
    return [
        Route("/api/v1/cli/status", _api_cli_status, methods=["GET"]),
        Route("/api/v1/delegations", _api_delegations, methods=["GET"]),
        Route("/api/v1/delegations/stats", _api_delegation_stats, methods=["GET"]),
        Route("/api/v1/sessions", _api_sessions, methods=["GET"]),
        Route("/api/v1/cost", _api_cost, methods=["GET"]),
        Route("/api/v1/health", _api_health, methods=["GET"]),
    ]
