"""Antigravity CLI (agy) quota preflight -- stdlib only.

Before a delegated agy call starts, ``execute_cli`` asks agy for its quota with
``agy -p "/usage" --output-format json`` (no agent turn, no tokens, no stored
conversation; about 6 s) and refuses to start the call when a bucket the model
draws from is below a floor. The caller then learns about an exhausted quota
from the delegation result instead of having to query it separately.

agy updates itself often and the /usage shape may change between versions, so
every function here returns a reason instead of raising: a failed or unreadable
query must never block a delegation (fail-open).

Settings are read from the environment on every call (not frozen at import) so a
long-lived MCP process picks up changes, like the other ``LLM_RELAY_AGY_*``
variables:

- ``LLM_RELAY_AGY_QUOTA_CHECK`` -- ``0`` skips the preflight (default: on)
- ``LLM_RELAY_AGY_QUOTA_BLOCK_PCT`` -- floor in percent; below it the call is
  not started (default 10; 0 = never block)
- ``LLM_RELAY_AGY_QUOTA_WARN_PCT`` -- below it the verdict is ``warn`` (default 30)
- ``LLM_RELAY_AGY_QUOTA_AFTER_MIN_S`` -- re-query after a call that ran at least
  this many seconds (default 60; 0 = always)
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

_DEFAULT_BLOCK_PCT = 10.0
_DEFAULT_WARN_PCT = 30.0
_DEFAULT_AFTER_MIN_S = 60.0
_USAGE_TIMEOUT_S = 45
# agy's own limit for the /usage call; kept below _USAGE_TIMEOUT_S so agy can
# report its own error before the subprocess is killed.
_USAGE_PRINT_TIMEOUT = "30s"
_FRACTIONAL_SECONDS = re.compile(r"\.\d+")


def usage_command(binary_path: str) -> List[str]:
    """The agy command that prints the quota buckets as a JSON envelope."""
    return [binary_path, "-p", "/usage", "--output-format", "json", "--print-timeout", _USAGE_PRINT_TIMEOUT]


def _is_number(value: Any) -> bool:
    # bool is an int subclass; NaN / inf would make every comparison meaningless.
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def parse_usage(stdout: Any) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Parse the /usage envelope into (buckets, None) or (None, reason).

    A bucket is ``{"id", "window", "remaining_pct", "reset_time"}``; buckets without
    an id are skipped and a non-numeric ``remaining_fraction`` gives
    ``remaining_pct`` None.
    """
    if not isinstance(stdout, str) or not stdout.strip():
        return None, "empty usage output"
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None, "usage output is not JSON"
    if not isinstance(data, dict):
        return None, "usage output is not a JSON object"

    status = data.get("status")
    if status != "SUCCESS":
        reason = "usage status {}".format(status)
        error = data.get("error")
        if error:
            reason = "{}: {}".format(reason, error)
        return None, reason

    command = data.get("command")
    payload = command.get("data") if isinstance(command, dict) else None
    groups = payload.get("groups") if isinstance(payload, dict) else None
    if not isinstance(groups, list):
        return None, "usage output has no command.data.groups list"

    buckets: List[Dict[str, Any]] = []
    for group in groups:
        raw_buckets = group.get("buckets") if isinstance(group, dict) else None
        if not isinstance(raw_buckets, list):
            continue
        for raw in raw_buckets:
            if not isinstance(raw, dict):
                continue
            bucket_id = raw.get("id")
            if not isinstance(bucket_id, str) or not bucket_id:
                continue
            fraction = raw.get("remaining_fraction")
            window = raw.get("window")
            reset_time = raw.get("reset_time")
            buckets.append({
                "id": bucket_id,
                "window": window if isinstance(window, str) else None,
                "remaining_pct": round(fraction * 100, 1) if _is_number(fraction) else None,
                "reset_time": reset_time if isinstance(reset_time, str) else None,
            })
    if not buckets:
        return None, "usage output has no quota buckets"
    return buckets, None


