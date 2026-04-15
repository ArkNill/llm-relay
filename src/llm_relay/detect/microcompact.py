"""Detector: Context Stripping -- microcompact + tool result clearing (Bug B4, #42542)."""

from __future__ import annotations

from llm_relay.detect.base import BaseDetector
from llm_relay.detect.models import Finding, ParsedSession, Severity

CLEARED_MARKER = "[Old tool result content cleared]"


class MicrocompactDetector(BaseDetector):
    detector_id = "microcompact"
    display_name = "Context Stripping (B4)"

    def check(self, session: ParsedSession) -> list[Finding]:
        findings: list[Finding] = []

        # Method 1: Scan user entries for cleared tool results
        cleared_count = 0
        cleared_evidence: list[str] = []

        for entry in session.entries:
            if entry.type != "user":
                continue
            tool_results = entry.get_tool_results()
            for tr in tool_results:
                content = tr.get("content", "")
                if isinstance(content, str) and CLEARED_MARKER in content:
                    cleared_count += 1
                    if len(cleared_evidence) < 5:
                        ts = entry.timestamp or "?"
                        tool_id = tr.get("tool_use_id", "?")[:12]
                        cleared_evidence.append(f"tool_result {tool_id} at {ts}")

            # Also check raw text content
            text = entry.get_content_text()
            if CLEARED_MARKER in text and not tool_results:
                cleared_count += 1

        # Method 2: Check for boundary system entries
        compact_boundaries = sum(1 for e in session.entries if e.type == "system" and e.subtype == "compact_boundary")
        microcompact_boundaries = sum(
            1 for e in session.entries if e.type == "system" and e.subtype == "microcompact_boundary"
        )

        if cleared_count == 0 and compact_boundaries == 0 and microcompact_boundaries == 0:
            return []

        if cleared_count > 0:
            severity = Severity.CRITICAL if cleared_count > 50 else Severity.WARN
            findings.append(
                Finding(
                    detector_id=self.detector_id,
                    severity=severity,
                    title=self.display_name,
                    detail=(
                        f"{cleared_count} tool result{'s' if cleared_count != 1 else ''} "
                        f"replaced with [{CLEARED_MARKER}]. "
                        f"Context is being silently stripped."
                    ),
                    recommendation="Start fresh sessions every 15-20 tool uses to avoid context loss.",
                    evidence=cleared_evidence,
                    bug_ref="https://github.com/anthropics/claude-code/issues/42542",
                )
            )

        if compact_boundaries > 0 or microcompact_boundaries > 0:
            parts: list[str] = []
            if compact_boundaries:
                parts.append(f"{compact_boundaries} compact")
            if microcompact_boundaries:
                parts.append(f"{microcompact_boundaries} microcompact")
            findings.append(
                Finding(
                    detector_id="compact_boundary",
                    severity=Severity.INFO,
                    title="Compaction Events",
                    detail=f"{' + '.join(parts)} boundary events detected.",
                    recommendation="Compaction reduces context window. This is expected in long sessions.",
                )
            )

        return findings
