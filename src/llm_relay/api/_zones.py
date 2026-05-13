"""Token-based zone classification (Claude Code + Codex dual-scale A/B).

Provides threshold classification for live context tokens across two scales:
  A) Absolute thresholds — env LLM_TOKEN_A_YELLOW/ORANGE/RED/HARD (CC) or CODEX_TOKEN_A_* (Codex)
  B) Ratio-of-ceiling    — env LLM_TOKEN_CEILING / CODEX_TOKEN_ZONE_CEILING (50/70/90/100%)

Overall zone = worst of A and B (max).
Also contains token metric utilities shared by session parsers.
"""

from __future__ import annotations

import os
from typing import Optional

from llm_relay.i18n import t

# ── Zone ordering (shared by CC + Codex) ──

_ZONE_ORDER = {"green": 0, "yellow": 1, "orange": 2, "red": 3, "hard": 4}


# ── Claude Code zone classification ──

# Cached at module load — avoids repeated os.getenv in hot path.
# Zone A defaults aligned with Zone B ratios of the practical 665K ceiling:
#   Yellow 332K (50%) / Orange 465K (70%) / Red 600K (90%) / Hard 665K (100%).
# Rationale: Claude Code's client-side auto-compact (re-introduced in v2.1.139)
# triggers around 650-670K cumulative context. Operators rely on these zones to
# hand off to a new session before compaction degrades context continuity.
# Override via LLM_TOKEN_A_*/LLM_TOKEN_CEILING if the exact threshold is confirmed.
_CACHED_TOKEN_A_YELLOW = int(os.getenv("LLM_TOKEN_A_YELLOW", "332000"))
_CACHED_TOKEN_A_ORANGE = int(os.getenv("LLM_TOKEN_A_ORANGE", "465000"))
_CACHED_TOKEN_A_RED = int(os.getenv("LLM_TOKEN_A_RED", "600000"))
_CACHED_TOKEN_A_HARD = int(os.getenv("LLM_TOKEN_A_HARD", "665000"))
_CACHED_TOKEN_CEILING = int(os.getenv("LLM_TOKEN_CEILING", "665000"))


def _classify_zone(turns: int) -> tuple:
    """Legacy turn-based classification -- kept only for backward compatibility.

    Not used by any endpoint anymore. Turn counts are display-only now.
    """
    yellow = int(os.getenv("LLM_TURN_YELLOW", "200"))
    orange = int(os.getenv("LLM_TURN_ORANGE", "250"))
    red = int(os.getenv("LLM_TURN_RED", "300"))

    if turns >= red:
        return "red", t("zone.danger"), None, t("zone.turn.red", n=red)
    if turns >= orange:
        return "orange", t("zone.warning"), red, t("zone.turn.orange", n=orange)
    if turns >= yellow:
        return "yellow", t("zone.caution"), orange, t("zone.turn.yellow", n=yellow)
    return "green", t("zone.safe"), yellow, None


