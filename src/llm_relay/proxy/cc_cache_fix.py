"""CC cache fix -- normalize request body for stable prompt caching.

Fixes two classes of cache invalidation that server-side configuration cannot address:
  - Type 1: Tool ordering jitter (MCP tools register async → non-deterministic order)
  - Type 3: Missing TTL on ephemeral cache_control blocks

Also captures diagnostics for future Phase 2 fixes (block drift, fingerprint).

Based on: cnighswonger/claude-code-cache-fix v1.7.2 (MIT)
Toggle: LLM_RELAY_CACHE_FIX=1 (default OFF)
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("llm-relay")

# Sub-feature toggles (all default ON when LLM_RELAY_CACHE_FIX=1)
_TTL_ENABLED = os.getenv("LLM_RELAY_CACHE_FIX_TTL", "1") == "1"
_SORT_TOOLS_ENABLED = os.getenv("LLM_RELAY_CACHE_FIX_SORT_TOOLS", "1") == "1"
_CAPTURE_ENABLED = os.getenv("LLM_RELAY_CACHE_FIX_CAPTURE", "1") == "1"

# --------------------------------------------------------------------------
# Block detection (Phase 2 prep -- used in Phase 1 for diagnostics only)
# --------------------------------------------------------------------------

_SR_PREFIX = "<system-reminder>"
_SR_NL = "<system-reminder>\n"


def is_system_reminder(text: str) -> bool:
    return isinstance(text, str) and text.startswith(_SR_PREFIX)


def is_hooks_block(text: str) -> bool:
    return is_system_reminder(text) and "hook success" in text[:200]


def is_skills_block(text: str) -> bool:
    return isinstance(text, str) and text.startswith(
        _SR_NL + "The following skills are available"
    )


def is_deferred_tools_block(text: str) -> bool:
    return isinstance(text, str) and text.startswith(
        _SR_NL + "The following deferred tools are now available"
    )


def is_mcp_block(text: str) -> bool:
    return isinstance(text, str) and text.startswith(
        _SR_NL + "# MCP Server Instructions"
    )


def classify_block(text: str) -> Optional[str]:
    """Return block type name or None if not a relocatable block."""
    if not isinstance(text, str) or not text.startswith(_SR_PREFIX):
        return None
    if is_hooks_block(text):
        return "hooks"
    if is_skills_block(text):
        return "skills"
    if is_deferred_tools_block(text):
        return "deferred"
    if is_mcp_block(text):
        return "mcp"
    return None


# --------------------------------------------------------------------------
# A. TTL injection
# --------------------------------------------------------------------------

def _inject_ttl_block(block: Dict[str, Any]) -> bool:
    """Inject ttl:'1h' into an ephemeral cache_control block missing ttl.

    Returns True if modified.
    """
    cc = block.get("cache_control")
    if not isinstance(cc, dict):
        return False
    if cc.get("type") == "ephemeral" and "ttl" not in cc:
        cc["ttl"] = "1h"
        return True
    return False


def inject_ttl(req_json: Dict[str, Any]) -> int:
    """Inject ttl:'1h' into all ephemeral cache_control blocks missing ttl.

    Scans system[] and messages[].content[].
    Returns count of injections.
    """
    count = 0

    # system[] blocks
    for block in req_json.get("system", []):
        if isinstance(block, dict) and _inject_ttl_block(block):
            count += 1

    # messages[].content[] blocks
    for msg in req_json.get("messages", []):
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and _inject_ttl_block(block):
                    count += 1

    return count


# --------------------------------------------------------------------------
# B. Tool ordering
# --------------------------------------------------------------------------

def sort_tools(req_json: Dict[str, Any]) -> bool:
    """Sort tools[] array by name for deterministic ordering.

    Returns True if the order changed.
    """
    tools = req_json.get("tools")
    if not isinstance(tools, list) or len(tools) == 0:
        return False

    # Check if already sorted
    names_before = [t.get("name", "") if isinstance(t, dict) else "" for t in tools]
    sorted_tools = sorted(tools, key=lambda t: t.get("name", "") if isinstance(t, dict) else "")
    names_after = [t.get("name", "") if isinstance(t, dict) else "" for t in sorted_tools]

    if names_before == names_after:
        return False

    req_json["tools"] = sorted_tools
    return True


# --------------------------------------------------------------------------
# C. Diagnostic capture
# --------------------------------------------------------------------------

_VERSION_RE = re.compile(r"cc_version=([^;\s]+)")


def _extract_cc_version(
    system: List[Any],
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Extract CC version and fingerprint from system[] billing block.

    CC embeds 'x-anthropic-billing-header: cc_version=X.Y.Z.FFF' in system[0].
    Falls back to HTTP headers if not found in system.
    Returns (base_version, fingerprint) or (None, None).
    """
    attr = ""
    # Primary: scan system[] blocks for billing header
    for block in system:
        text = _block_text(block)
        if "cc_version" in text:
            attr = text
            break
    # Fallback: HTTP headers
    if not attr and headers:
        for key in ("x-anthropic-attribution", "anthropic-attribution", "user-agent"):
            val = headers.get(key, "")
            if "cc_version" in val:
                attr = val
                break
    match = _VERSION_RE.search(attr)
    if not match:
        return None, None

    version_str = match.group(1)
    parts = version_str.split(".")
    if len(parts) >= 4:
        base_version = ".".join(parts[:3])
        fingerprint = parts[3]
        return base_version, fingerprint
    elif len(parts) == 3:
        return version_str, None
    return version_str, None


