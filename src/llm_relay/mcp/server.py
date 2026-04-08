"""FastMCP server exposing 6 CLI orchestration tools over stdio transport."""

from __future__ import annotations

import json
import logging

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("llm-relay-mcp")

mcp = FastMCP(
    "llm-relay",
    instructions=(
        "CLI orchestration tools for delegating tasks to Claude Code, "
        "OpenAI Codex, and Gemini CLI. Provides smart routing, "
        "usage tracking, and multi-CLI session diagnostics."
    ),
)


def _json(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


# ── Tool 1: cli_delegate ──


@mcp.tool()
def cli_delegate(
    cli: str,
    prompt: str,
    model: str = "",
    working_dir: str = "",
    max_budget_usd: float = 0,
    timeout: int = 120,
) -> str:
    """Delegate a task to a specific CLI tool (claude, codex, or gemini).

    The CLI is invoked in headless mode via subprocess using its official binary.
    Requires the CLI to be installed and authenticated.

    Args:
        cli: Which CLI to use ("claude", "codex", or "gemini")
        prompt: The task prompt to send to the CLI
        model: Optional model override (e.g. "sonnet", "gpt-5.4", "gemini-2.5-pro")
        working_dir: Optional working directory for the CLI
        max_budget_usd: Optional budget limit in USD (claude only, 0 = no limit)
        timeout: Execution timeout in seconds (default 120)
    """
    from llm_relay.orch.discovery import discover_all
    from llm_relay.orch.executor import execute_cli, prompt_hash, prompt_preview

    # Map short names to cli_id
    cli_map = {"claude": "claude-code", "codex": "openai-codex", "gemini": "gemini-cli"}
    cli_id = cli_map.get(cli, cli)

    all_clis = discover_all()
    target = None
    for s in all_clis:
        if s.cli_id == cli_id or s.binary_name == cli:
            target = s
            break

    if target is None or not target.is_usable():
        return _json({"success": False, "error": "CLI '{}' is not available or not authenticated".format(cli)})

    result = execute_cli(
        target,
        prompt,
        model=model or None,
        working_dir=working_dir or None,
        max_budget_usd=max_budget_usd if max_budget_usd > 0 else None,
        timeout=timeout,
    )

    # Log to DB
    try:
        from llm_relay.orch.db import get_orch_conn, log_delegation
        conn = get_orch_conn()
        log_delegation(
            conn,
            cli_id=result.cli_id,
            auth_method=result.auth_method.value,
            prompt_hash=prompt_hash(prompt),
            prompt_preview=prompt_preview(prompt),
            model=model or None,
            working_dir=working_dir or None,
            success=result.success,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            output_chars=len(result.output),
            error=result.error,
            strategy="direct",
        )
        conn.close()
    except Exception:
        logger.debug("Failed to log cli_delegate", exc_info=True)

    return _json({
        "success": result.success,
        "cli_id": result.cli_id,
        "output": result.output,
        "error": result.error,
        "duration_ms": round(result.duration_ms, 1),
        "exit_code": result.exit_code,
    })


# ── Tool 2: cli_status ──


@mcp.tool()
def cli_status() -> str:
    """Check which CLI tools are installed and authenticated.

    Returns the status of all registered CLI tools (Claude Code, Codex, Gemini)
    including installation path, authentication status, and preferred auth method.
    """
    from llm_relay.orch.discovery import discover_all

    statuses = discover_all()
    return _json([
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


# ── Tool 3: cli_probe ──


@mcp.tool()
def cli_probe(cli: str) -> str:
    """Deep probe of a specific CLI: version, auth status, default model, binary path.

    Args:
        cli: Which CLI to probe ("claude", "codex", or "gemini")
    """
    from llm_relay.orch.discovery import discover_all

    cli_map = {"claude": "claude-code", "codex": "openai-codex", "gemini": "gemini-cli"}
    cli_id = cli_map.get(cli, cli)

    for s in discover_all():
        if s.cli_id == cli_id or s.binary_name == cli:
            return _json({
                "cli_id": s.cli_id,
                "binary_name": s.binary_name,
                "binary_path": s.binary_path,
                "installed": s.installed,
                "authenticated": s.cli_authenticated,
                "api_key_name": s.api_key_name,
                "api_key_available": s.api_key_available,
                "preferred_auth": s.preferred_auth.value,
                "version": s.version,
                "usable": s.is_usable(),
            })

    return _json({"error": "CLI '{}' not found in registry".format(cli)})


# ── Tool 4: orch_delegate ──


@mcp.tool()
def orch_delegate(
    prompt: str,
    strategy: str = "auto",
    preferred_cli: str = "",
) -> str:
    """Smart delegation — automatically picks the best available CLI based on strategy.

    Strategies:
    - auto: Smart selection (strongest model first)
    - fastest: Shortest response time (typically Gemini)
    - cheapest: Prefer subscription CLIs (no extra cost)
    - strongest: Most capable model (typically Claude)
    - round_robin: Rotate through available CLIs

    Args:
        prompt: The task to delegate
        strategy: Routing strategy (default "auto")
        preferred_cli: Optional preferred CLI hint ("claude", "codex", "gemini")
    """
    from llm_relay.orch.models import DelegationRequest, DelegationStrategy
    from llm_relay.orch.router import route

    # Map strategy string to enum
    strategy_map = {
        "auto": DelegationStrategy.AUTO,
        "fastest": DelegationStrategy.FASTEST,
        "cheapest": DelegationStrategy.CHEAPEST,
        "strongest": DelegationStrategy.STRONGEST,
        "round_robin": DelegationStrategy.ROUND_ROBIN,
    }
    strat = strategy_map.get(strategy, DelegationStrategy.AUTO)

    # Map short name to cli_id
    cli_map = {"claude": "claude-code", "codex": "openai-codex", "gemini": "gemini-cli"}
    pref = cli_map.get(preferred_cli, preferred_cli) if preferred_cli else None

    request = DelegationRequest(
        prompt=prompt,
        preferred_cli=pref,
        strategy=strat,
    )

    result = route(request)

    return _json({
        "success": result.success,
        "cli_id": result.cli_id,
        "auth_method": result.auth_method.value,
        "output": result.output,
        "error": result.error,
        "duration_ms": round(result.duration_ms, 1),
        "exit_code": result.exit_code,
        "strategy": strategy,
    })


# ── Tool 5: orch_history ──


@mcp.tool()
def orch_history(limit: int = 20) -> str:
    """Recent delegation history with success/failure, duration, tokens used.

    Args:
        limit: Number of recent delegations to return (default 20)
    """
    from llm_relay.orch.db import get_delegation_history, get_orch_conn

    try:
        conn = get_orch_conn()
        history = get_delegation_history(conn, limit=limit)
        conn.close()
        return _json({"count": len(history), "delegations": history})
    except Exception as e:
        return _json({"error": str(e), "delegations": []})


# ── Tool 6: relay_stats ──


@mcp.tool()
def relay_stats(window_hours: float = 8) -> str:
    """Token usage, cost, and error rate statistics for recent delegations.

    Args:
        window_hours: How many hours to look back (default 8)
    """
    from llm_relay.orch.db import get_delegation_stats, get_orch_conn

    try:
        conn = get_orch_conn()
        stats = get_delegation_stats(conn, window_hours=window_hours)
        conn.close()
        return _json(stats)
    except Exception as e:
        return _json({"error": str(e)})
