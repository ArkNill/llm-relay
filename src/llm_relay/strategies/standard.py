"""Standard pruning strategies -- moderate trade-offs.

These trim content that is unlikely to affect ongoing reasoning:
thinking blocks (already consumed), oversized tool output, stale
tool results, and duplicate system reminders.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from llm_relay.strategies import Message, PruneAction, StrategyResult, strategy


def _byte_size(msg: Message) -> int:
    return len(json.dumps(msg, ensure_ascii=False))


def _is_protected(msg: Message) -> bool:
    return msg.get("type") in ("summary", "isCompactSummary") or msg.get("isCompactSummary") is True


def _get_content_blocks(msg: Message) -> list[dict] | None:
    """Extract content blocks from message.message.content (list form)."""
    inner = msg.get("message", {})
    content = inner.get("content") if isinstance(inner, dict) else None
    if isinstance(content, list):
        return content
    return None


# ---------------------------------------------------------------------------
# 5. thinking-blocks
# ---------------------------------------------------------------------------


@strategy(
    name="thinking-blocks",
    description="Remove or truncate thinking/signature blocks from assistant messages",
    tier="standard",
    estimated_savings="10-50%",
)
def thinking_blocks(
    messages: list[Message], config: dict
) -> tuple[list[Message], StrategyResult]:
    """Strip thinking blocks from assistant messages.

    Config options:
        thinking_mode: "remove" (default) | "truncate"
        thinking_max_chars: 200 (only for truncate mode)
    """
    mode = config.get("thinking_mode", "remove")
    max_chars = config.get("thinking_max_chars", 200)

    actions: list[PruneAction] = []
    removed_chars = 0

    for i, msg in enumerate(messages):
        if _is_protected(msg):
            continue

        blocks = _get_content_blocks(msg)
        if blocks is None:
            continue

        inner = msg.get("message", {})
        if inner.get("role") != "assistant":
            continue

        before = _byte_size(msg)
        new_blocks = []
        changed = False

        for block in blocks:
            btype = block.get("type", "")
            if btype in ("thinking", "redacted_thinking"):
                if mode == "remove":
                    changed = True
                    continue
                elif mode == "truncate":
                    text = block.get("thinking", "") or block.get("text", "")
                    if len(text) > max_chars:
                        block = {**block, "thinking": text[:max_chars] + "...[truncated]"}
                        changed = True
            # Also strip signature fields
            if "signature" in block:
                block = {k: v for k, v in block.items() if k != "signature"}
                changed = True
            new_blocks.append(block)

        if changed:
            inner["content"] = new_blocks
            after = _byte_size(msg)
            diff = before - after
            if diff > 0:
                actions.append(
                    PruneAction(
                        line_index=i,
                        action="replace",
                        reason="thinking/signature blocks removed",
                        original_bytes=before,
                        pruned_bytes=after,
                    )
                )
                removed_chars += diff

    return messages, StrategyResult(
        strategy_name="thinking-blocks",
        actions=actions,
        messages_replaced=len(actions),
        chars_removed=removed_chars,
    )


# ---------------------------------------------------------------------------
# 6. tool-output-trim
# ---------------------------------------------------------------------------

_DEFAULT_TOOL_MAX = 5000  # chars
_KEEP_LINES = 15


@strategy(
    name="tool-output-trim",
    description="Trim oversized tool result content to head + tail summary",
    tier="standard",
    estimated_savings="1-8%",
)
def tool_output_trim(
    messages: list[Message], config: dict
) -> tuple[list[Message], StrategyResult]:
    max_chars = config.get("tool_output_max_chars", _DEFAULT_TOOL_MAX)

    actions: list[PruneAction] = []
    removed_chars = 0

    for i, msg in enumerate(messages):
        if _is_protected(msg):
            continue

        blocks = _get_content_blocks(msg)
        if blocks is None:
            continue

        before = _byte_size(msg)
        changed = False

        for bi, block in enumerate(blocks):
            if block.get("type") != "tool_result":
                continue

            content = block.get("content", "")
            if isinstance(content, str) and len(content) > max_chars:
                lines = content.split("\n")
                if len(lines) > _KEEP_LINES * 2:
                    head = "\n".join(lines[:_KEEP_LINES])
                    tail = "\n".join(lines[-_KEEP_LINES:])
                    trimmed = len(lines) - _KEEP_LINES * 2
                    block["content"] = f"{head}\n\n...[{trimmed} lines trimmed]...\n\n{tail}"
                    changed = True
                elif len(content) > max_chars:
                    block["content"] = content[:max_chars] + f"\n...[trimmed from {len(content)} chars]"
                    changed = True

        if changed:
            after = _byte_size(msg)
            diff = before - after
            if diff > 0:
                actions.append(
                    PruneAction(
                        line_index=i,
                        action="replace",
                        reason="tool output trimmed",
                        original_bytes=before,
                        pruned_bytes=after,
                    )
                )
                removed_chars += diff

    return messages, StrategyResult(
        strategy_name="tool-output-trim",
        actions=actions,
        messages_replaced=len(actions),
        chars_removed=removed_chars,
    )


# ---------------------------------------------------------------------------
# 7. tool-result-age
# ---------------------------------------------------------------------------

_DEFAULT_MAX_AGE_TURNS = 30


@strategy(
    name="tool-result-age",
    description="Condense tool results older than N turns to a brief stub",
    tier="standard",
    estimated_savings="5-20%",
)
def tool_result_age(
    messages: list[Message], config: dict
) -> tuple[list[Message], StrategyResult]:
    max_age = config.get("max_age_turns", _DEFAULT_MAX_AGE_TURNS)

    # Count user turns from the end
    user_turns = 0
    turn_index: dict[int, int] = {}  # msg index → turns-from-end
    for i in range(len(messages) - 1, -1, -1):
        inner = messages[i].get("message", {})
        if isinstance(inner, dict) and inner.get("role") == "user":
            user_turns += 1
        turn_index[i] = user_turns

    actions: list[PruneAction] = []
    removed_chars = 0

    for i, msg in enumerate(messages):
        if _is_protected(msg) or turn_index.get(i, 0) < max_age:
            continue

        blocks = _get_content_blocks(msg)
        if blocks is None:
            continue

        before = _byte_size(msg)
        changed = False

        for block in blocks:
            if block.get("type") != "tool_result":
                continue
            content = block.get("content", "")
            if isinstance(content, str) and len(content) > 100:
                tool_id = block.get("tool_use_id", "?")[:12]
                block["content"] = f"[old tool result condensed -- tool_use_id={tool_id}]"
                changed = True

        if changed:
            after = _byte_size(msg)
            diff = before - after
            if diff > 0:
                actions.append(
                    PruneAction(
                        line_index=i,
                        action="replace",
                        reason=f"tool result aged out (>{max_age} turns)",
                        original_bytes=before,
                        pruned_bytes=after,
                    )
                )
                removed_chars += diff

    return messages, StrategyResult(
        strategy_name="tool-result-age",
        actions=actions,
        messages_replaced=len(actions),
        chars_removed=removed_chars,
    )


# ---------------------------------------------------------------------------
# 8. system-reminder-dedup
# ---------------------------------------------------------------------------


@strategy(
    name="system-reminder-dedup",
    description="Deduplicate repeated system-reminder content across messages",
    tier="standard",
    estimated_savings="0.5-2%",
)
def system_reminder_dedup(
    messages: list[Message], config: dict
) -> tuple[list[Message], StrategyResult]:
    """If identical system-reminder text appears multiple times, keep only the last."""

    # Index: hash(content) → list of (msg_index, block_index)
    reminder_locs: dict[str, list[tuple[int, int]]] = defaultdict(list)

    for i, msg in enumerate(messages):
        blocks = _get_content_blocks(msg)
        if blocks is None:
            continue
        for bi, block in enumerate(blocks):
            if block.get("type") != "text":
                continue
            text = block.get("text", "")
            if "<system-reminder>" in text and len(text) > 50:
                h = hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()
                reminder_locs[h].append((i, bi))

    # For each duplicate group, mark all but the last for removal
    to_remove_blocks: dict[int, set[int]] = defaultdict(set)  # msg_idx → {block_indices}
    for locs in reminder_locs.values():
        if len(locs) <= 1:
            continue
        for msg_i, block_i in locs[:-1]:
            to_remove_blocks[msg_i].add(block_i)

    if not to_remove_blocks:
        return messages, StrategyResult(strategy_name="system-reminder-dedup")

    actions: list[PruneAction] = []
    removed_chars = 0

    for i, msg in enumerate(messages):
        if i not in to_remove_blocks:
            continue

        blocks = _get_content_blocks(msg)
        if blocks is None:
            continue

        before = _byte_size(msg)
        remove_set = to_remove_blocks[i]
        new_blocks = [b for bi, b in enumerate(blocks) if bi not in remove_set]
        msg.get("message", {})["content"] = new_blocks

        after = _byte_size(msg)
        diff = before - after
        if diff > 0:
            actions.append(
                PruneAction(
                    line_index=i,
                    action="replace",
                    reason="duplicate system-reminder removed",
                    original_bytes=before,
                    pruned_bytes=after,
                )
            )
            removed_chars += diff

    return messages, StrategyResult(
        strategy_name="system-reminder-dedup",
        actions=actions,
        messages_replaced=len(actions),
        chars_removed=removed_chars,
    )
