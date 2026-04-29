"""Cost estimation -- per-model pricing for Anthropic API calls.

Absorbs the pricing model from kolkov/ccdiag.
"""

from __future__ import annotations

from dataclasses import dataclass

# Pricing: USD per million tokens (as of 2026-04)
# Cache read = 0.1x input price, Cache creation = 1.25x input price
_PRICING: dict[str, tuple[float, float]] = {
    # (input_per_mtok, output_per_mtok)
    "claude-opus-4-6": (15.0, 75.0),
    "claude-opus-4-5": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (0.25, 1.25),
    # Legacy
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-3-5-haiku": (0.25, 1.25),
    "claude-3-opus": (15.0, 75.0),
}

_CACHE_READ_FACTOR = 0.1
_CACHE_CREATE_FACTOR = 1.25


@dataclass
class CostEstimate:
    """Breakdown of estimated cost for a single API call."""

    model: str
    input_cost: float
    output_cost: float
    cache_create_cost: float
    cache_read_cost: float

    @property
    def total(self) -> float:
        return self.input_cost + self.output_cost + self.cache_create_cost + self.cache_read_cost


def _match_model(model: str) -> tuple[float, float]:
    """Find pricing for a model name, with fuzzy matching."""
    if not model:
        return (3.0, 15.0)  # default to Sonnet pricing

    # Exact match
    if model in _PRICING:
        return _PRICING[model]

    # Partial match
    model_lower = model.lower()
    for key, pricing in _PRICING.items():
        if key in model_lower or model_lower in key:
            return pricing

    # Family-based fallback
    if "opus" in model_lower:
        return (15.0, 75.0)
    if "haiku" in model_lower:
        return (0.25, 1.25)
    return (3.0, 15.0)  # default Sonnet


def estimate_cost(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation: int = 0,
    cache_read: int = 0,
) -> CostEstimate:
    """Estimate cost in USD for a single API call."""
    input_rate, output_rate = _match_model(model)

    return CostEstimate(
        model=model,
        input_cost=(input_tokens / 1_000_000) * input_rate,
        output_cost=(output_tokens / 1_000_000) * output_rate,
        cache_create_cost=(cache_creation / 1_000_000) * input_rate * _CACHE_CREATE_FACTOR,
        cache_read_cost=(cache_read / 1_000_000) * input_rate * _CACHE_READ_FACTOR,
    )


def estimate_session_cost(
    requests: list[dict],
    model_key: str = "model",
) -> CostEstimate:
    """Sum costs across multiple request records (from cc-relay DB rows)."""
    total = CostEstimate(model="mixed", input_cost=0, output_cost=0, cache_create_cost=0, cache_read_cost=0)
    for r in requests:
        est = estimate_cost(
            model=r.get(model_key, ""),
            input_tokens=r.get("input_tokens", 0),
            output_tokens=r.get("output_tokens", 0),
            cache_creation=r.get("cache_creation", 0),
            cache_read=r.get("cache_read", 0),
        )
        total.input_cost += est.input_cost
        total.output_cost += est.output_cost
        total.cache_create_cost += est.cache_create_cost
        total.cache_read_cost += est.cache_read_cost
    return total
