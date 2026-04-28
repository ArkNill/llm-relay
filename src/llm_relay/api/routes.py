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

# Docker environment detection — liveness filtering is impossible inside a container
# because /proc shows container processes, not host processes.
_IN_DOCKER = os.path.exists("/.dockerenv") or os.getenv("CC_IN_DOCKER", "") == "1"


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
    from llm_relay.i18n import t

    yellow = int(os.getenv("CC_TURN_YELLOW", "200"))
    orange = int(os.getenv("CC_TURN_ORANGE", "250"))
    red = int(os.getenv("CC_TURN_RED", "300"))

    if turns >= red:
        return "red", t("zone.danger"), None, t("zone.turn.red", n=red)
    if turns >= orange:
        return "orange", t("zone.warning"), red, t("zone.turn.orange", n=orange)
    if turns >= yellow:
        return "yellow", t("zone.caution"), orange, t("zone.turn.yellow", n=yellow)
    return "green", t("zone.safe"), yellow, None


def _classify_zone_absolute(tokens: int) -> tuple:
    """Zone A -- absolute token threshold classification.

    Env: CC_TOKEN_A_YELLOW / _A_ORANGE / _A_RED / _A_HARD
    Returns (zone, zone_label, next_threshold, message).
    """
    from llm_relay.i18n import t

    yellow = int(os.getenv("CC_TOKEN_A_YELLOW", "300000"))
    orange = int(os.getenv("CC_TOKEN_A_ORANGE", "500000"))
    red = int(os.getenv("CC_TOKEN_A_RED", "750000"))
    hard = int(os.getenv("CC_TOKEN_A_HARD", "900000"))

    if tokens >= hard:
        return "hard", t("zone.blocked"), None, t("zone.abs.hard", n=hard // 1000)
    if tokens >= red:
        return "red", t("zone.danger"), hard, t("zone.abs.red", n=red // 1000)
    if tokens >= orange:
        return "orange", t("zone.warning"), red, t("zone.abs.orange", n=orange // 1000)
    if tokens >= yellow:
        return "yellow", t("zone.caution"), orange, t("zone.abs.yellow", n=yellow // 1000)
    return "green", t("zone.safe"), yellow, None


def _classify_zone_ratio(tokens: int, ceiling: Optional[int] = None) -> tuple:
    """Zone B -- ratio-of-ceiling classification (50/70/90/100%).

    Env: CC_TOKEN_CEILING (default 1,000,000 for local / 500,000 recommended for public)
    Returns (zone, zone_label, next_threshold, message).
    """
    from llm_relay.i18n import t

    if ceiling is None:
        ceiling = int(os.getenv("CC_TOKEN_CEILING", "1000000"))
    if ceiling <= 0:
        return "green", t("zone.safe"), 0, None

    yellow_t = int(ceiling * 0.50)
    orange_t = int(ceiling * 0.70)
    red_t = int(ceiling * 0.90)
    ratio = tokens / ceiling if ceiling else 0.0

    if ratio >= 1.0:
        return "hard", t("zone.blocked"), None, t("zone.ratio.hard", n=ceiling // 1000)
    if ratio >= 0.90:
        return "red", t("zone.danger"), ceiling, t("zone.ratio.red", n=red_t // 1000)
    if ratio >= 0.70:
        return "orange", t("zone.warning"), red_t, t("zone.ratio.orange", n=orange_t // 1000)
    if ratio >= 0.50:
        return "yellow", t("zone.caution"), orange_t, t("zone.ratio.yellow", n=yellow_t // 1000)
    return "green", t("zone.safe"), yellow_t, None


async def _api_i18n(request: Request) -> Response:
    """Return i18n message dict for the requested locale."""
    from llm_relay.i18n import MESSAGES, get_lang

    lang = request.query_params.get("lang", get_lang())
    msgs = MESSAGES.get(lang, MESSAGES["en"])
    return _json_response({"lang": lang, "messages": msgs})


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
        from llm_relay.proxy.db import get_conn, get_session_cache_stats, get_ttl_tier, get_turn_count

        session_id = request.path_params["session_id"]
        conn = get_conn()
        data = get_turn_count(conn, session_id)
        turns = data["turns"]

        duration_s = 0.0
        if data["first_ts"] and data["last_ts"]:
            duration_s = data["last_ts"] - data["first_ts"]

        zones = _compute_zone_bundle(data["current_ctx"], data["peak_ctx"])
        cache = get_session_cache_stats(conn, session_id=session_id)
        ttl = get_ttl_tier(conn, session_id=session_id)

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
            # Cache hit rate + TTL tier
            "cache_hit_rate": cache["cache_hit_rate"],
            "ttl_tier": ttl["tier"],
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
        include_dead = request.query_params.get("include_dead", "0") == "1" or _IN_DOCKER
        conn = get_conn()
        rows = get_all_turn_counts(conn, window_hours=window)
        terminals = get_all_session_terminals(conn)
        ceiling = int(os.getenv("CC_TOKEN_CEILING", "1000000"))

        owned_cc_pids = collect_owned_cc_pids(terminals)
        now_ts = time.time()

        sessions = []
        for r in rows:
            term = terminals.get(r["session_id"]) or {}
            alive = _IN_DOCKER or check_cc_session_alive(term, r["last_ts"], owned_cc_pids, now_ts)
            if not alive and not include_dead:
                continue
            duration_s = 0.0
            if r["first_ts"] and r["last_ts"]:
                duration_s = r["last_ts"] - r["first_ts"]
            zones = _compute_zone_bundle(r["current_ctx"], r["peak_ctx"])
            sessions.append({
                "session_id": r["session_id"],
                "term_name": term.get("term_name"),
                "tty": term.get("tty"),
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
                # Context composition (requires LLM_RELAY_HISTORY=1)
                "composition": _get_composition_safe(conn, r["session_id"]),
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


# ── Context composition helper ──


def _get_composition_safe(conn: Any, session_id: str) -> Optional[dict]:
    """Return composition analysis for a session, or None on any failure."""
    if not os.getenv("LLM_RELAY_HISTORY", "") == "1":
        return None
    try:
        from llm_relay.proxy.composition import analyze_session_composition

        return analyze_session_composition(conn, session_id)
    except Exception as exc:
        logger.debug("Composition analysis failed for %s: %s", session_id, exc)
        return None


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
            detect_connection_type,
            discover_external_cli_sessions,
            get_last_user_prompt,
        )
        from llm_relay.proxy.db import (
            get_all_session_terminals,
            get_all_turn_counts,
            get_conn,
            get_session_cache_stats,
            get_ttl_tier,
        )

        window = float(request.query_params.get("window", "4"))
        include_dead = request.query_params.get("include_dead", "0") == "1" or _IN_DOCKER
        conn = get_conn()
        rows = get_all_turn_counts(conn, window_hours=window)
        terminals = get_all_session_terminals(conn)
        ceiling = int(os.getenv("CC_TOKEN_CEILING", "1000000"))

        owned_cc_pids = collect_owned_cc_pids(terminals)
        now_ts = time.time()

        sessions = []
        for r in rows:
            term = terminals.get(r["session_id"]) or {}
            alive = _IN_DOCKER or check_cc_session_alive(term, r["last_ts"], owned_cc_pids, now_ts)
            if not alive and not include_dead:
                continue

            duration_s = 0.0
            if r["first_ts"] and r["last_ts"]:
                duration_s = r["last_ts"] - r["first_ts"]
            prompt_info = get_last_user_prompt(r["session_id"])
            zones = _compute_zone_bundle(r["current_ctx"], r["peak_ctx"])
            cache = get_session_cache_stats(conn, session_id=r["session_id"])
            ttl = get_ttl_tier(conn, session_id=r["session_id"])
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
                # Cache hit rate + TTL tier
                "cache_hit_rate": cache["cache_hit_rate"],
                "ttl_tier": ttl["tier"],
                # Dual zone bundle
                **zones,
                # Terminal + prompt
                "last_prompt": prompt_info["text"],
                "last_prompt_ts": prompt_info["timestamp"],
                "tty": term.get("tty"),
                "cc_pid": term.get("cc_pid"),
                "term_pid": term.get("term_pid"),
                "term_name": term.get("term_name"),
                "connection_type": detect_connection_type(term.get("cc_pid") or 0),
                "composition": _get_composition_safe(conn, r["session_id"]),
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


# ── GET /api/v1/history ──

async def _api_history_sessions(request: Request) -> Response:
    """Return sessions that have conversation history recorded."""
    try:
        from llm_relay.proxy.db import get_conn, get_history_sessions

        window = float(request.query_params.get("window", "24"))
        conn = get_conn()
        sessions = get_history_sessions(conn, window_hours=window)
        ceiling = int(os.getenv("CC_TOKEN_CEILING", "1000000"))

        for s in sessions:
            s["history_source"] = "proxy_db"
            s["ceiling"] = ceiling

        # Codex/Gemini do not flow through the proxy DB. For the history list,
        # expose their session-file summaries so token growth is visible there too.
        try:
            from llm_relay.api.display import discover_external_cli_sessions

            seen = {s["session_id"] for s in sessions}
            external = discover_external_cli_sessions(
                window_hours=window,
                include_dead=True,
                check_open_fds=False,
            )
            for ext in external:
                if ext["session_id"] in seen:
                    continue
                sessions.append({
                    "session_id": ext["session_id"],
                    "total_turns": ext["turns"],
                    "first_ts": ext["first_ts"],
                    "last_ts": ext["last_ts"],
                    "total_request_bytes": 0,
                    "total_response_bytes": 0,
                    "provider": ext["provider"],
                    "current_ctx": ext.get("current_ctx", 0),
                    "peak_ctx": ext.get("peak_ctx", 0),
                    "recent_peak": ext.get("recent_peak", 0),
                    "cumul_unique": ext.get("cumul_unique", 0),
                    "ceiling": ext.get("ceiling", 0),
                    "model_window": ext.get("model_window", 0),
                    "official_context_window": ext.get("official_context_window", 0),
                    "official_max_output": ext.get("official_max_output", 0),
                    "alive": ext.get("alive", False),
                    "history_source": "session_file",
                })
            sessions.sort(key=lambda s: s.get("last_ts") or 0, reverse=True)
        except Exception as exc:
            logger.debug("External CLI history discovery failed: %s", exc)

        return _json_response({"count": len(sessions), "sessions": sessions})
    except ImportError:
        return _json_response({"error": "Proxy module not available"}, status=501)
    except Exception as e:
        logger.error("Failed to get history sessions: %s", e)
        return _json_response({"error": str(e)}, status=500)


# ── GET /api/v1/history/{session_id} ──

async def _api_history_detail(request: Request) -> Response:
    """Return conversation history for a specific session.

    Query params:
      turn_start (int): Start turn number (default 0)
      turn_end (int): End turn number (-1 = all, default -1)
      include_thinking (0|1): Include thinking blocks (default 0)
      raw (0|1): Return raw stored data without diff reconstruction (default 0)
    """
    try:
        from llm_relay.proxy.db import get_conn, get_session_history

        session_id = request.path_params["session_id"]
        turn_start = int(request.query_params.get("turn_start", "0"))
        turn_end = int(request.query_params.get("turn_end", "-1"))
        include_thinking = request.query_params.get("include_thinking", "0") == "1"

        conn = get_conn()
        turns = get_session_history(
            conn, session_id,
            turn_start=turn_start,
            turn_end=turn_end,
            include_thinking=include_thinking,
        )

        # Codex/Gemini history-list entries come from session files, not proxy DB.
        # If the DB has no turns, fall back to reconstructing detail from disk.
        if not turns:
            try:
                from llm_relay.api.display import _find_session_file, _parse_codex_session_history

                session_path = _find_session_file(session_id)
                if session_path and ".codex" in str(session_path):
                    turns = _parse_codex_session_history(
                        session_path,
                        include_thinking=include_thinking,
                    )
                    if turn_start > 0:
                        turns = [t for t in turns if t.get("turn_number", 0) >= turn_start]
                    if turn_end >= 0:
                        turns = [t for t in turns if t.get("turn_number", 0) <= turn_end]
            except Exception as exc:
                logger.debug("Session-file history fallback failed for %s: %s", session_id, exc)

        return _json_response({
            "session_id": session_id,
            "total_turns": len(turns),
            "turns": turns,
        })
    except ImportError:
        return _json_response({"error": "Proxy module not available"}, status=501)
    except Exception as e:
        logger.error("Failed to get session history: %s", e)
        return _json_response({"error": str(e)}, status=500)


# ── GET /api/v1/history/{session_id}/compactions ──

async def _api_history_compactions(request: Request) -> Response:
    """Return compaction events for a specific session."""
    try:
        from llm_relay.proxy.db import get_conn, get_session_compactions

        session_id = request.path_params["session_id"]
        conn = get_conn()
        compactions = get_session_compactions(conn, session_id)
        return _json_response({
            "session_id": session_id,
            "compaction_count": len(compactions),
            "compactions": compactions,
        })
    except ImportError:
        return _json_response({"error": "Proxy module not available"}, status=501)
    except Exception as e:
        logger.error("Failed to get compaction events: %s", e)
        return _json_response({"error": str(e)}, status=500)


# ── GET /api/v1/quota ──

async def _api_quota(request: Request) -> Response:
    """Return latest ratelimit quota data (Q5h/Q7d utilization + overage)."""
    try:
        from llm_relay.proxy.db import get_conn, get_latest_quota

        conn = get_conn()
        quota = get_latest_quota(conn)
        if not quota:
            return _json_response({"available": False, "message": "No ratelimit data yet"})
        return _json_response({"available": True, **quota})
    except ImportError:
        return _json_response({"error": "Proxy module not available"}, status=501)
    except Exception as e:
        logger.error("Failed to get quota: %s", e)
        return _json_response({"error": str(e)}, status=500)


# ── GET /api/v1/errors ──

async def _api_errors(request: Request) -> Response:
    """Return error rate statistics."""
    try:
        from llm_relay.proxy.db import get_conn, get_error_stats

        window = float(request.query_params.get("window", "8"))
        session_id = request.query_params.get("session_id")
        conn = get_conn()
        stats = get_error_stats(conn, session_id=session_id, window_hours=window)
        return _json_response({"window_hours": window, **stats})
    except ImportError:
        return _json_response({"error": "Proxy module not available"}, status=501)
    except Exception as e:
        logger.error("Failed to get error stats: %s", e)
        return _json_response({"error": str(e)}, status=500)


# ── GET /api/v1/cache ──

async def _api_cache(request: Request) -> Response:
    """Return cache hit rate statistics."""
    try:
        from llm_relay.proxy.db import get_conn, get_session_cache_stats

        window = float(request.query_params.get("window", "8"))
        session_id = request.query_params.get("session_id")
        conn = get_conn()
        stats = get_session_cache_stats(conn, session_id=session_id, window_hours=window)
        return _json_response({"window_hours": window, **stats})
    except ImportError:
        return _json_response({"error": "Proxy module not available"}, status=501)
    except Exception as e:
        logger.error("Failed to get cache stats: %s", e)
        return _json_response({"error": str(e)}, status=500)


# ── GET /api/v1/ttl ──

async def _api_ttl(request: Request) -> Response:
    """Return TTL tier detection (1h vs 5m ephemeral cache)."""
    try:
        from llm_relay.proxy.db import get_conn, get_ttl_tier

        session_id = request.query_params.get("session_id")
        conn = get_conn()
        ttl = get_ttl_tier(conn, session_id=session_id)
        return _json_response(ttl)
    except ImportError:
        return _json_response({"error": "Proxy module not available"}, status=501)
    except Exception as e:
        logger.error("Failed to get TTL tier: %s", e)
        return _json_response({"error": str(e)}, status=500)


async def _api_history_composition(request: Request) -> Response:
    """Return per-turn composition analysis for a session."""
    if not os.getenv("LLM_RELAY_HISTORY", "") == "1":
        return _json_response(
            {"error": "History not enabled (set LLM_RELAY_HISTORY=1)"}, status=501,
        )
    try:
        from llm_relay.proxy.composition import analyze_session_composition_per_turn
        from llm_relay.proxy.db import get_conn

        session_id = request.path_params["session_id"]
        conn = get_conn()
        result = analyze_session_composition_per_turn(conn, session_id)
        if result is None:
            return _json_response(
                {"error": "No history data for session", "session_id": session_id},
                status=404,
            )
        return _json_response(result)
    except ImportError:
        return _json_response({"error": "Proxy module not available"}, status=501)
    except Exception as e:
        logger.error("Failed to get per-turn composition: %s", e)
        return _json_response({"error": str(e)}, status=500)


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
        # Surfaced data endpoints (quota, errors, cache, ttl)
        Route("/api/v1/quota", _api_quota, methods=["GET"]),
        Route("/api/v1/errors", _api_errors, methods=["GET"]),
        Route("/api/v1/cache", _api_cache, methods=["GET"]),
        Route("/api/v1/ttl", _api_ttl, methods=["GET"]),
        # Session history
        Route("/api/v1/history", _api_history_sessions, methods=["GET"]),
        Route("/api/v1/history/{session_id}", _api_history_detail, methods=["GET"]),
        Route("/api/v1/history/{session_id}/compactions", _api_history_compactions, methods=["GET"]),
        Route("/api/v1/history/{session_id}/composition", _api_history_composition, methods=["GET"]),
        Route("/api/v1/i18n", _api_i18n, methods=["GET"]),
    ]
