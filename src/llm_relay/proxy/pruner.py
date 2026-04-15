"""Pruning orchestrator -- compose strategies, execute pipeline, relink UUIDs.

Usage:
    from llm_relay.proxy.pruner import prune

    report = prune(messages, tier="standard")
    print(report)
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path

from llm_relay.proxy.fileutil import FileSnapshot, advisory_lock, atomic_write
from llm_relay.strategies import StrategyResult, compose_prescription


@dataclass
class PruneConfig:
    """Tuning knobs passed to individual strategies."""

    tier: str = "standard"
    # thinking-blocks
    thinking_mode: str = "remove"  # "remove" | "truncate"
    thinking_max_chars: int = 200
    # tool-output-trim
    tool_output_max_chars: int = 5000
    # tool-result-age
    max_age_turns: int = 30
    # mega-block-trim
    mega_block_chars: int = 20_000

    def as_dict(self) -> dict:
        return {
            "thinking_mode": self.thinking_mode,
            "thinking_max_chars": self.thinking_max_chars,
            "tool_output_max_chars": self.tool_output_max_chars,
            "max_age_turns": self.max_age_turns,
            "mega_block_chars": self.mega_block_chars,
        }


@dataclass
class PruneReport:
    """Aggregated result of the full pruning pipeline."""

    tier: str
    strategy_results: list[StrategyResult] = field(default_factory=list)
    messages_before: int = 0
    messages_after: int = 0
    chars_before: int = 0
    chars_after: int = 0

    @property
    def total_removed(self) -> int:
        return self.messages_before - self.messages_after

    @property
    def chars_saved(self) -> int:
        return self.chars_before - self.chars_after

    @property
    def savings_pct(self) -> float:
        if self.chars_before == 0:
            return 0.0
        return (self.chars_saved / self.chars_before) * 100

    def summary(self) -> str:
        lines = [
            f"Prune report (tier={self.tier})",
            f"  Messages: {self.messages_before} → {self.messages_after} (-{self.total_removed})",
            f"  Size: {self.chars_before:,} → {self.chars_after:,} chars"
            f" (-{self.chars_saved:,}, {self.savings_pct:.1f}%)",
            "",
        ]
        for sr in self.strategy_results:
            if sr.total_actions > 0:
                lines.append(
                    f"  \\[{sr.strategy_name}] "
                    f"removed={sr.messages_removed} replaced={sr.messages_replaced} "
                    f"chars_saved={sr.chars_removed:,}"
                )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# parentUuid re-linking (from cozempic)
# ---------------------------------------------------------------------------


def _relink_parent_uuids(
    messages: list[dict], removed_uuids: set[str]
) -> None:
    """Walk up the parent chain for orphaned children and repoint them
    to the nearest surviving ancestor.  Modifies messages in place."""

    if not removed_uuids:
        return

    # Build uuid → parentUuid map from remaining messages
    uuid_to_parent: dict[str, str] = {}
    for msg in messages:
        uid = msg.get("uuid", "")
        pid = msg.get("parentUuid", "")
        if uid:
            uuid_to_parent[uid] = pid

    # Also include removed messages so we can walk through them
    # (they were in the original list before pruning)
    # We pass removed_uuids which were collected before removal.

    surviving = set(uuid_to_parent.keys())

    for msg in messages:
        pid = msg.get("parentUuid", "")
        if not pid or pid not in removed_uuids:
            continue

        # Walk up until we find a surviving ancestor or hit root
        visited: set[str] = set()
        cursor = pid
        while cursor and cursor in removed_uuids and cursor not in visited:
            visited.add(cursor)
            cursor = uuid_to_parent.get(cursor, "")

        # Repoint
        if cursor and cursor in surviving:
            msg["parentUuid"] = cursor
        else:
            # No surviving ancestor found -- make it a root
            msg["parentUuid"] = ""


# ---------------------------------------------------------------------------
# Core prune function
# ---------------------------------------------------------------------------


def prune(
    messages: list[dict],
    tier: str = "standard",
    config: PruneConfig | None = None,
) -> tuple[list[dict], PruneReport]:
    """Run the pruning pipeline on *messages* and return (pruned, report).

    Messages are deep-copied before modification.  The original list
    is not modified.
    """
    if config is None:
        config = PruneConfig(tier=tier)

    cfg_dict = config.as_dict()

    # Deep copy to avoid mutating originals
    msgs = copy.deepcopy(messages)

    chars_before = sum(len(json.dumps(m, ensure_ascii=False)) for m in msgs)
    messages_before = len(msgs)

    # Collect UUIDs before pruning for re-linking
    all_uuids_before = {m.get("uuid", "") for m in msgs if m.get("uuid")}

    pipeline = compose_prescription(tier)
    results: list[StrategyResult] = []

    for strat in pipeline:
        msgs, result = strat.fn(msgs, cfg_dict)
        results.append(result)

    # Determine removed UUIDs
    surviving_uuids = {m.get("uuid", "") for m in msgs if m.get("uuid")}
    removed_uuids = all_uuids_before - surviving_uuids

    # Re-link parentUuids
    _relink_parent_uuids(msgs, removed_uuids)

    chars_after = sum(len(json.dumps(m, ensure_ascii=False)) for m in msgs)

    report = PruneReport(
        tier=tier,
        strategy_results=results,
        messages_before=messages_before,
        messages_after=len(msgs),
        chars_before=chars_before,
        chars_after=chars_after,
    )

    return msgs, report


# ---------------------------------------------------------------------------
# File-level prune (with concurrent-write safety)
# ---------------------------------------------------------------------------


def prune_session_file(
    path: Path,
    tier: str = "standard",
    config: PruneConfig | None = None,
    dry_run: bool = True,
    output_path: Path | None = None,
) -> PruneReport:
    """Load a JSONL session file, prune it, and optionally write back.

    Args:
        path: Path to the session JSONL file.
        tier: Pruning aggressiveness.
        config: Optional config overrides.
        dry_run: If True (default), only report savings without writing.
        output_path: If given, write pruned output here instead of in-place.

    Returns:
        PruneReport with per-strategy breakdown.

    Raises:
        BlockingIOError: Another process is pruning the same file.
        RuntimeError: File changed in a conflicting way during prune.
    """
    path = Path(path)

    # Load messages from JSONL
    messages: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    pruned, report = prune(messages, tier=tier, config=config)

    if dry_run:
        return report

    # Write output
    out = "\n".join(json.dumps(m, ensure_ascii=False) for m in pruned) + "\n"

    if output_path:
        atomic_write(output_path, out)
    else:
        # In-place write with concurrent-write safety
        snapshot = FileSnapshot.take(path)

        with advisory_lock(path):
            state = snapshot.classify()

            if state == "unchanged":
                atomic_write(path, out)
            elif state == "appended":
                # Append the delta to our pruned output
                delta = snapshot.read_delta()
                atomic_write(path, out.encode("utf-8") + delta)
            else:
                raise RuntimeError(
                    f"File {path} changed in a conflicting way during pruning. "
                    "Re-run to retry with fresh data."
                )

    return report
