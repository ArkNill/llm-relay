"""CC effort fix -- clamp output_config.effort when thinking is disabled.

Claude Code 2.1.220 issues web-tool subrequests (WebSearch, WebFetch) with
thinking turned off, while still forwarding the session effort level. The
Anthropic API rejects effort above 'high' for such requests:

    400 output_config.effort 'xhigh' is not supported when thinking is
    disabled on this model. Use effort 'high' or below, or enable thinking.

The result is that every web tool call fails once the session runs at
effort='xhigh' or 'max', even though normal (thinking-enabled) turns are fine.

This module clamps effort down to 'high' for exactly those requests, so a
session can keep effort='max' for its regular turns. Requests that enable
thinking, or that already sit at 'high' or below, are left untouched.

Toggle: LLM_RELAY_EFFORT_FIX=1 (default ON)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("llm-relay")

# Effort levels the API refuses when thinking is disabled.
_BLOCKED_EFFORTS = ("xhigh", "max")

# Highest effort accepted with thinking disabled.
_CLAMP_TARGET = os.getenv("LLM_RELAY_EFFORT_FIX_TARGET", "high")

# The 'llm-relay' logger runs at WARNING, so info-level clamp events are
# invisible. Set =1 to surface them for diagnosis.
_VERBOSE = os.getenv("LLM_RELAY_EFFORT_FIX_VERBOSE", "0") == "1"


def _thinking_disabled(req_json: Dict[str, Any]) -> bool:
    """True only when the request explicitly disables thinking.

    An omitted 'thinking' key means adaptive thinking (the API default), which
    accepts xhigh/max. Only {'type': 'disabled'} triggers the effort cap, so a
    missing key must NOT be clamped -- doing so would silently downgrade every
    normal turn.
    """
    thinking = req_json.get("thinking")
    if not isinstance(thinking, dict):
        return False
    return thinking.get("type") == "disabled"


def normalize_request(req_json: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Clamp effort in-place when thinking is disabled.

    Returns (modified, original_effort). original_effort is None when the
    request was left untouched.
    """
    output_config = req_json.get("output_config")
    if not isinstance(output_config, dict):
        return False, None

    effort = output_config.get("effort")
    if effort not in _BLOCKED_EFFORTS:
        return False, None

    if not _thinking_disabled(req_json):
        return False, None

    output_config["effort"] = _CLAMP_TARGET
    logger.log(
        logging.WARNING if _VERBOSE else logging.INFO,
        "effort_fix: clamped %s -> %s (thinking disabled)",
        effort,
        _CLAMP_TARGET,
    )
    return True, effort
