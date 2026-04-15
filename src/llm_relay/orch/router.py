"""Request routing -- select the best CLI based on strategy and auth priority."""

from __future__ import annotations

import logging
from typing import List, Optional

from llm_relay.orch.db import get_orch_conn, log_delegation
from llm_relay.orch.discovery import get_available
from llm_relay.orch.executor import execute_cli, prompt_hash, prompt_preview
from llm_relay.orch.models import (
    AuthMethod,
    CLIStatus,
    DelegationRequest,
    DelegationResult,
    DelegationStrategy,
)

logger = logging.getLogger(__name__)

# Strategy-based preference order
_STRENGTH_ORDER = ["claude-code", "openai-codex", "gemini-cli"]
_SPEED_ORDER = ["gemini-cli", "openai-codex", "claude-code"]
_COST_ORDER = ["gemini-cli", "openai-codex", "claude-code"]  # subscription CLIs are "free"

# Round-robin state (process-level)
_rr_index = 0


def route(request: DelegationRequest) -> DelegationResult:
    """Route a delegation request to the best available CLI.

    Priority:
    1. If preferred_cli specified and available, use it
    2. Strategy-based selection from available CLIs
    3. Auth priority within each CLI: OAuth CLI > API key > skip
    """
    available = get_available(require_auth=True)

    if not available:
        return DelegationResult(
            cli_id="none",
            auth_method=AuthMethod.NONE,
            success=False,
            output="",
            error="No authenticated CLI tools available. "
                  "Install and authenticate at least one of: claude, codex, gemini",
        )

    # Select CLI
    cli = _select_cli(available, request.strategy, request.preferred_cli)
    if cli is None:
        return DelegationResult(
            cli_id=request.preferred_cli or "none",
            auth_method=AuthMethod.NONE,
            success=False,
            output="",
            error="Preferred CLI '{}' is not available".format(request.preferred_cli),
        )

    # Execute
    result = execute_cli(
        cli,
        request.prompt,
        model=request.model,
        working_dir=request.working_dir,
        max_budget_usd=request.max_budget_usd,
        timeout=request.timeout,
    )

    # Log to DB (best-effort)
    try:
        conn = get_orch_conn()
        log_delegation(
            conn,
            cli_id=result.cli_id,
            auth_method=result.auth_method.value,
            prompt_hash=prompt_hash(request.prompt),
            prompt_preview=prompt_preview(request.prompt),
            model=request.model,
            working_dir=request.working_dir,
            success=result.success,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            output_chars=len(result.output),
            error=result.error,
            strategy=request.strategy.value,
        )
        conn.close()
    except Exception:
        logger.debug("Failed to log delegation", exc_info=True)

    return result


def _select_cli(
    available: List[CLIStatus],
    strategy: DelegationStrategy,
    preferred: Optional[str] = None,
) -> Optional[CLIStatus]:
    """Select the best CLI based on strategy."""
    global _rr_index

    # If preferred CLI is specified, try to use it
    if preferred:
        for cli in available:
            if cli.cli_id == preferred or cli.binary_name == preferred:
                return cli
        return None  # Preferred not available

    if not available:
        return None

    if strategy == DelegationStrategy.ROUND_ROBIN:
        cli = available[_rr_index % len(available)]
        _rr_index += 1
        return cli

    # Strategy-based ordering
    order = {
        DelegationStrategy.STRONGEST: _STRENGTH_ORDER,
        DelegationStrategy.FASTEST: _SPEED_ORDER,
        DelegationStrategy.CHEAPEST: _COST_ORDER,
        DelegationStrategy.AUTO: _STRENGTH_ORDER,
    }.get(strategy, _STRENGTH_ORDER)

    # Sort available CLIs by the strategy order
    available_ids = {cli.cli_id: cli for cli in available}
    for cli_id in order:
        if cli_id in available_ids:
            return available_ids[cli_id]

    # Fallback to first available
    return available[0]
