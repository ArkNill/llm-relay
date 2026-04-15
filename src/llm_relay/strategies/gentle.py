"""Gentle pruning strategies -- safe, high-confidence removals.

These remove data that is definitively redundant: already-summarised
pre-compaction history, progress ticks, duplicate file snapshots,
and billing/timing metadata that Claude never reads.
"""

from __future__ import annotations

import json

from llm_relay.strategies import Message, PruneAction, StrategyResult, strategy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PROTECTED_TYPES = frozenset({"summary", "isCompactSummary"})


def _is_protected(msg: Message) -> bool:
    """Messages that must never be pruned."""
    return msg.get("type") in _PROTECTED_TYPES or msg.get("isCompactSummary") is True


def _byte_size(msg: Message) -> int:
    return len(json.dumps(msg, ensure_ascii=False))


# ---------------------------------------------------------------------------
# 1. compact-summary-collapse
# ---------------------------------------------------------------------------


@strategy(
    name="compact-summary-collapse",
    description="Remove pre-compaction messages already captured in compact summaries",
    tier="gentle",
    estimated_savings="40-90%",
)
def compact_summary_collapse(
    messages: list[Message], config: dict
) -> tuple[list[Message], StrategyResult]:
    """Find the latest compaction boundary and remove everything before it
    (except the summary itself)."""

    boundary_idx = -1
    for i, msg in enumerate(messages):
        if msg.get("isCompactSummary") is True or msg.get("type") == "summary":
            boundary_idx = i

    if boundary_idx <= 0:
        return messages, StrategyResult(strategy_name="compact-summary-collapse")

    kept: list[Message] = []
    actions: list[PruneAction] = []
    removed_chars = 0

    for i, msg in enumerate(messages):
        if i < boundary_idx:
            sz = _byte_size(msg)
            actions.append(
                PruneAction(
                    line_index=i,
                    action="remove",
                    reason="pre-compaction message",
                    original_bytes=sz,
                    pruned_bytes=0,
                )
            )
            removed_chars += sz
        else:
            kept.append(msg)

    return kept, StrategyResult(
        strategy_name="compact-summary-collapse",
        actions=actions,
        messages_removed=len(actions),
        chars_removed=removed_chars,
    )


# ---------------------------------------------------------------------------
# 2. progress-collapse
# ---------------------------------------------------------------------------


@strategy(
    name="progress-collapse",
    description="Collapse consecutive progress/status tick messages into the last one",
    tier="gentle",
    estimated_savings="20-48%",
)
def progress_collapse(
    messages: list[Message], config: dict
) -> tuple[list[Message], StrategyResult]:
    """Remove intermediate progress ticks, keeping only the last of each run."""

    kept: list[Message] = []
    actions: list[PruneAction] = []
    removed_chars = 0

    i = 0
    while i < len(messages):
        if messages[i].get("type") == "progress" and not _is_protected(messages[i]):
            # Scan ahead for consecutive progress
            run_start = i
            while (
                i + 1 < len(messages)
                and messages[i + 1].get("type") == "progress"
                and not _is_protected(messages[i + 1])
            ):
                i += 1

            # Keep only the last of the run
            for j in range(run_start, i):
                sz = _byte_size(messages[j])
                actions.append(
                    PruneAction(
                        line_index=j,
                        action="remove",
                        reason="intermediate progress tick",
                        original_bytes=sz,
                        pruned_bytes=0,
                    )
                )
                removed_chars += sz
            kept.append(messages[i])  # keep last
        else:
            kept.append(messages[i])
        i += 1

    return kept, StrategyResult(
        strategy_name="progress-collapse",
        actions=actions,
        messages_removed=len(actions),
        chars_removed=removed_chars,
    )


# ---------------------------------------------------------------------------
# 3. file-history-dedup
# ---------------------------------------------------------------------------


@strategy(
    name="file-history-dedup",
    description="Deduplicate repeated file-history-snapshot entries by messageId",
    tier="gentle",
    estimated_savings="3-6%",
)
def file_history_dedup(
    messages: list[Message], config: dict
) -> tuple[list[Message], StrategyResult]:
    """When the same file-history-snapshot appears multiple times,
    keep only the latest occurrence."""

    # First pass: find the last occurrence of each messageId
    last_seen: dict[str, int] = {}
    for i, msg in enumerate(messages):
        if msg.get("type") == "file-history-snapshot":
            mid = msg.get("messageId", "")
            if mid:
                last_seen[mid] = i

    # Second pass: remove earlier duplicates
    kept: list[Message] = []
    actions: list[PruneAction] = []
    removed_chars = 0

    for i, msg in enumerate(messages):
        if msg.get("type") == "file-history-snapshot":
            mid = msg.get("messageId", "")
            if mid and last_seen.get(mid) != i:
                sz = _byte_size(msg)
                actions.append(
                    PruneAction(
                        line_index=i,
                        action="remove",
                        reason=f"duplicate file-history-snapshot (messageId={mid[:12]})",
                        original_bytes=sz,
                        pruned_bytes=0,
                    )
                )
                removed_chars += sz
                continue
        kept.append(msg)

    return kept, StrategyResult(
        strategy_name="file-history-dedup",
        actions=actions,
        messages_removed=len(actions),
        chars_removed=removed_chars,
    )


# ---------------------------------------------------------------------------
# 4. metadata-strip
# ---------------------------------------------------------------------------

_STRIP_KEYS = frozenset(
    {
        "costUSD",
        "duration",
        "durationMs",
        "stop_reason",
        "stop_sequence",
    }
)

_USAGE_KEYS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    }
)


@strategy(
    name="metadata-strip",
    description="Strip billing/timing metadata fields that Claude never reads",
    tier="gentle",
    estimated_savings="1-3%",
)
def metadata_strip(
    messages: list[Message], config: dict
) -> tuple[list[Message], StrategyResult]:
    """Remove costUSD, duration, stop_reason, stop_sequence and usage
    sub-fields from message dicts.  The original message objects are
    modified in place (shallow copy the list if you need originals)."""

    actions: list[PruneAction] = []
    removed_chars = 0

    for i, msg in enumerate(messages):
        if _is_protected(msg):
            continue

        before = _byte_size(msg)
        changed = False

        for key in _STRIP_KEYS:
            if key in msg:
                del msg[key]
                changed = True

        # Strip usage sub-object on message.message
        inner = msg.get("message")
        if isinstance(inner, dict) and "usage" in inner:
            del inner["usage"]
            changed = True

        if changed:
            after = _byte_size(msg)
            diff = before - after
            if diff > 0:
                actions.append(
                    PruneAction(
                        line_index=i,
                        action="replace",
                        reason="metadata fields stripped",
                        original_bytes=before,
                        pruned_bytes=after,
                    )
                )
                removed_chars += diff

    return messages, StrategyResult(
        strategy_name="metadata-strip",
        actions=actions,
        messages_replaced=len(actions),
        chars_removed=removed_chars,
    )
