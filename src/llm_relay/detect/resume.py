"""Detector: Resume Corruption -- timestamp anomalies, null bytes, version mismatch, DAG breaks (#43044)."""

from __future__ import annotations

from llm_relay.detect.base import BaseDetector
from llm_relay.detect.models import Finding, ParsedSession, Severity


class ResumeDetector(BaseDetector):
    detector_id = "resume"
    display_name = "Resume Issues"

    def check(self, session: ParsedSession) -> list[Finding]:
        findings: list[Finding] = []

        # Check 1: Null bytes in file
        if session.null_bytes_found:
            findings.append(
                Finding(
                    detector_id=self.detector_id,
                    severity=Severity.WARN,
                    title="Null Byte Corruption",
                    detail="Session file contains null bytes or replacement characters -- possible data corruption.",
                    recommendation=(
                        "This session may have been corrupted during a resume or crash. "
                        "Start a new session."
                    ),
                    bug_ref="https://github.com/anthropics/claude-code/issues/43044",
                )
            )

        # Check 2: Timestamp reversals
        timestamps = [e.timestamp for e in session.entries if e.timestamp]
        reversals = 0
        reversal_evidence: list[str] = []
        for i in range(1, len(timestamps)):
            if timestamps[i] < timestamps[i - 1]:
                reversals += 1
                if len(reversal_evidence) < 3:
                    reversal_evidence.append(f"{timestamps[i - 1]} > {timestamps[i]} at index {i}")

        if reversals > 0:
            findings.append(
                Finding(
                    detector_id=self.detector_id,
                    severity=Severity.WARN,
                    title="Timestamp Reversal",
                    detail=(
                        f"{reversals} timestamp reversal{'s' if reversals != 1 else ''} detected -- "
                        f"entries appear out of chronological order."
                    ),
                    recommendation=(
                        "Timestamp reversals may indicate ExY fallback corruption from resume. "
                        "Avoid --resume on this session."
                    ),
                    evidence=reversal_evidence,
                    bug_ref="https://github.com/anthropics/claude-code/issues/43044",
                )
            )

        # Check 3: Multiple versions (cross-version resume)
        versions = session.all_versions
        if len(versions) > 1:
            findings.append(
                Finding(
                    detector_id=self.detector_id,
                    severity=Severity.INFO,
                    title="Cross-Version Session",
                    detail=f"Session spans {len(versions)} Claude Code versions: {', '.join(versions)}.",
                    recommendation="Sessions resumed across versions may have compatibility issues.",
                    evidence=[f"v{v}" for v in versions],
                )
            )

        # Check 4: DAG breaks (parentUuid=null mid-session)
        dag_breaks = 0
        for i, entry in enumerate(session.entries):
            if i == 0:
                continue
            if entry.type not in ("user", "assistant"):
                continue
            if entry.is_compact_summary:
                continue
            if not entry.parent_uuid and entry.uuid:
                dag_breaks += 1

        if dag_breaks > 0:
            findings.append(
                Finding(
                    detector_id=self.detector_id,
                    severity=Severity.WARN,
                    title="DAG Breaks",
                    detail=(
                        f"{dag_breaks} message{'s' if dag_breaks != 1 else ''} with null parentUuid mid-session -- "
                        f"conversation threading is broken."
                    ),
                    recommendation="Broken message chains can cause context loss on resume.",
                    bug_ref="https://github.com/anthropics/claude-code/issues/43044",
                )
            )

        # Check 5: Parse errors (indicates file corruption)
        if session.parse_errors > 0:
            findings.append(
                Finding(
                    detector_id=self.detector_id,
                    severity=Severity.WARN,
                    title="Parse Errors",
                    detail=f"{session.parse_errors} malformed JSON lines in session file.",
                    recommendation="Malformed lines indicate file corruption. Do not resume this session.",
                )
            )

        return findings
