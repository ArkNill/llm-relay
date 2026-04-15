"""Aggressive pruning strategies -- higher savings, higher risk.

These remove content that *might* still be useful but is unlikely to
affect the current reasoning path: HTTP spam runs, error-retry loops,
mega blocks, and base64 images.
"""

from __future__ import annotations

import hashlib
import json
import re

from llm_relay.strategies import Message, PruneAction, StrategyResult, strategy


def _byte_size(msg: Message) -> int:
    return len(json.dumps(msg, ensure_ascii=False))


def _is_protected(msg: Message) -> bool:
    return msg.get("type") in ("summary", "isCompactSummary") or msg.get("isCompactSummary") is True


def _get_content_blocks(msg: Message) -> list[dict] | None:
    inner = msg.get("message", {})
    content = inner.get("content") if isinstance(inner, dict) else None
    return content if isinstance(content, list) else None


# ---------------------------------------------------------------------------
# 9. http-spam
# ---------------------------------------------------------------------------

_HTTP_TOOLS = frozenset({"WebFetch", "WebSearch"})


@strategy(
    name="http-spam",
    description="Collapse runs of consecutive WebFetch/WebSearch tool calls",
    tier="aggressive",
    estimated_savings="0-5%",
)
def http_spam(
    messages: list[Message], config: dict
) -> tuple[list[Message], StrategyResult]:
    """When multiple WebFetch/WebSearch calls appear consecutively,
    keep the first and last, remove the middle."""

    # Build tool-use index: (msg_idx, tool_name)
    tool_runs: list[tuple[int, str]] = []
    for i, msg in enumerate(messages):
        blocks = _get_content_blocks(msg)
        if blocks is None:
            continue
        for block in blocks:
            if block.get("type") == "tool_use" and block.get("name") in _HTTP_TOOLS:
                tool_runs.append((i, block["name"]))
                break

    # Find consecutive runs of HTTP tool messages
    remove_indices: set[int] = set()
    run_start = 0
    while run_start < len(tool_runs):
        run_end = run_start
        while (
            run_end + 1 < len(tool_runs)
            and tool_runs[run_end + 1][0] - tool_runs[run_end][0] <= 2  # allow 1 gap (result msg)
        ):
            run_end += 1

        run_len = run_end - run_start + 1
        if run_len >= 3:
            # Remove middle messages (keep first and last)
            for j in range(run_start + 1, run_end):
                remove_indices.add(tool_runs[j][0])
                # Also remove the next message if it's a tool result
                next_idx = tool_runs[j][0] + 1
                if next_idx < len(messages):
                    remove_indices.add(next_idx)

        run_start = run_end + 1

    if not remove_indices:
        return messages, StrategyResult(strategy_name="http-spam")

    kept: list[Message] = []
    actions: list[PruneAction] = []
    removed_chars = 0

    for i, msg in enumerate(messages):
        if i in remove_indices and not _is_protected(msg):
            sz = _byte_size(msg)
            actions.append(
                PruneAction(
                    line_index=i,
                    action="remove",
                    reason="intermediate HTTP tool call in run",
                    original_bytes=sz,
                    pruned_bytes=0,
                )
            )
            removed_chars += sz
        else:
            kept.append(msg)

    return kept, StrategyResult(
        strategy_name="http-spam",
        actions=actions,
        messages_removed=len(actions),
        chars_removed=removed_chars,
    )


# ---------------------------------------------------------------------------
# 10. error-retry-collapse
# ---------------------------------------------------------------------------