def _classify_zone_absolute(tokens: int) -> tuple:
    """Zone A -- absolute token threshold classification.

    Env: LLM_TOKEN_A_YELLOW / _A_ORANGE / _A_RED / _A_HARD
    Returns (zone, zone_label, next_threshold, message).
    """
    yellow = _CACHED_TOKEN_A_YELLOW
    orange = _CACHED_TOKEN_A_ORANGE
    red = _CACHED_TOKEN_A_RED
    hard = _CACHED_TOKEN_A_HARD

    if tokens >= hard:
        return "hard", t("zone.blocked"), None, t("zone.abs.hard", n=hard // 1000)
    if tokens >= red:
        return "red", t("zone.danger"), hard, t("zone.abs.red.cc", n=red // 1000)
    if tokens >= orange:
        return "orange", t("zone.warning"), red, t("zone.abs.orange", n=orange // 1000)
    if tokens >= yellow:
        return "yellow", t("zone.caution"), orange, t("zone.abs.yellow", n=yellow // 1000)
    return "green", t("zone.safe"), yellow, None


def _classify_zone_ratio(tokens: int, ceiling: Optional[int] = None) -> tuple:
    """Zone B -- ratio-of-ceiling classification (50/70/90/100%).

    Env: LLM_TOKEN_CEILING (default 665K — Opus 4.7 forced auto-compact ceiling.
    Override to 500K for public deployments without 1M context entitlement.)
    Returns (zone, zone_label, next_threshold, message).
    """
    if ceiling is None:
        ceiling = _CACHED_TOKEN_CEILING
    if ceiling <= 0:
        return "green", t("zone.safe"), 0, None

    yellow_t = int(ceiling * 0.50)
    orange_t = int(ceiling * 0.70)
    red_t = int(ceiling * 0.90)
    ratio = tokens / ceiling if ceiling else 0.0
    pct = int(ratio * 100)

    _kw = dict(pct=pct, cur=tokens // 1000, ceil=ceiling // 1000)
    if ratio >= 1.0:
        return "hard", t("zone.blocked"), None, t("zone.ratio.hard", **_kw)
    if ratio >= 0.90:
        return "red", t("zone.danger"), ceiling, t("zone.ratio.red.cc", **_kw)
    if ratio >= 0.70:
        return "orange", t("zone.warning"), red_t, t("zone.ratio.orange", **_kw)
    if ratio >= 0.50:
        return "yellow", t("zone.caution"), orange_t, t("zone.ratio.yellow", **_kw)
    return "green", t("zone.safe"), yellow_t, None


def _overall_zone(zone_a: str, zone_b: str) -> str:
    """Return whichever of the two zones is more severe (max by _ZONE_ORDER)."""
    if _ZONE_ORDER.get(zone_a, 0) >= _ZONE_ORDER.get(zone_b, 0):
        return zone_a
    return zone_b


def _compute_zone_bundle(current_ctx: int, peak_ctx: int, ceiling: Optional[int] = None) -> dict:
    """Compute Zone A/B on current_ctx (primary) + A/B on peak_ctx (reference).

    Returns a flat dict ready to be merged into the session response.
    """
    za_cur = _classify_zone_absolute(current_ctx)
    zb_cur = _classify_zone_ratio(current_ctx, ceiling=ceiling)
    za_peak = _classify_zone_absolute(peak_ctx)
    zb_peak = _classify_zone_ratio(peak_ctx, ceiling=ceiling)
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


# ── Codex zone classification ──

# Official OpenAI public limit for GPT-5.5 Thinking Pro / GPT-5 Codex-class
# models: 400k context with 128k max output, leaving 272k input context.
_OPENAI_CODEX_OFFICIAL_CONTEXT_WINDOW = 400_000
_OPENAI_CODEX_OFFICIAL_MAX_OUTPUT = 128_000
_OPENAI_CODEX_OFFICIAL_INPUT_WINDOW = (
    _OPENAI_CODEX_OFFICIAL_CONTEXT_WINDOW - _OPENAI_CODEX_OFFICIAL_MAX_OUTPUT
)

_CODEX_ZONE_ORDER = {"green": 0, "yellow": 1, "orange": 2, "red": 3, "hard": 4}


def _codex_display_ceiling() -> int:
    """Operator-facing ceiling for Codex session cards.

    Defaults to the official model context window (400K) so the progress bar
    reflects real model capacity, not an arbitrary Zone-A threshold.
    """
    return int(os.getenv(
        "CODEX_TOKEN_DISPLAY_CEILING",
        str(_OPENAI_CODEX_OFFICIAL_CONTEXT_WINDOW),
    ))


def _codex_zone_ceiling() -> int:
    """Runtime ceiling for ratio-based Codex zone-B classification.

    Uses the official model context window (400K) so zone-B percentages
    align with zone-A absolute thresholds and the display progress bar.
    """
    return int(os.getenv(
        "CODEX_TOKEN_ZONE_CEILING",
        str(_OPENAI_CODEX_OFFICIAL_CONTEXT_WINDOW),
    ))


def _codex_classify_absolute(tokens: int) -> tuple:
    """Classify Codex live context against absolute operator thresholds.

    Defaults calibrated to the official 400K context window:
      Yellow 200K (50%) / Orange 280K (70%) / Red 360K (90%) / Hard 400K (100%).
    """
    yellow = int(os.getenv("CODEX_TOKEN_A_YELLOW", "200000"))
    orange = int(os.getenv("CODEX_TOKEN_A_ORANGE", "280000"))
    red = int(os.getenv("CODEX_TOKEN_A_RED", "360000"))
    hard = int(os.getenv("CODEX_TOKEN_A_HARD", "400000"))

    if tokens >= hard:
        return "hard", t("zone.blocked"), None, t("zone.abs.hard", n=hard // 1000)
    if tokens >= red:
        return "red", t("zone.danger"), hard, t("zone.abs.red", n=red // 1000)
    if tokens >= orange:
        return "orange", t("zone.warning"), red, t("zone.abs.orange", n=orange // 1000)
    if tokens >= yellow:
        return "yellow", t("zone.caution"), orange, t("zone.abs.yellow", n=yellow // 1000)
    return "green", t("zone.safe"), yellow, None


def _codex_classify_ratio(tokens: int, ceiling: int) -> tuple:
    """Classify Codex live context as a ratio of the runtime ceiling.

    Messages now show the *actual* ratio and token count so the operator
    sees real numbers instead of the fixed threshold label.
    """
    if ceiling <= 0:
        return "green", t("zone.safe"), 0, None

    yellow_t = int(ceiling * 0.50)
    orange_t = int(ceiling * 0.70)
    red_t = int(ceiling * 0.90)
    ratio = tokens / ceiling if ceiling else 0.0
    pct = int(ratio * 100)

    _kw = dict(pct=pct, cur=tokens // 1000, ceil=ceiling // 1000)
    if ratio >= 1.0:
        return "hard", t("zone.blocked"), None, t("zone.ratio.hard", **_kw)
    if ratio >= 0.90:
        return "red", t("zone.danger"), ceiling, t("zone.ratio.red", **_kw)
    if ratio >= 0.70:
        return "orange", t("zone.warning"), red_t, t("zone.ratio.orange", **_kw)
    if ratio >= 0.50:
        return "yellow", t("zone.caution"), orange_t, t("zone.ratio.yellow", **_kw)
    return "green", t("zone.safe"), yellow_t, None


def _codex_compute_zone_bundle(current_ctx: int, peak_ctx: int) -> dict:
    """Compute Codex-only live-context zones without affecting Claude/Gemini paths."""
    zone_ceiling = _codex_zone_ceiling()
    zone_a = _codex_classify_absolute(current_ctx)
    zone_b = _codex_classify_ratio(current_ctx, zone_ceiling)
    zone_a_peak = _codex_classify_absolute(peak_ctx)
    zone_b_peak = _codex_classify_ratio(peak_ctx, zone_ceiling)
    zone = zone_a[0] if _CODEX_ZONE_ORDER[zone_a[0]] >= _CODEX_ZONE_ORDER[zone_b[0]] else zone_b[0]

    if _CODEX_ZONE_ORDER[zone_a[0]] >= _CODEX_ZONE_ORDER[zone_b[0]]:
        message = zone_a[3]
        next_threshold = zone_a[2]
    else:
        message = zone_b[3]
        next_threshold = zone_b[2]

    return {
        "zone": zone,
        "zone_a": zone_a[0],
        "zone_a_label": zone_a[1],
        "zone_a_message": zone_a[3],
        "zone_a_next": zone_a[2],
        "zone_b": zone_b[0],
        "zone_b_label": zone_b[1],
        "zone_b_message": zone_b[3],
        "zone_b_next": zone_b[2],
        # Keep legacy fields populated for consumers that still read them.
        "zone_a_peak": zone_a_peak[0],
        "zone_b_peak": zone_b_peak[0],
        "message": message,
        "next_threshold": next_threshold,
    }


# ── Token metric utilities (shared by session parsers) ──


def _to_int(value) -> int:
    """Best-effort int conversion for provider usage counters."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _usage_total(usage: dict) -> int:
    """Return provider-reported total_tokens, or compute a conservative total."""
    total = _to_int(usage.get("total_tokens"))
    if total:
        return total
    return (
        _to_int(usage.get("input_tokens"))
        + _to_int(usage.get("output_tokens"))
        + _to_int(usage.get("reasoning_output_tokens"))
    )


def _context_tokens_from_openai_usage(usage: dict) -> int:
    """Return the prompt/context tokens for a Codex token_count usage record."""
    return _to_int(usage.get("input_tokens"))


def _extract_codex_token_metrics(payload: dict, recent_contexts: list) -> dict:
    """Extract display metrics from a Codex event_msg token_count payload."""
    info = payload.get("info", {})
    if not isinstance(info, dict):
        return {}

    last_usage = info.get("last_token_usage", {})
    total_usage = info.get("total_token_usage", {})
    if not isinstance(last_usage, dict):
        last_usage = {}
    if not isinstance(total_usage, dict):
        total_usage = {}

    current_ctx = _context_tokens_from_openai_usage(last_usage)
    if current_ctx:
        recent_contexts.append(current_ctx)

    metrics = {
        "current_ctx": current_ctx,
        "cumul_unique": _usage_total(total_usage),
        "model_window": _to_int(info.get("model_context_window")),
    }
    return {k: v for k, v in metrics.items() if v}
