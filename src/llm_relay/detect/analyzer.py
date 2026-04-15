"""Orchestrator: runs all detectors and builds reports."""

from __future__ import annotations

from datetime import datetime, timezone

from llm_relay.detect import __version__, get_detectors_for_provider
from llm_relay.detect.models import Finding, FullReport, ParsedSession, SessionReport, Severity


def analyze_session(
    session: ParsedSession,
) -> SessionReport:
    """Run appropriate detectors on a single session."""
    findings: list[Finding] = []
    detectors = get_detectors_for_provider(session.provider)

    for detector in detectors:
        try:
            results = detector.check(session)
            findings.extend(results)
        except Exception:
            findings.append(
                Finding(
                    detector_id=detector.detector_id,
                    severity=Severity.INFO,
                    title=f"{detector.display_name} Error",
                    detail=f"Detector '{detector.detector_id}' raised an exception.",
                    recommendation="This may indicate an unusual session format.",
                )
            )

    # Sort: CRITICAL first, then WARN, then INFO
    findings.sort(key=lambda f: f.severity, reverse=True)

    return SessionReport(session=session, findings=findings)


def analyze_all(
    sessions: list[ParsedSession],
    total_sessions: int = 0,
) -> FullReport:
    """Analyze all sessions and produce a full report."""
    reports: list[SessionReport] = []

    for session in sessions:
        report = analyze_session(session)
        reports.append(report)

    return FullReport(
        session_reports=reports,
        scan_timestamp=datetime.now(timezone.utc).isoformat(),
        relay_version=__version__,
        sessions_scanned=len(sessions),
        total_sessions=total_sessions or len(sessions),
    )