def fetch_usage(
    binary_path: str,
    *,
    cwd: Optional[str] = None,
    timeout: int = _USAGE_TIMEOUT_S,
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Run the /usage query and parse it. Never raises."""
    try:
        proc = subprocess.run(
            usage_command(binary_path),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            # A quota query must not trigger a self-update mid-delegation.
            env={**os.environ, "AGY_CLI_DISABLE_AUTO_UPDATE": "true"},
        )
    except subprocess.TimeoutExpired:
        return None, "usage query timed out after {}s".format(timeout)
    except OSError as e:
        return None, str(e)
    except Exception as e:  # fail-open: an unexpected error must not stop the delegation
        return None, "usage query failed: {}".format(e)

    buckets, reason = parse_usage(proc.stdout)
    if buckets is None and proc.returncode != 0:
        reason = "usage query exited {}: {}".format(proc.returncode, reason)
    return buckets, reason


def quota_group(model: Optional[str]) -> Optional[str]:
    """Quota group a model draws from: ``gemini``, ``3p`` (Claude / GPT) or None (unknown)."""
    name = (model or "").lower()
    if not name or name.startswith("gemini"):
        return "gemini"
    if name.startswith("claude") or name.startswith("gpt"):
        return "3p"
    return None


def relevant_buckets(buckets: List[Dict[str, Any]], group: str, model: Optional[str]) -> List[Dict[str, Any]]:
    """Buckets a call with ``model`` draws from.

    Always the group's ``-5h`` / ``-weekly`` pair; a model-specific bucket such as
    ``gemini-argon-wtu-weekly`` only when one of its middle tokens (``argon``,
    ``wtu``) appears in the model name.
    """
    name = (model or "").lower()
    shared = {"{}-5h".format(group), "{}-weekly".format(group)}
    prefix = "{}-".format(group)
    picked = []
    for bucket in buckets:
        bucket_id = bucket.get("id")
        if not isinstance(bucket_id, str):
            continue
        if bucket_id in shared:
            picked.append(bucket)
            continue
        if bucket_id.startswith(prefix):
            middle = bucket_id.lower().split("-")[1:-1]
            if any(token and token in name for token in middle):
                picked.append(bucket)
    return picked


def _parse_time(value: Any) -> Optional[datetime]:
    """ISO 8601 timestamp (``Z`` allowed) to an aware datetime, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # Python 3.9 rejects fractional seconds that are not 3 or 6 digits; minutes are enough here.
        try:
            parsed = datetime.fromisoformat(_FRACTIONAL_SECONDS.sub("", text, count=1))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _now(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now


def _minutes_until(reset_time: Any, now: datetime) -> Optional[int]:
    parsed = _parse_time(reset_time)
    if parsed is None:
        return None
    seconds = (parsed - now).total_seconds()
    if seconds < 0:
        # Past resets include the 1970 epoch agy reports for unused buckets.
        return None
    return int(seconds // 60)


def summarize(buckets: List[Dict[str, Any]], now: Optional[datetime] = None) -> Dict[str, Dict[str, Any]]:
    """``{id: {"remaining_pct", "reset_time", "resets_in_min"}}`` for the given buckets."""
    current = _now(now)
    return {
        bucket["id"]: {
            "remaining_pct": bucket.get("remaining_pct"),
            "reset_time": bucket.get("reset_time"),
            "resets_in_min": _minutes_until(bucket.get("reset_time"), current),
        }
        for bucket in buckets
        if isinstance(bucket.get("id"), str)
    }


def _fmt_pct(value: float) -> str:
    return "{:.1f}".format(value)


def _fmt_threshold(value: float) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else str(number)


def evaluate(
    buckets: List[Dict[str, Any]],
    model: Optional[str],
    *,
    block_pct: float,
    warn_pct: float,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Quota verdict for a call with ``model``: ``ok`` / ``warn`` / ``blocked`` / ``unknown``.

    Thresholds compare with a strict ``<`` (a 10% floor lets 10.0 through and
    stops 9.9); ``block_pct`` 0 never blocks.
    """
    group = quota_group(model)
    quota: Dict[str, Any] = {
        "verdict": "unknown",
        "group": group,
        "block_pct": float(block_pct),
        "warn_pct": float(warn_pct),
    }
    if group is None:
        quota["note"] = "no quota group known for model '{}'".format(model)
        return quota

    relevant = relevant_buckets(buckets, group, model)
    quota["before"] = summarize(relevant, now)
    known = [b for b in relevant if b.get("remaining_pct") is not None]
    if not known:
        quota["note"] = "no quota bucket for group '{}'".format(group)
        return quota

    # Lowest first so the note and the block message lead with the tightest bucket.
    known.sort(key=lambda b: b["remaining_pct"])
    low = known[0]["remaining_pct"]
    if block_pct > 0 and low < block_pct:
        quota["verdict"] = "blocked"
        quota["note"] = "; ".join(
            "{} {}% < floor {}%".format(b["id"], _fmt_pct(b["remaining_pct"]), _fmt_threshold(block_pct))
            for b in known
            if b["remaining_pct"] < block_pct
        )
    elif low < warn_pct:
        quota["verdict"] = "warn"
        quota["note"] = "{} {}% < warn {}%".format(known[0]["id"], _fmt_pct(low), _fmt_threshold(warn_pct))
    else:
        quota["verdict"] = "ok"
    return quota


def _fmt_duration(minutes: int) -> str:
    if minutes < 60:
        return "{}m".format(minutes)
    return "{}h {}m".format(minutes // 60, minutes % 60)


def block_message(quota: Dict[str, Any]) -> str:
    """Error text for a blocked delegation; always starts with ``quota preflight:``."""
    block_pct = quota.get("block_pct") or 0.0
    before = quota.get("before") or {}
    under = sorted(
        (
            (bucket_id, info)
            for bucket_id, info in before.items()
            if isinstance(info, dict) and _is_number(info.get("remaining_pct")) and info["remaining_pct"] < block_pct
        ),
        key=lambda item: item[1]["remaining_pct"],
    )
    parts = []
    for bucket_id, info in under:
        part = "{} has {}% left (floor {}%)".format(
            bucket_id, _fmt_pct(info["remaining_pct"]), _fmt_threshold(block_pct),
        )
        minutes = info.get("resets_in_min")
        reset_at = _parse_time(info.get("reset_time"))
        if isinstance(minutes, int) and reset_at is not None:
            local = reset_at.astimezone().strftime("%Y-%m-%d %H:%M %Z")
            part += "; resets {}, in {}".format(local, _fmt_duration(minutes))
        parts.append(part)
    if not parts:
        parts.append("quota below the floor of {}%".format(_fmt_threshold(block_pct)))
    return "quota preflight: {}. agy was not started.".format("; ".join(parts))


def _env_float(name: str, default: float, low: float, high: Optional[float]) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value) or value < low or (high is not None and value > high):
        return default
    return value


def thresholds_from_env() -> Tuple[float, float]:
    """(block_pct, warn_pct) from the environment; invalid or out-of-range values give the defaults."""
    return (
        _env_float("LLM_RELAY_AGY_QUOTA_BLOCK_PCT", _DEFAULT_BLOCK_PCT, 0.0, 100.0),
        _env_float("LLM_RELAY_AGY_QUOTA_WARN_PCT", _DEFAULT_WARN_PCT, 0.0, 100.0),
    )


def check_enabled() -> bool:
    """False only when ``LLM_RELAY_AGY_QUOTA_CHECK`` is ``0``."""
    return os.environ.get("LLM_RELAY_AGY_QUOTA_CHECK") != "0"


def after_min_seconds() -> float:
    """Minimum run time (seconds) before the quota is queried again after a call; 0 = always."""
    return _env_float("LLM_RELAY_AGY_QUOTA_AFTER_MIN_S", _DEFAULT_AFTER_MIN_S, 0.0, None)
