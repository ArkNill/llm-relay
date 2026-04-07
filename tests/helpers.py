"""Shared test helpers for llm-relay."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def make_entry(
    entry_type: str = "assistant",
    uuid: str = "test-uuid",
    parent_uuid: str = "parent-uuid",
    timestamp: str = "2026-04-03T10:00:00Z",
    session_id: str = "test-session",
    model: str = "claude-opus-4-6",
    stop_reason: str = "end_turn",
    input_tokens: int = 1000,
    output_tokens: int = 500,
    cache_creation: int = 100,
    cache_read: int = 900,
    request_id: str = "req_001",
    version: str = "2.1.91",
    subtype: str = "",
    content: Any | None = None,
    is_compact_summary: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    """Build a raw JSONL entry dict for testing."""
    entry: dict[str, Any] = {
        "type": entry_type,
        "uuid": uuid,
        "parentUuid": parent_uuid,
        "timestamp": timestamp,
        "sessionId": session_id,
        "version": version,
    }

    if request_id:
        entry["requestId"] = request_id

    if subtype:
        entry["subtype"] = subtype

    if is_compact_summary:
        entry["isCompactSummary"] = True

    if entry_type == "assistant":
        msg_content: Any = content
        if msg_content is None:
            msg_content = [{"type": "text", "text": "Hello"}]

        entry["message"] = {
            "model": model,
            "type": "message",
            "role": "assistant",
            "content": msg_content,
            "stop_reason": stop_reason,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
            },
        }
    elif entry_type == "user":
        msg_content = content
        if msg_content is None:
            msg_content = "user prompt"
        entry["message"] = {"role": "user", "content": msg_content}
    elif entry_type == "system":
        entry["message"] = {"content": content or "system message"}
        entry["level"] = extra.get("level", "info")
        entry["content"] = content or "system message"

    entry.update(extra)
    return entry


def write_session_file(path: Path, entries: list[dict[str, Any]]) -> None:
    """Write a list of entry dicts as JSONL."""
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
