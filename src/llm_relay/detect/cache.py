"""Detector: Cache Efficiency + Cold Start Waste."""

from __future__ import annotations

from llm_relay.detect.base import BaseDetector
from llm_relay.detect.models import Finding, ParsedSession, Severity


class CacheDetector(BaseDetector):
    detector_id = "cache"
    display_name = "Cache Efficiency"

    def check(self, session: ParsedSession) -> list[Finding]:
        findings: list[Finding] = []
        groups = session.group_by_request_id()

        if not groups:
            return []

        # Compute per-request cache ratios using FINAL entries only
        ratios: list[float] = []
        ordered_request_ids: list[str] = []

        for entry in session.entries:
            if entry.request_id and entry.request_id not in ordered_request_ids:
                ordered_request_ids.append(entry.request_id)

        for rid in ordered_request_ids:
            entries = groups.get(rid, [])
            final = None
            for e in entries:
                if e.is_final:
                    final = e
                    break
            if final is None and entries:
                final = entries[-1]
            if final and final.usage and final.usage.total_cache > 0:
                ratios.append(final.usage.cache_read_ratio)

        if not ratios:
            return []

        avg_ratio = sum(ratios) / len(ratios)

        if avg_ratio < 0.40:
            severity = Severity.CRITICAL
        elif avg_ratio < 0.80:
            severity = Severity.WARN
        else:
            severity = Severity.INFO

        findings.append(
            Finding(
                detector_id=self.detector_id,
                severity=severity,
                title=self.display_name,
                detail=f"Average cache read ratio: {avg_ratio:.0%} across {len(ratios)} requests (healthy: >80%).",
                recommendation=(
                    "Low cache hit means each request re-sends most of the conversation uncached. "
                    "Start fresh sessions for new tasks. Avoid --resume on old sessions."
                    if severity != Severity.INFO
                    else "Cache efficiency is healthy."
                ),
                evidence=[f"Request {i + 1}: {r:.0%}" for i, r in enumerate(ratios[:5])],
            )
        )

        # Cold start detection: first N requests with low cache ratio
        early_ratios = ratios[:5]
        cold_starts = sum(1 for r in early_ratios if r < 0.50)
        if cold_starts >= 3:
            findings.append(
                Finding(
                    detector_id="cold_start",
                    severity=Severity.INFO,
                    title="Cold Start Waste",
                    detail=f"{cold_starts} cold cache rebuilds at session start.",
                    recommendation=(
                        "Multiple cold starts can waste tokens. "
                        "This is normal for new sessions but should not happen on --resume."
                    ),
                    evidence=[f"Request {i + 1}: {r:.0%}" for i, r in enumerate(early_ratios) if r < 0.50],
                    bug_ref="https://github.com/anthropics/claude-code/issues/42906",
                )
            )

        return findings
