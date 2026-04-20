"""Starlette API routes for dashboard data -- reuses existing proxy dependencies."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, List, Optional

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


# ── Zone classification ──

# Turn count is display-only and no longer drives zone judgment.
# Zones are computed from current_ctx (token-based) via two independent scales:
#   A) absolute thresholds  -- env CC_TOKEN_A_YELLOW/ORANGE/RED/HARD
#   B) ratio-of-ceiling     -- env CC_TOKEN_CEILING (50/70/90/100 %)
# Overall zone = worst of A and B (max).

_ZONE_ORDER = {"green": 0, "yellow": 1, "orange": 2, "red": 3, "hard": 4}


def _classify_zone(turns: int) -> tuple:
    """Legacy turn-based classification -- kept only for backward compatibility.

    Not used by any endpoint anymore. Turn counts are display-only now.
    """
    yellow = int(os.getenv("CC_TURN_YELLOW", "200"))
    orange = int(os.getenv("CC_TURN_ORANGE", "250"))
    red = int(os.getenv("CC_TURN_RED", "300"))

    if turns >= red:
        return "red", "위험", None, f"{red}턴 초과. 품질 저하 가능성이 높습니다. 새 세션으로 전환하세요."
    if turns >= orange:
        return "orange", "경고", red, f"{orange}턴 도달. 품질 저하 구간 진입 임박. 로테이션을 권장합니다."
    if turns >= yellow:
        return "yellow", "주의", orange, f"{yellow}턴 도달. 새 세션 준비를 권장합니다."
    return "green", "안전", yellow, None


def _classify_zone_absolute(tokens: int) -> tuple:
    """Zone A -- absolute token threshold classification.

    Env: CC_TOKEN_A_YELLOW / _A_ORANGE / _A_RED / _A_HARD
    Returns (zone, zone_label, next_threshold, message).
    """
    yellow = int(os.getenv("CC_TOKEN_A_YELLOW", "300000"))
    orange = int(os.getenv("CC_TOKEN_A_ORANGE", "500000"))
    red = int(os.getenv("CC_TOKEN_A_RED", "750000"))
    hard = int(os.getenv("CC_TOKEN_A_HARD", "900000"))

    if tokens >= hard:
        return "hard", "차단", None, f"{hard // 1000}K 초과. 즉시 세션 정리 필요."
    if tokens >= red:
        return "red", "위험", hard, f"{red // 1000}K 도달. 세션 로테이션 필수."
    if tokens >= orange:
        return "orange", "경고", red, f"{orange // 1000}K 도달. 현재 작업 마무리 후 rotate."
    if tokens >= yellow:
        return "yellow", "주의", orange, f"{yellow // 1000}K 도달. 문서 업데이트 + rotate 준비."
    return "green", "안전", yellow, None


def _classify_zone_ratio(tokens: int, ceiling: Optional[int] = None) -> tuple:
    """Zone B -- ratio-of-ceiling classification (50/70/90/100%).

    Env: CC_TOKEN_CEILING (default 1,000,000 for local / 500,000 recommended for public)
    Returns (zone, zone_label, next_threshold, message).
    """
    if ceiling is None:
        ceiling = int(os.getenv("CC_TOKEN_CEILING", "1000000"))
    if ceiling <= 0:
        return "green", "안전", 0, None

    yellow_t = int(ceiling * 0.50)
    orange_t = int(ceiling * 0.70)
    red_t = int(ceiling * 0.90)
    ratio = tokens / ceiling if ceiling else 0.0

    if ratio >= 1.0:
        return "hard", "차단", None, f"100% ({ceiling // 1000}K) 천장 도달. 즉시 세션 정리."
    if ratio >= 0.90:
        return "red", "위험", ceiling, f"90% ({red_t // 1000}K) 도달. 로테이션 필수."
    if ratio >= 0.70:
        return "orange", "경고", red_t, f"70% ({orange_t // 1000}K) 도달. 마무리 후 rotate."
    if ratio >= 0.50:
        return "yellow", "주의", orange_t, f"50% ({yellow_t // 1000}K) 도달. rotate 준비."
    return "green", "안전", yellow_t, None


def _overall_zone(zone_a: str, zone_b: str) -> str:
    """Return whichever of the two zones is more severe (max by _ZONE_ORDER)."""
    if _ZONE_ORDER.get(zone_a, 0) >= _ZONE_ORDER.get(zone_b, 0):
        return zone_a
    return zone_b


def _compute_zone_bundle(current_ctx: int, peak_ctx: int) -> dict:
    """Compute Zone A/B on current_ctx (primary) + A/B on peak_ctx (reference).

    Returns a flat dict ready to be merged into the session response.
    """
    za_cur = _classify_zone_absolute(current_ctx)
    zb_cur = _classify_zone_ratio(current_ctx)
    za_peak = _classify_zone_absolute(peak_ctx)
    zb_peak = _classify_zone_ratio(peak_ctx)
    overall = _overall_zone(za_cur[0], zb_cur[0])

    # Pick message from the worst-of-A/B on current_ctx
    if _ZONE_ORDER.get(za_cur[0], 0) >= _ZONE_ORDER.get(zb_cur[0], 0):
        worst_msg = za_cur[3]
        worst_next = za_cur[2]
    else:
        worst_msg = zb_cur[3]
        worst_next = zb_cur[2]

    return {
        "zone": overall,
        "zone_a": za_cur[0],
        "zone_a_label": za_cur[1],
        "zone_a_message": za_cur[3],
        "zone_a_next": za_cur[2],
        "zone_b": zb_cur[0],
        "zone_b_label": zb_cur[1],
        "zone_b_message": zb_cur[3],
        "zone_b_next": zb_cur[2],
        "zone_a_peak": za_peak[0],
        "zone_b_peak": zb_peak[0],
        # legacy-compatible fields
        "message": worst_msg,
        "next_threshold": worst_next,
    }


async def _api_turns(request: Request) -> Response:
    """Return turn count + 4 token metrics + dual-zone classification for a session."""
    try:
        from llm_relay.proxy.db import get_conn, get_turn_count

        session_id = request.path_params["session_id"]
        conn = get_conn()
        data = get_turn_count(conn, session_id)
        turns = data["turns"]

        duration_s = 0.0
        if data["first_ts"] and data["last_ts"]:
            duration_s = data["last_ts"] - data["first_ts"]

        zones = _compute_zone_bundle(data["current_ctx"], data["peak_ctx"])

        return _json_response({
            "session_id": session_id,
            "turns": turns,
            "first_ts": data["first_ts"],
            "last_ts": data["last_ts"],
            "duration_s": round(duration_s, 1),
            # 4 token metrics
            "current_ctx": data["current_ctx"],
            "peak_ctx": data["peak_ctx"],
            "recent_peak": data["recent_peak"],
            "cumul_unique": data["cumul_unique"],
            # Ceiling for ratio display on the client
            "ceiling": int(os.getenv("CC_TOKEN_CEILING", "1000000")),
            # Zone bundle (zone, zone_a*, zone_b*, zone_{a,b}_peak, legacy message/next_threshold)
            **zones,
        })
    except ImportError:
        return _json_response({"error": "Proxy module not available"}, status=501)
    except Exception as e:
        logger.error("Failed to get turn count: %s", e)
        return _json_response({"error": str(e)}, status=500)


# ── GET /api/v1/turns (all sessions) ──

async def _api_turns_all(request: Request) -> Response:
    """Return turn counts + token metrics + dual-zone classification for active sessions.

    Filters out dead sessions (CC process exited) using the same liveness logic
    as /api/v1/display so the dashboard Turn Monitor doesn't accumulate zombies.
    Use ?include_dead=1 to bypass the filter.
    """
    try:
        from llm_relay.api.display import check_cc_session_alive, collect_owned_cc_pids
        from llm_relay.proxy.db import get_all_session_terminals, get_all_turn_counts, get_conn

        window = float(request.query_params.get("window", "4"))
        include_dead = request.query_params.get("include_dead", "0") == "1"
        conn = get_conn()
        rows = get_all_turn_counts(conn, window_hours=window)
        terminals = get_all_session_terminals(conn)
        ceiling = int(os.getenv("CC_TOKEN_CEILING", "1000000"))

        owned_cc_pids = collect_owned_cc_pids(terminals)
        now_ts = time.time()

        sessions = []
        for r in rows:
            term = terminals.get(r["session_id"]) or {}
            alive = check_cc_session_alive(term, r["last_ts"], owned_cc_pids, now_ts)
            if not alive and not include_dead:
                continue
            duration_s = 0.0
            if r["first_ts"] and r["last_ts"]:
                duration_s = r["last_ts"] - r["first_ts"]
            zones = _compute_zone_bundle(r["current_ctx"], r["peak_ctx"])
            sessions.append({
                "session_id": r["session_id"],
                "turns": r["turns"],
                "first_ts": r["first_ts"],
                "last_ts": r["last_ts"],
                "duration_s": round(duration_s, 1),
                "current_ctx": r["current_ctx"],
                "peak_ctx": r["peak_ctx"],
                "recent_peak": r["recent_peak"],
                "cumul_unique": r["cumul_unique"],
                "ceiling": ceiling,
                "alive": alive,
                **zones,
            })

        return _json_response({"count": len(sessions), "sessions": sessions})
    except ImportError:
        return _json_response({"error": "Proxy module not available", "sessions": []}, status=501)
    except Exception as e:
        logger.error("Failed to get all turn counts: %s", e)
        return _json_response({"error": str(e), "sessions": []}, status=500)


# ── POST /api/v1/session-terminal ──

async def _api_session_terminal(request: Request) -> Response:
    """Upsert terminal info for a session (called by Stop hook).

    When a new session registers the same cc_pid as an older session
    (terminal reuse), the old session's cc_pid/tty are automatically
    cleared so it no longer appears alive on the display page.
    """
    try:
        from llm_relay.proxy.db import get_conn, upsert_session_terminal

        body = await request.json()
        session_id = body.get("session_id")
        if not session_id:
            return _json_response({"error": "session_id required"}, status=400)

        conn = get_conn()
        upsert_session_terminal(
            conn,
            session_id=session_id,
            tty=body.get("tty"),
            cc_pid=body.get("cc_pid"),
            term_pid=body.get("term_pid"),
            term_name=body.get("term_name"),
        )
        return _json_response({"ok": True, "session_id": session_id})
    except ImportError:
        return _json_response({"error": "Proxy module not available"}, status=501)
    except Exception as e:
        logger.error("Failed to upsert session terminal: %s", e)
        return _json_response({"error": str(e)}, status=500)


# ── GET /api/v1/display ──

async def _api_display(request: Request) -> Response:
    """Return sessions with turn count + last user prompt + terminal info for display page.

    Filters sessions by whether their CC process is still running.
    Uses session_terminals.tty to locate running claude processes on the host via
    /host/proc, so stale cc_pid values don't cause false negatives.
    Use `?include_dead=1` to include dead sessions.
    """
    try:
        from llm_relay.api.display import (
            check_cc_session_alive,
            collect_owned_cc_pids,
            discover_external_cli_sessions,
            get_last_user_prompt,
        )
        from llm_relay.proxy.db import get_all_session_terminals, get_all_turn_counts, get_conn

        window = float(request.query_params.get("window", "4"))
        include_dead = request.query_params.get("include_dead", "0") == "1"
        conn = get_conn()
        rows = get_all_turn_counts(conn, window_hours=window)
        terminals = get_all_session_terminals(conn)
        ceiling = int(os.getenv("CC_TOKEN_CEILING", "1000000"))

        owned_cc_pids = collect_owned_cc_pids(terminals)
        now_ts = time.time()

        sessions = []
        for r in rows:
            term = terminals.get(r["session_id"]) or {}
            alive = check_cc_session_alive(term, r["last_ts"], owned_cc_pids, now_ts)
            if not alive and not include_dead:
                continue

            duration_s = 0.0
            if r["first_ts"] and r["last_ts"]:
                duration_s = r["last_ts"] - r["first_ts"]
            prompt_info = get_last_user_prompt(r["session_id"])
            zones = _compute_zone_bundle(r["current_ctx"], r["peak_ctx"])
            sessions.append({
                "session_id": r["session_id"],
                "provider": "claude-code",
                "provider_name": "Claude Code",
                "turns": r["turns"],
                "first_ts": r["first_ts"],
                "last_ts": r["last_ts"],
                "duration_s": round(duration_s, 1),
                # 4 token metrics
                "current_ctx": r["current_ctx"],
                "peak_ctx": r["peak_ctx"],
                "recent_peak": r["recent_peak"],
                "cumul_unique": r["cumul_unique"],
                "ceiling": ceiling,
                # Dual zone bundle
                **zones,
                # Terminal + prompt
                "last_prompt": prompt_info["text"],
                "last_prompt_ts": prompt_info["timestamp"],
                "tty": term.get("tty"),
                "cc_pid": term.get("cc_pid"),
                "term_pid": term.get("term_pid"),
                "term_name": term.get("term_name"),
                "alive": alive,
            })

        # Merge Codex/Gemini sessions discovered from session files
        try:
            external = discover_external_cli_sessions(
                window_hours=window, include_dead=include_dead,
            )
            sessions.extend(external)
        except Exception as exc:
            logger.debug("External CLI session discovery failed: %s", exc)

        return _json_response({"count": len(sessions), "sessions": sessions})
    except ImportError:
        return _json_response({"error": "Proxy module not available", "sessions": []}, status=501)
    except Exception as e:
        logger.error("Failed to get display data: %s", e)
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
    """Combined health check -- CLI status + proxy status."""
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
        Route("/api/v1/turns", _api_turns_all, methods=["GET"]),
        Route("/api/v1/turns/{session_id}", _api_turns, methods=["GET"]),
        Route("/api/v1/display", _api_display, methods=["GET"]),
        Route("/api/v1/session-terminal", _api_session_terminal, methods=["POST"]),
        Route("/api/v1/health", _api_health, methods=["GET"]),
    ]
