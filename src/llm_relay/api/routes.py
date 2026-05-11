"""Starlette API routes for dashboard data -- reuses existing proxy dependencies."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import urllib.request
from typing import Any, List, Optional

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from llm_relay.i18n import MESSAGES, get_lang

logger = logging.getLogger(__name__)


def _get_db_conn():
    """Get DB connection — prefer the proxy's shared connection (same WAL view).

    Falls back to a fresh connection if the proxy hasn't been initialised
    (e.g. in tests, or when the API server runs standalone).
    """
    try:
        from llm_relay.proxy import proxy as _proxy_mod
        if _proxy_mod._conn is not None:
            return _proxy_mod._conn
    except (ImportError, AttributeError):
        pass
    from llm_relay.proxy.db import get_conn
    return get_conn()


def _json_response(data: Any, status: int = 200) -> Response:
    return Response(
        json.dumps(data, ensure_ascii=False, default=str),
        status_code=status,
        media_type="application/json",
    )


def _parse_int(request: Request, name: str, default: str) -> int:
    """Parse integer query parameter, return 400 response on invalid input."""
    raw = request.query_params.get(name, default)
    try:
        return int(raw)
    except (ValueError, TypeError):
        raise _ParamError(name, raw)


def _parse_float(request: Request, name: str, default: str) -> float:
    """Parse float query parameter, return 400 response on invalid input."""
    raw = request.query_params.get(name, default)
    try:
        return float(raw)
    except (ValueError, TypeError):
        raise _ParamError(name, raw)


class _ParamError(ValueError):
    """Raised by _parse_int/_parse_float on invalid query parameters."""
    def __init__(self, name: str, value: str):
        self.name = name
        self.value = value
        super().__init__(f"Invalid parameter '{name}': '{value}'")


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

    try:
        limit = _parse_int(request, "limit", "50")
        conn = get_orch_conn()
        history = get_delegation_history(conn, limit=limit)
        conn.close()
        return _json_response({"count": len(history), "delegations": history})
    except _ParamError as pe:
        return _json_response({"error": str(pe)}, status=400)
    except Exception as e:
        logger.error("Failed to get delegation history: %s", e)
        return _json_response({"error": "Internal server error", "delegations": []}, status=500)


# ── GET /api/v1/delegations/stats ──

async def _api_delegation_stats(request: Request) -> Response:
    """Return aggregate delegation statistics."""
    from llm_relay.orch.db import get_delegation_stats, get_orch_conn

    try:
        window = _parse_float(request, "window", "24")
        conn = get_orch_conn()
        stats = get_delegation_stats(conn, window_hours=window)
        conn.close()
        return _json_response(stats)
    except _ParamError as pe:
        return _json_response({"error": str(pe)}, status=400)
    except Exception as e:
        logger.error("Failed to get delegation stats: %s", e)
        return _json_response({"error": "Internal server error"}, status=500)


# ── GET /api/v1/sessions ──

async def _api_sessions(request: Request) -> Response:
    """Return proxy session summaries (from existing proxy DB)."""
    try:
        from llm_relay.proxy.db import get_session_summary

        window = _parse_float(request, "window", "8")
        conn = _get_db_conn()
        summaries = get_session_summary(conn, window_hours=window)
        return _json_response({"count": len(summaries), "sessions": summaries})
    except ImportError:
        return _json_response({"error": "Proxy module not available", "sessions": []}, status=501)
    except _ParamError as pe:
        return _json_response({"error": str(pe)}, status=400)
    except Exception as e:
        logger.error("Failed to get sessions: %s", e)
        return _json_response({"error": "Internal server error", "sessions": []}, status=500)


# ── Zone classification (moved to _zones.py) ──

from llm_relay.api._zones import (  # noqa: F401, E402 (re-exported for tests + display)
    _CACHED_TOKEN_A_HARD,
    _CACHED_TOKEN_A_ORANGE,
    _CACHED_TOKEN_A_RED,
    _CACHED_TOKEN_A_YELLOW,
    _CACHED_TOKEN_CEILING,
    _ZONE_ORDER,
    _classify_zone,
    _classify_zone_absolute,
    _classify_zone_ratio,
    _compute_zone_bundle,
    _overall_zone,
)


def _win32_liveness_fallback(last_ts: Optional[float], now_ts: float) -> bool:
    """Windows fallback: treat CC session as alive if recently active + claude process exists.

    On Linux, terminal/proc-based liveness is accurate and this is never called.
    On Windows, /proc doesn't exist and psutil open_files() requires admin, so we
    use mtime proximity + process existence as a best-effort substitute.
    """
    if sys.platform != "win32" or not last_ts:
        return False
    if (now_ts - last_ts) >= 600:
        return False
    try:
        from llm_relay.api._compat import is_cli_running_cached
        return is_cli_running_cached("claude")
    except ImportError:
        return False


async def _api_turns(request: Request) -> Response:
    """Return turn count + 4 token metrics + dual-zone classification for a session."""
    try:
        from llm_relay.proxy.db import (
            _duration_seconds,
            _ts_to_epoch,
            get_session_cache_stats,
            get_ttl_tier,
            get_turn_count,
        )

        session_id = request.path_params["session_id"]
        conn = _get_db_conn()
        data = get_turn_count(conn, session_id)
        turns = data["turns"]

        duration_s = 0.0
        if data["first_ts"] and data["last_ts"]:
            duration_s = _duration_seconds(data["last_ts"] - data["first_ts"])

        zones = _compute_zone_bundle(data["current_ctx"], data["peak_ctx"])
        cache = get_session_cache_stats(conn, session_id=session_id)
        ttl = get_ttl_tier(conn, session_id=session_id)

        return _json_response({
            "session_id": session_id,
            "turns": turns,
            "first_ts": _ts_to_epoch(data["first_ts"]),
            "last_ts": _ts_to_epoch(data["last_ts"]),
            "duration_s": round(duration_s, 1),
            # 4 token metrics
            "current_ctx": data["current_ctx"],
            "peak_ctx": data["peak_ctx"],
            "recent_peak": data["recent_peak"],
            "cumul_unique": data["cumul_unique"],
            # Ceiling for ratio display on the client
            "ceiling": _CACHED_TOKEN_CEILING,
            # Model metadata (consistent with Codex/Gemini)
            "model_window": 0,
            "official_context_window": 0,
            "official_max_output": 0,
            # Cache hit rate
            "cache_hit_rate": cache["cache_hit_rate"],
            # TTL tier
            "ttl_tier": ttl["tier"],
            # Zone bundle (zone, zone_a*, zone_b*, zone_{a,b}_peak, legacy message/next_threshold)
            **zones,
        })
    except ImportError:
        return _json_response({"error": "Proxy module not available"}, status=501)
    except Exception as e:
        logger.error("Failed to get turn count: %s", e)
        return _json_response({"error": "Internal server error"}, status=500)


# ── GET /api/v1/turns (all sessions) ──

async def _api_turns_all(request: Request) -> Response:
    """Return turn counts + token metrics + dual-zone classification for active sessions.

    Filters out dead sessions (CC process exited) using the same liveness logic
    as /api/v1/display so the dashboard Turn Monitor doesn't accumulate zombies.
    Use ?include_dead=1 to bypass the filter.
    """
    try:
        from llm_relay.api.display import check_cc_session_alive, collect_owned_cc_pids
        from llm_relay.proxy.db import _duration_seconds, _ts_to_epoch, get_all_session_terminals, get_all_turn_counts

        window = _parse_float(request, "window", "4")
        include_dead = request.query_params.get("include_dead", "0") == "1"
        conn = _get_db_conn()
        rows = get_all_turn_counts(conn, window_hours=window)
        terminals = get_all_session_terminals(conn)
        ceiling = _CACHED_TOKEN_CEILING

        owned_cc_pids = collect_owned_cc_pids(terminals)
        now_ts = time.time()

        sessions = []
        for r in rows:
            term = terminals.get(r["session_id"]) or {}
            last_ts_epoch = _ts_to_epoch(r["last_ts"])
            alive = check_cc_session_alive(term, last_ts_epoch, owned_cc_pids, now_ts)
            if not alive:
                alive = _win32_liveness_fallback(last_ts_epoch, now_ts)
            if not alive and not include_dead:
                continue
            duration_s = 0.0
            if r["first_ts"] and r["last_ts"]:
                duration_s = _duration_seconds(r["last_ts"] - r["first_ts"])
            zones = _compute_zone_bundle(r["current_ctx"], r["peak_ctx"])
            sessions.append({
                "session_id": r["session_id"],
                "term_name": term.get("term_name"),
                "tty": term.get("tty"),
                "turns": r["turns"],
                "first_ts": _ts_to_epoch(r["first_ts"]),
                "last_ts": last_ts_epoch,
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
        return _json_response({"error": "Internal server error", "sessions": []}, status=500)


# ── POST /api/v1/session-terminal ──

async def _api_session_terminal(request: Request) -> Response:
    """Upsert terminal info for a session (called by Stop hook).

    When a new session registers the same cc_pid as an older session
    (terminal reuse), the old session's cc_pid/tty are automatically
    cleared so it no longer appears alive on the display page.
    """
    try:
        from llm_relay.proxy.db import upsert_session_terminal

        body = await request.json()
        session_id = body.get("session_id")
        if not session_id or not isinstance(session_id, str):
            return _json_response({"error": "session_id required (string)"}, status=400)
        if len(session_id) > 128:
            return _json_response({"error": "session_id too long"}, status=400)

        # Validate optional fields
        cc_pid = body.get("cc_pid")
        term_pid = body.get("term_pid")
        tty = body.get("tty")
        term_name = body.get("term_name")

        if cc_pid is not None:
            try:
                cc_pid = int(cc_pid)
            except (ValueError, TypeError):
                return _json_response({"error": "cc_pid must be integer"}, status=400)
        if term_pid is not None:
            try:
                term_pid = int(term_pid)
            except (ValueError, TypeError):
                return _json_response({"error": "term_pid must be integer"}, status=400)
        if tty is not None and (not isinstance(tty, str) or len(tty) > 256):
            return _json_response({"error": "tty must be string <= 256 chars"}, status=400)
        if term_name is not None and (not isinstance(term_name, str) or len(term_name) > 256):
            return _json_response({"error": "term_name must be string <= 256 chars"}, status=400)

        conn = _get_db_conn()
        upsert_session_terminal(
            conn,
            session_id=session_id,
            tty=tty,
            cc_pid=cc_pid,
            term_pid=term_pid,
            term_name=term_name,
        )
        return _json_response({"ok": True, "session_id": session_id})
    except ImportError:
        return _json_response({"error": "Proxy module not available"}, status=501)
    except Exception as e:
        logger.error("Failed to upsert session terminal: %s", e)
        return _json_response({"error": "Internal server error"}, status=500)


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
            _duration_seconds,
            _ts_to_epoch,
            get_all_session_terminals,
            get_all_turn_counts,
            get_session_cache_stats,
            get_ttl_tier,
        )

        window = _parse_float(request, "window", "4")
        include_dead = request.query_params.get("include_dead", "0") == "1"
        conn = _get_db_conn()
        rows = get_all_turn_counts(conn, window_hours=window)
        terminals = get_all_session_terminals(conn)
        ceiling = _CACHED_TOKEN_CEILING

        owned_cc_pids = collect_owned_cc_pids(terminals)
        now_ts = time.time()

        sessions = []
        proxy_session_ids = set()  # type: set
        for r in rows:
            term = terminals.get(r["session_id"]) or {}
            last_ts_epoch = _ts_to_epoch(r["last_ts"])
            alive = check_cc_session_alive(term, last_ts_epoch, owned_cc_pids, now_ts)
            if not alive:
                alive = _win32_liveness_fallback(last_ts_epoch, now_ts)
            if not alive and not include_dead:
                continue
            proxy_session_ids.add(r["session_id"])

            duration_s = 0.0
            if r["first_ts"] and r["last_ts"]:
                duration_s = _duration_seconds(r["last_ts"] - r["first_ts"])
            prompt_info = get_last_user_prompt(r["session_id"])
            zones = _compute_zone_bundle(r["current_ctx"], r["peak_ctx"])
            cache = get_session_cache_stats(conn, session_id=r["session_id"])
            ttl = get_ttl_tier(conn, session_id=r["session_id"])
            sessions.append({
                "session_id": r["session_id"],
                "provider": "claude-code",
                "provider_name": "Claude Code",
                "turns": r["turns"],
                "first_ts": _ts_to_epoch(r["first_ts"]),
                "last_ts": last_ts_epoch,
                "duration_s": round(duration_s, 1),
                # 4 token metrics
                "current_ctx": r["current_ctx"],
                "peak_ctx": r["peak_ctx"],
                "recent_peak": r["recent_peak"],
                "cumul_unique": r["cumul_unique"],
                "ceiling": ceiling,
                # Model metadata (consistent with Codex/Gemini)
                "model_window": 0,
                "official_context_window": 0,
                "official_max_output": 0,
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
                "alive": alive,
                # Connection type (ssh, tmux, ssh+tmux, native, etc.)
                "connection_type": detect_connection_type(term.get("cc_pid") or 0),
                # Context composition (requires LLM_RELAY_HISTORY=1)
                "composition": _get_composition_safe(conn, r["session_id"]),
            })

        # Merge Codex/Gemini (and CC on Windows) sessions discovered from session files
        try:
            external = discover_external_cli_sessions(
                window_hours=window, include_dead=include_dead,
            )
            # Deduplicate: skip file-based CC sessions already in proxy DB
            for ext in external:
                if ext.get("session_id") not in proxy_session_ids:
                    sessions.append(ext)
        except Exception as exc:
            logger.debug("External CLI session discovery failed: %s", exc)

        return _json_response({"count": len(sessions), "sessions": sessions})
    except ImportError:
        return _json_response({"error": "Proxy module not available", "sessions": []}, status=501)
    except Exception as e:
        logger.error("Failed to get display data: %s", e)
        return _json_response({"error": "Internal server error", "sessions": []}, status=500)


# ── GET /api/v1/cost ──

async def _api_cost(request: Request) -> Response:
    """Return cost breakdown from proxy DB."""
    try:
        from llm_relay.proxy.db import get_conn  # noqa: F401 (availability check)

        window = _parse_float(request, "window", "24")
        import time
        cutoff = time.time() - (window * 3600)
        conn = _get_db_conn()
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
    except _ParamError as pe:
        return _json_response({"error": str(pe)}, status=400)
    except Exception as e:
        logger.error("Failed to get cost data: %s", e)
        return _json_response({"error": "Internal server error"}, status=500)


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
        conn = _get_db_conn()
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
        from llm_relay.proxy.db import get_history_sessions

        window = _parse_float(request, "window", "24")
        conn = _get_db_conn()
        sessions = get_history_sessions(conn, window_hours=window)
        ceiling = _CACHED_TOKEN_CEILING

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
        return _json_response({"error": "Internal server error"}, status=500)


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
        from llm_relay.proxy.db import get_session_history

        session_id = request.path_params["session_id"]
        turn_start = _parse_int(request, "turn_start", "0")
        turn_end = _parse_int(request, "turn_end", "-1")
        include_thinking = request.query_params.get("include_thinking", "0") == "1"

        conn = _get_db_conn()
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
    except _ParamError as pe:
        return _json_response({"error": str(pe)}, status=400)
    except Exception as e:
        logger.error("Failed to get session history: %s", e)
        return _json_response({"error": "Internal server error"}, status=500)


# ── GET /api/v1/history/{session_id}/compactions ──

async def _api_history_compactions(request: Request) -> Response:
    """Return compaction events for a specific session."""
    try:
        from llm_relay.proxy.db import get_session_compactions

        session_id = request.path_params["session_id"]
        conn = _get_db_conn()
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
        return _json_response({"error": "Internal server error"}, status=500)


# ── GET /api/v1/quota ──

async def _api_quota(request: Request) -> Response:
    """Return latest ratelimit quota data (Q5h/Q7d utilization + overage)."""
    try:
        from llm_relay.proxy.db import get_latest_quota

        conn = _get_db_conn()
        quota = get_latest_quota(conn)
        if not quota:
            return _json_response({"available": False, "message": "No ratelimit data yet"})
        return _json_response({"available": True, **quota})
    except ImportError:
        return _json_response({"error": "Proxy module not available"}, status=501)
    except Exception as e:
        logger.error("Failed to get quota: %s", e)
        return _json_response({"error": "Internal server error"}, status=500)


# ── GET /api/v1/errors ──

async def _api_errors(request: Request) -> Response:
    """Return error rate statistics."""
    try:
        from llm_relay.proxy.db import get_error_stats

        window = _parse_float(request, "window", "8")
        session_id = request.query_params.get("session_id")
        conn = _get_db_conn()
        stats = get_error_stats(conn, session_id=session_id, window_hours=window)
        return _json_response({"window_hours": window, **stats})
    except ImportError:
        return _json_response({"error": "Proxy module not available"}, status=501)
    except _ParamError as pe:
        return _json_response({"error": str(pe)}, status=400)
    except Exception as e:
        logger.error("Failed to get error stats: %s", e)
        return _json_response({"error": "Internal server error"}, status=500)


# ── GET /api/v1/cache ──

async def _api_cache(request: Request) -> Response:
    """Return cache hit rate statistics."""
    try:
        from llm_relay.proxy.db import get_session_cache_stats

        window = _parse_float(request, "window", "8")
        session_id = request.query_params.get("session_id")
        conn = _get_db_conn()
        stats = get_session_cache_stats(conn, session_id=session_id, window_hours=window)
        return _json_response({"window_hours": window, **stats})
    except ImportError:
        return _json_response({"error": "Proxy module not available"}, status=501)
    except _ParamError as pe:
        return _json_response({"error": str(pe)}, status=400)
    except Exception as e:
        logger.error("Failed to get cache stats: %s", e)
        return _json_response({"error": "Internal server error"}, status=500)


# ── GET /api/v1/ttl ──

async def _api_ttl(request: Request) -> Response:
    """Return TTL tier detection (1h vs 5m ephemeral cache)."""
    try:
        from llm_relay.proxy.db import get_ttl_tier

        session_id = request.query_params.get("session_id")
        conn = _get_db_conn()
        ttl = get_ttl_tier(conn, session_id=session_id)
        return _json_response(ttl)
    except ImportError:
        return _json_response({"error": "Proxy module not available"}, status=501)
    except Exception as e:
        logger.error("Failed to get TTL tier: %s", e)
        return _json_response({"error": "Internal server error"}, status=500)


async def _api_history_composition(request: Request) -> Response:
    """Return per-turn composition analysis for a session."""
    if not os.getenv("LLM_RELAY_HISTORY", "") == "1":
        return _json_response(
            {"error": "History not enabled (set LLM_RELAY_HISTORY=1)"}, status=501,
        )
    try:
        from llm_relay.proxy.composition import analyze_session_composition_per_turn
        from llm_relay.proxy.db import get_conn  # noqa: F401 (availability check)

        session_id = request.path_params["session_id"]
        conn = _get_db_conn()
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
        return _json_response({"error": "Internal server error"}, status=500)


# ── Upstream provider status (proxied + 60s-cached) ──

_STATUS_TTL_S = 60.0

# Per-provider cache: {provider: (expiry_epoch, payload)}
_status_cache = {}  # type: dict

_ANTHROPIC_STATUS_URL = os.getenv(
    "LLM_RELAY_ANTHROPIC_STATUS_URL",
    "https://status.claude.com/api/v2/summary.json",
)
_OPENAI_STATUS_URL = os.getenv(
    "LLM_RELAY_OPENAI_STATUS_URL",
    "https://status.openai.com/api/v2/summary.json",
)
_GEMINI_STATUS_URL = os.getenv(
    "LLM_RELAY_GEMINI_STATUS_URL",
    "https://status.cloud.google.com/incidents.json",
)


def _fetch_statuspage_sync(url: str, source_url: str, label: str, now: float) -> dict:
    """Fetch from Statuspage.io-compatible API (Anthropic, OpenAI)."""
    try:
        req = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": "llm-relay-dashboard"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read())
        status = payload.get("status", {}) or {}
        incidents = payload.get("incidents", []) or []
        return {
            "indicator": status.get("indicator", "unknown"),
            "description": status.get("description", ""),
            "incidents": [
                {
                    "name": i.get("name", ""),
                    "status": i.get("status", ""),
                    "impact": i.get("impact", ""),
                    "updated_at": i.get("updated_at", ""),
                    "shortlink": i.get("shortlink", ""),
                }
                for i in incidents
            ],
            "last_fetched": now,
            "source_url": source_url,
            "fetch_ok": True,
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug("%s status fetch failed: %s", label, exc)
        return {
            "indicator": "unknown",
            "description": "Status fetch failed",
            "incidents": [],
            "last_fetched": now,
            "source_url": source_url,
            "fetch_ok": False,
            "error": str(exc)[:200],
        }


def _fetch_google_status_sync(url: str, now: float) -> dict:
    """Fetch from Google Cloud Status API and normalize to Statuspage.io shape."""
    try:
        req = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": "llm-relay-dashboard"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            all_incidents = json.loads(resp.read())
        # Filter active incidents related to Vertex AI / Gemini / AI Platform
        ai_keywords = ("vertex", "gemini", "ai platform", "generative", "aiplatform")
        active = []
        for inc in (all_incidents if isinstance(all_incidents, list) else []):
            if inc.get("end"):
                continue
            desc = (inc.get("external_desc", "") + " " + inc.get("service_name", "")).lower()
            if any(kw in desc for kw in ai_keywords) or inc.get("service_name", "") == "Multiple Products":
                severity = inc.get("severity", "low")
                active.append({
                    "name": inc.get("external_desc", "")[:120],
                    "status": inc.get("status_impact", ""),
                    "impact": {"high": "major", "medium": "minor", "low": "minor"}.get(severity, "minor"),
                    "updated_at": inc.get("modified", ""),
                    "shortlink": "",
                })
        severity_map = {"major": 3, "minor": 1}
        worst = max((severity_map.get(a["impact"], 0) for a in active), default=0)
        indicator = {3: "major", 1: "minor"}.get(worst, "none")
        return {
            "indicator": indicator,
            "description": "All Systems Operational" if indicator == "none" else active[0]["name"] if active else "",
            "incidents": active[:5],
            "last_fetched": now,
            "source_url": "https://status.cloud.google.com",
            "fetch_ok": True,
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug("Gemini status fetch failed: %s", exc)
        return {
            "indicator": "unknown",
            "description": "Status fetch failed",
            "incidents": [],
            "last_fetched": now,
            "source_url": "https://status.cloud.google.com",
            "fetch_ok": False,
            "error": str(exc)[:200],
        }


async def _api_provider_status(provider: str, fetch_fn, request: Request) -> Response:
    """Generic cached status endpoint."""
    now = time.time()
    cached = _status_cache.get(provider)
    if cached is not None and cached[0] > now:
        return _json_response(cached[1])
    data = await asyncio.to_thread(fetch_fn, now)
    _status_cache[provider] = (now + _STATUS_TTL_S, data)
    return _json_response(data)


async def _api_anthropic_status(request: Request) -> Response:
    return await _api_provider_status(
        "anthropic",
        lambda now: _fetch_statuspage_sync(_ANTHROPIC_STATUS_URL, "https://status.claude.com", "Anthropic", now),
        request,
    )


async def _api_openai_status(request: Request) -> Response:
    return await _api_provider_status(
        "openai",
        lambda now: _fetch_statuspage_sync(_OPENAI_STATUS_URL, "https://status.openai.com", "OpenAI", now),
        request,
    )


async def _api_gemini_status(request: Request) -> Response:
    return await _api_provider_status(
        "gemini",
        lambda now: _fetch_google_status_sync(_GEMINI_STATUS_URL, now),
        request,
    )


async def _api_i18n(request: Request) -> Response:
    """Return i18n message catalog for the requested locale."""
    lang = request.query_params.get("lang", get_lang())
    msgs = MESSAGES.get(lang, MESSAGES["en"])
    return _json_response({"lang": lang, "messages": msgs})


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
        # Upstream provider status (proxied + cached 60s)
        Route("/api/v1/anthropic-status", _api_anthropic_status, methods=["GET"]),
        Route("/api/v1/openai-status", _api_openai_status, methods=["GET"]),
        Route("/api/v1/gemini-status", _api_gemini_status, methods=["GET"]),
        # i18n
        Route("/api/v1/i18n", _api_i18n, methods=["GET"]),
    ]
