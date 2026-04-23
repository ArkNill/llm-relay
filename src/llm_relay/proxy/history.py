"""Session history capture -- records full conversation turns for replay.

Activated via LLM_RELAY_HISTORY=1.  Separated from proxy.py to keep the
hot-path module lean; all heavy lifting (diff computation, compaction
detection, thinking extraction) lives here.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from .db import log_compaction_event, log_conversation_turn

logger = logging.getLogger("llm-relay.history")

# Per-session previous-turn message count cache (process lifetime).
# Key: session_id, Value: (turn_number, message_count, input_tokens)
_session_prev: Dict[str, Tuple[int, int, int]] = {}


# ── Delta computation ──


def _compute_delta(
    messages: List[dict],
    previous_count: int,
) -> Tuple[List[dict], str]:
    """Compute the delta (new messages) since the previous turn.

    Returns (messages_to_store, storage_mode).

    Normal flow: each turn adds +2 messages (assistant response + new user msg).
    If current count < previous count, compaction happened -- store full snapshot.
    First turn (previous_count == 0): always store full.
    """
    current_count = len(messages)

    if previous_count == 0:
        # First turn for this session -- store everything
        return messages, "full"

    if current_count < previous_count:
        # Compaction detected -- store full snapshot
        return messages, "full"

    # Normal delta: only new messages
    delta = messages[previous_count:]
    if not delta:
        # No new messages (shouldn't happen, but be safe)
        return messages, "full"

    return delta, "delta"


# ── Thinking block extraction ──


def _extract_thinking(content_blocks: Any) -> List[dict]:
    """Extract thinking/reasoning blocks from response content.

    Handles both list-of-blocks format and direct content.
    Returns a list of thinking block dicts (type, thinking text).
    """
    if not isinstance(content_blocks, list):
        return []

    thinking = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type", "")
        if block_type == "thinking":
            thinking.append({
                "type": "thinking",
                "thinking": block.get("thinking", ""),
            })
    return thinking


# ── Compaction detection ──


def _detect_compaction(
    session_id: str,
    current_count: int,
    previous_count: int,
    current_tokens: int,
    previous_tokens: int,
) -> Optional[dict]:
    """Detect compaction by message count drop or token drop.

    Returns compaction info dict if detected, None otherwise.

    Heuristics:
    - Message count decreased (any drop = compaction, since normal turns add messages)
    - Input tokens dropped by >30% (aggressive summarization/pruning)
    """
    if previous_count == 0:
        return None

    # Message count drop
    count_dropped = current_count < previous_count

    # Token drop (only if we have previous token data)
    token_dropped = False
    token_drop_pct = 0.0
    if previous_tokens > 0 and current_tokens > 0:
        token_drop_pct = (previous_tokens - current_tokens) / previous_tokens * 100
        token_dropped = token_drop_pct > 30.0

    if not count_dropped and not token_dropped:
        return None

    dropped_count = max(0, previous_count - current_count)

    return {
        "previous_count": previous_count,
        "current_count": current_count,
        "dropped_count": dropped_count,
        "previous_tokens": previous_tokens,
        "current_tokens": current_tokens,
        "token_drop_pct": round(token_drop_pct, 1),
    }


# ── Response extraction helpers ──


def _extract_response_content(resp_json: dict) -> Tuple[Optional[str], Optional[str]]:
    """Extract response message and thinking blocks from a non-streaming response.

    Returns (response_message_json, thinking_blocks_json).
    """
    content = resp_json.get("content")
    if content is None:
        return None, None

    response_msg = json.dumps(content, ensure_ascii=False)
    thinking = _extract_thinking(content)
    thinking_json = json.dumps(thinking, ensure_ascii=False) if thinking else None

    return response_msg, thinking_json


def _extract_model_params(req_json: dict) -> Tuple[Optional[str], Optional[float], Optional[int]]:
    """Extract model, temperature, max_tokens from request JSON."""
    model = req_json.get("model")
    temperature = req_json.get("temperature")
    max_tokens = req_json.get("max_tokens")
    return model, temperature, max_tokens


# ── Main capture functions ──


def capture_turn(
    conn: sqlite3.Connection,
    session_id: Optional[str],
    req_json: Optional[dict],
    resp_json: dict,
    input_tokens: int = 0,
    request_size: int = 0,
    response_size: int = 0,
    raw_mode: bool = False,
) -> None:
    """Capture a conversation turn from a non-streaming response.

    Called from proxy._proxy() after log_request().
    """
    if not session_id or not req_json:
        return

    messages = req_json.get("messages", [])
    if not messages:
        return

    try:
        _do_capture(
            conn=conn,
            session_id=session_id,
            messages=messages,
            req_json=req_json,
            response_content=resp_json.get("content"),
            input_tokens=input_tokens,
            request_size=request_size,
            response_size=response_size,
            raw_mode=raw_mode,
            provider="anthropic",
        )
    except Exception:
        logger.debug("history capture failed (non-stream)", exc_info=True)


def capture_turn_streamed(
    conn: sqlite3.Connection,
    session_id: Optional[str],
    req_json: Optional[dict],
    accumulated_content: List[dict],
    model: Optional[str] = None,
    input_tokens: int = 0,
    request_size: int = 0,
    response_size: int = 0,
    raw_mode: bool = False,
) -> None:
    """Capture a conversation turn from a streaming response.

    Called from proxy._proxy_stream() finally block after log_request().
    accumulated_content is the list of content blocks built from SSE events.
    """
    if not session_id or not req_json:
        return

    messages = req_json.get("messages", [])
    if not messages:
        return

    try:
        _do_capture(
            conn=conn,
            session_id=session_id,
            messages=messages,
            req_json=req_json,
            response_content=accumulated_content,
            input_tokens=input_tokens,
            request_size=request_size,
            response_size=response_size,
            raw_mode=raw_mode,
            provider="anthropic",
            model_override=model,
        )
    except Exception:
        logger.debug("history capture failed (stream)", exc_info=True)


def capture_delegation_turn(
    conn: sqlite3.Connection,
    session_id: str,
    cli_id: str,
    prompt: str,
    output: str,
    model: Optional[str] = None,
    duration_ms: float = 0.0,
) -> None:
    """Capture a CLI delegation (Codex/Gemini) as a conversation turn.

    These don't flow through the proxy, so we record them at the MCP layer.
    """
    try:
        # Delegation turns don't have Anthropic message structure;
        # store as simple user/assistant exchange.
        request_msg = json.dumps(
            [{"role": "user", "content": prompt}],
            ensure_ascii=False,
        )
        response_msg = json.dumps(
            [{"type": "text", "text": output}],
            ensure_ascii=False,
        )

        # Use a delegation-scoped turn counter
        prev = _session_prev.get(session_id)
        turn_number = (prev[0] + 1) if prev else 1

        log_conversation_turn(
            conn,
            session_id=session_id,
            turn_number=turn_number,
            storage_mode="full",
            request_messages=request_msg,
            response_message=response_msg,
            model=model,
            total_message_count=1,
            previous_message_count=prev[1] if prev else 0,
            request_size_bytes=len(prompt.encode("utf-8")),
            response_size_bytes=len(output.encode("utf-8")),
            provider=cli_id,
        )

        _session_prev[session_id] = (turn_number, 1, 0)
    except Exception:
        logger.debug("history capture failed (delegation)", exc_info=True)


# ── Internal implementation ──


def _do_capture(
    conn: sqlite3.Connection,
    session_id: str,
    messages: List[dict],
    req_json: dict,
    response_content: Any,
    input_tokens: int,
    request_size: int,
    response_size: int,
    raw_mode: bool,
    provider: str,
    model_override: Optional[str] = None,
) -> None:
    """Core capture logic shared by streaming and non-streaming paths."""
    current_count = len(messages)
    prev = _session_prev.get(session_id)
    previous_count = prev[1] if prev else 0
    previous_tokens = prev[2] if prev else 0
    turn_number = (prev[0] + 1) if prev else 1

    # Compute delta
    if raw_mode:
        msgs_to_store = messages
        storage_mode = "full"
    else:
        msgs_to_store, storage_mode = _compute_delta(messages, previous_count)

    # Detect compaction
    compaction = _detect_compaction(
        session_id, current_count, previous_count, input_tokens, previous_tokens,
    )
    if compaction:
        # On compaction, always store full snapshot
        msgs_to_store = messages
        storage_mode = "full"
        log_compaction_event(
            conn,
            session_id=session_id,
            turn_number=turn_number,
            previous_count=compaction["previous_count"],
            current_count=compaction["current_count"],
            dropped_count=compaction["dropped_count"],
            previous_tokens=compaction["previous_tokens"],
            current_tokens=compaction["current_tokens"],
            token_drop_pct=compaction["token_drop_pct"],
        )
        logger.info(
            "COMPACTION DETECTED: session=%s turn=%d dropped=%d tokens=%.1f%%",
            session_id, turn_number,
            compaction["dropped_count"], compaction["token_drop_pct"],
        )

    # Extract response and thinking
    response_msg_json = None
    thinking_json = None
    if response_content is not None:
        response_msg_json = json.dumps(response_content, ensure_ascii=False)
        thinking = _extract_thinking(response_content)
        if thinking:
            thinking_json = json.dumps(thinking, ensure_ascii=False)

    # Extract model params
    model, temperature, max_tokens = _extract_model_params(req_json)
    if model_override:
        model = model_override

    # Serialize request messages
    request_msg_json = json.dumps(msgs_to_store, ensure_ascii=False)

    log_conversation_turn(
        conn,
        session_id=session_id,
        turn_number=turn_number,
        storage_mode=storage_mode,
        request_messages=request_msg_json,
        response_message=response_msg_json,
        thinking_blocks=thinking_json,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        total_message_count=current_count,
        previous_message_count=previous_count,
        request_size_bytes=request_size,
        response_size_bytes=response_size,
        provider=provider,
    )

    # Update session state
    _session_prev[session_id] = (turn_number, current_count, input_tokens)
