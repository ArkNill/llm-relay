"""Guard daemon -- context size monitoring with threshold-based intervention.

Absorbs the guard concept from cozempic: continuous monitoring of session
context growth with 4-tier thresholds for automated response.

Runs as an asyncio task inside the proxy's lifespan (no separate process).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("llm-relay.guard")


@dataclass
class SessionState:
    """Per-session tracking state."""

    session_id: str
    estimated_context_chars: int = 0
    turn_count: int = 0
    last_prune_ts: float = 0
    last_checkpoint_ts: float = 0
    alerts_sent: set[str] = field(default_factory=set)


@dataclass
class GuardConfig:
    """Guard daemon configuration."""

    enabled: bool = False
    mode: str = "passive"  # "passive" (log only) | "active" (can modify)
    interval_s: int = 30
    checkpoint_dir: Path = field(default_factory=lambda: Path.home() / ".llm-relay" / "checkpoints")

    # 4-tier thresholds (% of estimated context window)
    # Using 200K as baseline context window size in chars
    context_window_chars: int = 800_000  # ~200K tokens * 4 chars/token

    tier_checkpoint_pct: float = 25.0
    tier_gentle_pct: float = 55.0
    tier_standard_pct: float = 80.0
    tier_aggressive_pct: float = 90.0

    @classmethod
    def from_env(cls) -> GuardConfig:
        """Load config from environment variables."""
        return cls(
            enabled=os.environ.get("LLM_RELAY_GUARD", "") == "1",
            mode=os.environ.get("LLM_RELAY_GUARD_MODE", "passive"),
            interval_s=int(os.environ.get("LLM_RELAY_GUARD_INTERVAL", "30")),
        )


class Guard:
    """Session context monitor with threshold-based responses."""

    def __init__(self, config: GuardConfig | None = None):
        self.config = config or GuardConfig.from_env()
        self.sessions: dict[str, SessionState] = {}

    def update_session(
        self, session_id: str, request_body_bytes: int, message_count: int = 0
    ) -> None:
        """Called by the proxy after each request to update session tracking."""
        if not session_id:
            return

        state = self.sessions.setdefault(session_id, SessionState(session_id=session_id))
        state.estimated_context_chars = request_body_bytes
        state.turn_count += 1

    def check_thresholds(self, session_id: str) -> str | None:
        """Check if a session has crossed any threshold.

        Returns the tier name if a NEW threshold was crossed, else None.
        """
        state = self.sessions.get(session_id)
        if not state:
            return None

        pct = (state.estimated_context_chars / self.config.context_window_chars) * 100

        if pct >= self.config.tier_aggressive_pct and "aggressive" not in state.alerts_sent:
            state.alerts_sent.add("aggressive")
            return "aggressive"
        if pct >= self.config.tier_standard_pct and "standard" not in state.alerts_sent:
            state.alerts_sent.add("standard")
            return "standard"
        if pct >= self.config.tier_gentle_pct and "gentle" not in state.alerts_sent:
            state.alerts_sent.add("gentle")
            return "gentle"
        if pct >= self.config.tier_checkpoint_pct and "checkpoint" not in state.alerts_sent:
            state.alerts_sent.add("checkpoint")
            return "checkpoint"

        return None

    def checkpoint_session(self, session_id: str) -> Path | None:
        """Create a checkpoint backup of the session's JSONL file."""
        # Find the session JSONL
        base = Path.home() / ".claude" / "projects"
        if not base.exists():
            return None

        for jsonl_path in base.rglob("*.jsonl"):
            # Check if this file contains the session ID
            try:
                with open(jsonl_path, encoding="utf-8") as f:
                    first_line = f.readline()
                    if session_id in first_line:
                        # Found it -- checkpoint
                        self.config.checkpoint_dir.mkdir(parents=True, exist_ok=True)
                        ts = int(time.time())
                        dest = self.config.checkpoint_dir / f"{session_id[:12]}_{ts}.jsonl"
                        shutil.copy2(jsonl_path, dest)
                        logger.info("Checkpoint: %s → %s", jsonl_path.name, dest.name)
                        return dest
            except OSError:
                continue

        return None

    def get_session_status(self, session_id: str) -> dict:
        """Return current status for a session."""
        state = self.sessions.get(session_id)
        if not state:
            return {"session_id": session_id, "status": "unknown"}

        pct = (state.estimated_context_chars / self.config.context_window_chars) * 100
        return {
            "session_id": session_id,
            "context_chars": state.estimated_context_chars,
            "context_pct": round(pct, 1),
            "turn_count": state.turn_count,
            "alerts": sorted(state.alerts_sent),
        }

    def get_all_status(self) -> list[dict]:
        """Return status for all tracked sessions."""
        return [self.get_session_status(sid) for sid in self.sessions]


# ---------------------------------------------------------------------------
# Async loop for integration with proxy
# ---------------------------------------------------------------------------


async def guard_loop(guard: Guard) -> None:
    """Periodic guard check loop.  Run as an asyncio task inside proxy lifespan."""
    interval = guard.config.interval_s
    logger.info("Guard daemon started (mode=%s, interval=%ds)", guard.config.mode, interval)

    while True:
        await asyncio.sleep(interval)

        for sid, state in list(guard.sessions.items()):
            tier = guard.check_thresholds(sid)
            if tier is None:
                continue

            pct = (state.estimated_context_chars / guard.config.context_window_chars) * 100

            if tier == "checkpoint":
                logger.info(
                    "Session %s at %.0f%% context -- checkpointing",
                    sid[:12], pct,
                )
                guard.checkpoint_session(sid)
            elif guard.config.mode == "passive":
                logger.warning(
                    "Session %s at %.0f%% context -- recommend %s prune",
                    sid[:12], pct, tier,
                )
            else:
                # Active mode: trigger prune
                logger.warning(
                    "Session %s at %.0f%% context -- triggering %s prune",
                    sid[:12], pct, tier,
                )
                # In active mode, we would call the pruner here.
                # For now, just checkpoint + log.
                guard.checkpoint_session(sid)
