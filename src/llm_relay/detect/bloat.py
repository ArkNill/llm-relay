"""Detector: Session Bloat -- PRELIM/FINAL duplication ratio."""

from __future__ import annotations

from llm_relay.detect.base import BaseDetector
from llm_relay.detect.models import Finding, ParsedSession, Severity


class BloatDetector(BaseDetector):
    detector_id = "bloat"
    display_name = "Log Inflation"

    def check(self, session: ParsedSession) -> list[Finding]:
        groups = session.group_by_request_id()
        if not groups:
            return []

        total_entries = 0
        total_final = 0

        for entries in groups.values():
            total_entries += len(entries)
            finals = sum(1 for e in entries if e.is_final)
            total_final += max(finals, 1)

        if total_final == 0:
            return []

        inflation_ratio = total_entries / total_final

        if inflation_ratio <= 1.5:
            return []

        severity = Severity.WARN if inflation_ratio > 3.0 else Severity.INFO

        return [
            Finding(
                detector_id=self.detector_id,
                severity=severity,
                title=self.display_name,
                detail=(
                    f"{inflation_ratio:.1f}x PRELIM/FINAL duplication ratio "
                    f"({total_entries} entries, {total_final} final). "
                    f"Token counts in local stats may appear inflated."
                ),
                recommendation=(
                    "PRELIM entries are streaming fragments logged before the final response. "
                    "This inflates local stats display but does NOT affect actual billing."
                ),
            )
        ]
