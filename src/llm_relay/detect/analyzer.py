"""Orchestrator: runs all detectors and builds reports."""

from __future__ import annotations

from datetime import datetime, timezone

from llm_relay.detect import __version__, get_detectors_for_provider
from llm_relay.detect.models import Finding, FullReport, GrowthBookConfig, ParsedSession, SessionReport, Severity


def analyze_session(
    session: ParsedSession,
    growthbook: GrowthBookConfig | None = None,
) -> SessionReport:
    """Run appropriate detectors on a single session."""
    findings: list[Finding] = []
    detectors = get_detectors_for_provider(session.provider)

    for detector in detectors:
        # GrowthBook detector only runs with config
        if detector.detector_id == "growthbook" and growthbook is None:
            continue
        try:
            results = detector.check(session, growthbook=growthbook)
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
    growthbook: GrowthBookConfig | None = None,
    total_sessions: int = 0,
) -> FullReport:
    """Analyze all sessions and produce a full report."""
    reports: list[SessionReport] = []
    global_findings: list[Finding] = []

    for session in sessions:
        report = analyze_session(session, growthbook=growthbook)
        reports.append(report)

    # GrowthBook findings are global -- extract from the first session report and deduplicate
    gb_finding_ids: set = set()
    for report in reports:
        for finding in list(report.findings):
            if finding.detector_id == "growthbook":
                key = (finding.detector_id, finding.title)
                if key not in gb_finding_ids:
                    global_findings.append(finding)
                    gb_finding_ids.add(key)
                report.findings.remove(finding)

    return FullReport(
        session_reports=reports,
        global_findings=global_findings,
        growthbook=growthbook,
        scan_timestamp=datetime.now(timezone.utc).isoformat(),
        relay_version=__version__,
        sessions_scanned=len(sessions),
        total_sessions=total_sessions or len(sessions),
    )