@strategy(
    name="error-retry-collapse",
    description="Collapse error→retry sequences with identical tool input into a single attempt",
    tier="aggressive",
    estimated_savings="0-5%",
)
def error_retry_collapse(
    messages: list[Message], config: dict
) -> tuple[list[Message], StrategyResult]:
    """Detect tool_use → error_result → retry_with_same_input sequences and
    remove intermediate failed attempts."""

    # Build index of tool_use blocks by input hash
    tool_uses: list[tuple[int, str, str]] = []  # (msg_idx, tool_name, input_hash)

    for i, msg in enumerate(messages):
        blocks = _get_content_blocks(msg)
        if blocks is None:
            continue
        for block in blocks:
            if block.get("type") == "tool_use":
                inp = json.dumps(block.get("input", {}), sort_keys=True)
                h = hashlib.md5(inp.encode(), usedforsecurity=False).hexdigest()
                tool_uses.append((i, block.get("name", ""), h))

    # Find consecutive identical tool invocations
    remove_indices: set[int] = set()
    seen: dict[str, list[int]] = {}  # hash → [msg indices]

    for idx, (msg_i, name, h) in enumerate(tool_uses):
        key = f"{name}:{h}"
        if key not in seen:
            seen[key] = [idx]
            continue

        prev_idx = seen[key][-1]
        prev_msg_i = tool_uses[prev_idx][0]

        # Check if the result between them was an error
        for mid in range(prev_msg_i + 1, msg_i):
            blocks = _get_content_blocks(messages[mid])
            if blocks is None:
                continue
            for block in blocks:
                if block.get("type") == "tool_result" and block.get("is_error"):
                    # This is an error-retry: remove the earlier attempt + error
                    remove_indices.add(prev_msg_i)
                    remove_indices.add(mid)
                    break

        seen[key].append(idx)

    if not remove_indices:
        return messages, StrategyResult(strategy_name="error-retry-collapse")

    kept: list[Message] = []
    actions: list[PruneAction] = []
    removed_chars = 0

    for i, msg in enumerate(messages):
        if i in remove_indices and not _is_protected(msg):
            sz = _byte_size(msg)
            actions.append(
                PruneAction(
                    line_index=i,
                    action="remove",
                    reason="error-retry intermediate attempt",
                    original_bytes=sz,
                    pruned_bytes=0,
                )
            )
            removed_chars += sz
        else:
            kept.append(msg)

    return kept, StrategyResult(
        strategy_name="error-retry-collapse",
        actions=actions,
        messages_removed=len(actions),
        chars_removed=removed_chars,
    )


# ---------------------------------------------------------------------------
# 11. mega-block-trim
# ---------------------------------------------------------------------------

_DEFAULT_MEGA_THRESHOLD = 20_000


@strategy(
    name="mega-block-trim",
    description="Truncate any single content block exceeding threshold to head+tail",
    tier="aggressive",
    estimated_savings="0-10%",
)
def mega_block_trim(
    messages: list[Message], config: dict
) -> tuple[list[Message], StrategyResult]:
    threshold = config.get("mega_block_chars", _DEFAULT_MEGA_THRESHOLD)
    keep_chars = threshold // 4  # keep 25% from each end

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

        for block in blocks:
            for key in ("text", "content", "thinking"):
                val = block.get(key, "")
                if isinstance(val, str) and len(val) > threshold:
                    head = val[:keep_chars]
                    tail = val[-keep_chars:]
                    trimmed = len(val) - keep_chars * 2
                    block[key] = f"{head}\n\n...[{trimmed} chars trimmed]...\n\n{tail}"
                    changed = True

        if changed:
            after = _byte_size(msg)
            diff = before - after
            if diff > 0:
                actions.append(
                    PruneAction(
                        line_index=i,
                        action="replace",
                        reason=f"mega block trimmed (>{threshold} chars)",
                        original_bytes=before,
                        pruned_bytes=after,
                    )
                )
                removed_chars += diff

    return messages, StrategyResult(
        strategy_name="mega-block-trim",
        actions=actions,
        messages_replaced=len(actions),
        chars_removed=removed_chars,
    )


# ---------------------------------------------------------------------------
# 12. image-strip
# ---------------------------------------------------------------------------

_B64_PATTERN = re.compile(r"^[A-Za-z0-9+/]{100,}={0,2}$")


@strategy(
    name="image-strip",
    description="Remove base64-encoded image blocks from tool results and content",
    tier="aggressive",
    estimated_savings="0-30%",
)
def image_strip(
    messages: list[Message], config: dict
) -> tuple[list[Message], StrategyResult]:
    actions: list[PruneAction] = []
    removed_chars = 0

    for i, msg in enumerate(messages):
        if _is_protected(msg):
            continue

        blocks = _get_content_blocks(msg)
        if blocks is None:
            continue

        before = _byte_size(msg)
        new_blocks = []
        changed = False

        for block in blocks:
            btype = block.get("type", "")

            # Direct image block
            if btype == "image":
                changed = True
                new_blocks.append({"type": "text", "text": "[image removed by pruner]"})
                continue

            # base64 data in source field
            source = block.get("source", {})
            if isinstance(source, dict) and source.get("type") == "base64":
                changed = True
                media = source.get("media_type", "image/*")
                new_blocks.append({"type": "text", "text": f"[image removed: {media}]"})
                continue

            new_blocks.append(block)

        if changed:
            msg.get("message", {})["content"] = new_blocks
            after = _byte_size(msg)
            diff = before - after
            if diff > 0:
                actions.append(
                    PruneAction(
                        line_index=i,
                        action="replace",
                        reason="image block removed",
                        original_bytes=before,
                        pruned_bytes=after,
                    )
                )
                removed_chars += diff

    return messages, StrategyResult(
        strategy_name="image-strip",
        actions=actions,
        messages_replaced=len(actions),
        chars_removed=removed_chars,
    )
