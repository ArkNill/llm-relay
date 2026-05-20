"""Verify primitives -- idempotent checks for install / config / integration.

Each primitive returns a `VerifyReport`: a structured pass/fail/warn/skipped
record per check, designed to be consumed either by a human (`--format text`)
or by an agent automating an llm-relay install (`--format json`).

This package complements `env_fingerprint`: env-fingerprint describes the
*state* of the user's environment, verify *asserts expectations* about that
state. An agent typically uses env-fingerprint to plan, then verify to
confirm each step it took.

Shared output schema (schema_version "1"):

  {
    "schema_version": "1",
    "target": "install" | "config" | "integration",
    "captured_at": "...",
    "overall": "pass" | "fail" | "warn",
    "summary": {"pass": N, "fail": N, "warn": N, "skipped": N},
    "checks": [
      {
        "id": "...",
        "label": "...",
        "status": "pass" | "fail" | "warn" | "skipped",
        "detail": "...",
        "remediation": "..." | null,
        "data": {...} | null
      }
    ]
  }

Status semantics:
  pass     -- expectation met
  warn     -- expectation met but with a caveat the operator/agent should know
  fail     -- expectation NOT met; remediation should fix it
  skipped  -- check was not applicable (e.g. CLI not installed for integration)

Overall priority: fail > warn > (pass | skipped).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

SCHEMA_VERSION = "1"

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_WARN = "warn"
STATUS_SKIPPED = "skipped"

_ALL_STATUSES = (STATUS_PASS, STATUS_FAIL, STATUS_WARN, STATUS_SKIPPED)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class VerifyCheck:
    """A single verification result.

    `data` is optional structured detail that an agent may need (e.g. the
    actual port number that was found in use). It's separate from `detail`
    so machine consumers don't have to parse the human-friendly string.
    """
    id: str
    label: str
    status: str
    detail: str
    remediation: Optional[str] = None
    data: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.status not in _ALL_STATUSES:
            raise ValueError(
                "VerifyCheck.status must be one of {} (got {!r})".format(
                    _ALL_STATUSES, self.status,
                )
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
            "remediation": self.remediation,
            "data": self.data,
        }


@dataclass
class VerifyReport:
    """Aggregate verification result for a single target."""
    target: str
    captured_at: str = field(default_factory=_now_iso)
    schema_version: str = SCHEMA_VERSION
    checks: List[VerifyCheck] = field(default_factory=list)

    @property
    def overall(self) -> str:
        """fail > warn > pass. Skipped checks don't influence overall."""
        statuses = {c.status for c in self.checks}
        if STATUS_FAIL in statuses:
            return STATUS_FAIL
        if STATUS_WARN in statuses:
            return STATUS_WARN
        return STATUS_PASS

    @property
    def summary(self) -> Dict[str, int]:
        counts = {status: 0 for status in _ALL_STATUSES}
        for c in self.checks:
            counts[c.status] += 1
        return counts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "target": self.target,
            "captured_at": self.captured_at,
            "overall": self.overall,
            "summary": self.summary,
            "checks": [c.to_dict() for c in self.checks],
        }


def run_check(
    check_id: str,
    label: str,
    fn: Callable[[], VerifyCheck],
    *,
    fallback_remediation: Optional[str] = None,
) -> VerifyCheck:
    """Run a check function, capturing exceptions as fail status.

    A check function returns a fully formed `VerifyCheck`. If the function
    raises, we synthesize a fail result so a single broken probe never
    crashes the whole report -- partial output beats no output.
    """
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 -- intentional broad capture
        return VerifyCheck(
            id=check_id,
            label=label,
            status=STATUS_FAIL,
            detail="{}: {}".format(type(exc).__name__, exc),
            remediation=fallback_remediation,
            data={"_error": True},
        )
    # Sanity: enforce id/label consistency in case the check function returns
    # an unrelated VerifyCheck by mistake. The provided id/label always win.
    result.id = check_id
    result.label = label
    return result


def aggregate(target: str, reports: List[VerifyReport]) -> VerifyReport:
    """Combine multiple sub-reports into one (e.g. for `verify all`).

    The target string identifies the combined report (e.g. "all" or
    "integration" when collapsing per-CLI sub-reports). Check IDs from
    sub-reports are namespaced as `{sub_target}.{original_id}` to avoid
    collisions across sub-reports.
    """
    combined = VerifyReport(target=target)
    for sub in reports:
        for check in sub.checks:
            combined.checks.append(VerifyCheck(
                id="{}.{}".format(sub.target, check.id),
                label=check.label,
                status=check.status,
                detail=check.detail,
                remediation=check.remediation,
                data=check.data,
            ))
    return combined