def _preview(text: str, max_len: int = 100) -> str:
    """Return first max_len chars of text for diagnostic preview."""
    if not isinstance(text, str):
        return ""
    return text[:max_len]


def _block_text(block: Any) -> str:
    """Extract text from a content block (string or dict with 'text' key)."""
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        return block.get("text", "")
    return ""


def capture_diagnostics(
    req_json: Dict[str, Any],
    headers: Dict[str, str],
    tools_reordered: bool,
    ttl_injected: int,
) -> Dict[str, Any]:
    """Capture diagnostic info for cache analysis.

    Returns a dict suitable for log_cache_diagnostic().
    """
    # System blocks preview
    system = req_json.get("system", [])

    cc_version, fingerprint = _extract_cc_version(system, headers)
    system_previews = []  # type: List[str]
    for block in system:
        text = _block_text(block)
        system_previews.append(_preview(text))

    # messages[0] content preview
    messages = req_json.get("messages", [])
    msg0_previews = []  # type: List[str]
    if messages:
        content = messages[0].get("content", [])
        if isinstance(content, list):
            for block in content[:10]:
                text = _block_text(block)
                msg0_previews.append(_preview(text))
        elif isinstance(content, str):
            msg0_previews.append(_preview(content))

    # Scan for drifted blocks (relocatable blocks outside messages[0])
    drifted = {}  # type: Dict[str, List[int]]
    first_user_idx = None
    for i, msg in enumerate(messages):
        if msg.get("role") == "user":
            if first_user_idx is None:
                first_user_idx = i
            # Skip messages[0] (or first user message)
            if i == first_user_idx:
                continue
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    text = _block_text(block)
                    block_type = classify_block(text)
                    if block_type:
                        drifted.setdefault(block_type, []).append(i)
            elif isinstance(content, str):
                block_type = classify_block(content)
                if block_type:
                    drifted.setdefault(block_type, []).append(i)

    # Tool count
    tools = req_json.get("tools", [])
    tools_count = len(tools) if isinstance(tools, list) else 0

    return {
        "cc_version": cc_version,
        "fingerprint": fingerprint,
        "system_block_count": len(system) if isinstance(system, list) else 0,
        "system_preview": json.dumps(system_previews, ensure_ascii=False) if system_previews else None,
        "msg0_block_count": len(msg0_previews),
        "msg0_preview": json.dumps(msg0_previews, ensure_ascii=False) if msg0_previews else None,
        "drifted_blocks": json.dumps(drifted, ensure_ascii=False) if drifted else None,
        "tools_count": tools_count,
        "tools_reordered": int(tools_reordered),
        "ttl_injected": ttl_injected,
    }


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------

def normalize_request(
    req_json: Dict[str, Any],
    headers: Dict[str, str],
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Normalize a CC API request for stable prompt caching.

    Modifies req_json in-place (TTL injection, tool reordering).
    Returns (modified, diagnostics_dict_or_None).

    Safe: catches all exceptions and returns (False, None) on failure.
    """
    try:
        modified = False
        ttl_count = 0
        tools_reordered = False

        # A. TTL injection
        if _TTL_ENABLED:
            ttl_count = inject_ttl(req_json)
            if ttl_count > 0:
                modified = True

        # B. Tool ordering
        if _SORT_TOOLS_ENABLED:
            tools_reordered = sort_tools(req_json)
            if tools_reordered:
                modified = True

        # C. Diagnostic capture
        diag = None  # type: Optional[Dict[str, Any]]
        if _CAPTURE_ENABLED:
            diag = capture_diagnostics(req_json, headers, tools_reordered, ttl_count)

        if modified:
            logger.info(
                "CACHE FIX: ttl_injected=%d, tools_reordered=%s",
                ttl_count,
                tools_reordered,
            )

        return modified, diag

    except Exception:
        logger.debug("cc_cache_fix.normalize_request failed, pass-through", exc_info=True)
        return False, None
