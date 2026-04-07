"""Pruning strategy registry and prescription composition.

Strategies are registered via the @strategy decorator and composed into
prescriptions (ordered pipelines) by tier: gentle → standard → aggressive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

# ---------- types ----------------------------------------------------------

Message = dict  # raw JSONL message dict


@dataclass
class PruneAction:
    """Single pruning action applied to a message."""

    line_index: int
    action: str  # "remove" | "replace"
    reason: str
    original_bytes: int
    pruned_bytes: int


@dataclass
class StrategyResult:
    """Outcome of running one strategy over a message list."""

    strategy_name: str
    actions: list[PruneAction] = field(default_factory=list)
    messages_removed: int = 0
    messages_replaced: int = 0
    chars_removed: int = 0

    @property
    def total_actions(self) -> int:
        return self.messages_removed + self.messages_replaced


@dataclass
class StrategyInfo:
    """Metadata + callable for a registered strategy."""

    name: str
    description: str
    tier: str  # "gentle", "standard", "aggressive"
    estimated_savings: str  # e.g. "5-15%"
    fn: Callable[[list[Message], dict], tuple[list[Message], StrategyResult]]


# ---------- registry -------------------------------------------------------

_STRATEGIES: dict[str, StrategyInfo] = {}

TIER_ORDER = ("gentle", "standard", "aggressive")


def strategy(
    name: str,
    description: str,
    tier: str,
    estimated_savings: str = "",
):
    """Decorator to register a pruning strategy.

    The decorated function must accept (messages, config) and return
    (pruned_messages, StrategyResult).
    """

    def wrapper(fn: Callable) -> Callable:
        _STRATEGIES[name] = StrategyInfo(
            name=name,
            description=description,
            tier=tier,
            estimated_savings=estimated_savings,
            fn=fn,
        )
        return fn

    return wrapper


def get_strategies(tier: str | None = None) -> list[StrategyInfo]:
    """Return registered strategies, optionally filtered by tier."""
    if tier is None:
        return list(_STRATEGIES.values())
    return [s for s in _STRATEGIES.values() if s.tier == tier]


def compose_prescription(tier: str = "standard") -> list[StrategyInfo]:
    """Build an ordered strategy pipeline up to and including *tier*.

    gentle → gentle strategies only
    standard → gentle + standard
    aggressive → gentle + standard + aggressive
    """
    if tier not in TIER_ORDER:
        raise ValueError(f"Unknown tier {tier!r}; choose from {TIER_ORDER}")

    cutoff = TIER_ORDER.index(tier)
    allowed = set(TIER_ORDER[: cutoff + 1])
    return [s for s in _STRATEGIES.values() if s.tier in allowed]


# Force-import strategy modules so @strategy decorators execute.
from llm_relay.strategies import aggressive as _agg  # noqa: E402,F401
from llm_relay.strategies import gentle as _gen  # noqa: E402,F401
from llm_relay.strategies import standard as _std  # noqa: E402,F401
