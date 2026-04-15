"""Plain text formatter -- stdlib only."""

from __future__ import annotations

from llm_relay.detect.models import FullReport, Health, SessionReport, Severity
from llm_relay.formatters.base import BaseFormatter


def _severity_prefix(s: Severity) -> str:
    return {"critical": "CRITICAL", "warn": "WARN", "info": "INFO"}.get(s.value, "?")


def _health_label(h: Health) -> str:
    return {"healthy": "HEALTHY", "degraded": "DEGRADED", "unhealthy": "UNHEALTHY"}.get(h.value, "?")


def _format_size(size_bytes: int) -> str:
    if size_bytes >= 1_048_576:
        return f"{size_bytes / 1_048_576:.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def _format_session_report(report: SessionReport, verbose: bool = False) -> str:
    """Format a single session report as plain text."""
    s = report.session
    health = _health_label(report.health)
    sid = s.session_id[:8]

    # Date from first timestamp
    date = ""
    if s.first_timestamp:
        date = s.first_timestamp[:10]

    prov = f"[{s.provider}] " if s.provider != "claude-code" else ""
    header = f"  {health:11s} {prov}{sid}  ({date}, {_format_size(s.file_size_bytes)}, {s.entry_count} entries)"

    if report.health == Health.HEALTHY and not verbose:
        return header

    lines = [header]
    for i, f in enumerate(report.findings):
        prefix = _severity_prefix(f.severity)
        connector = "|--" if i < len(report.findings) - 1 else "`--"
        lines.append(f"  {connector} {prefix}  {f.title}")
        lines.append(f"  |  {f.detail}")
        if f.recommendation:
            lines.append(f"  |  -> {f.recommendation}")
        if f.bug_ref:
            lines.append(f"  |  Ref: {f.bug_ref}")

    return "\n".join(lines)


class PlainFormatter(BaseFormatter):
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose

    def format(self, report: FullReport) -> str:
        lines = [
            f"llm-relay v{report.relay_version} -- AI CLI Session Health Check",
            f"Scanned {report.sessions_scanned} of {report.total_sessions} sessions.",
            "",
            (
                f"Overall: {report.healthy_count} healthy, "
                f"{report.degraded_count} degraded, "
                f"{report.unhealthy_count} unhealthy"
            ),
            "",
        ]

        # Show unhealthy first, then degraded, then healthy (if verbose)
        for health_filter in [Health.UNHEALTHY, Health.DEGRADED, Health.HEALTHY]:
            for sr in report.session_reports:
                if sr.health != health_filter:
                    continue
                if sr.health == Health.HEALTHY and not self.verbose:
                    continue
                lines.append(_format_session_report(sr, self.verbose))
                lines.append("")

        # Global findings
        if report.global_findings:
            lines.append("-- Global --")
            for f in report.global_findings:
                prefix = _severity_prefix(f.severity)
                lines.append(f"  {prefix}  {f.title}")
                lines.append(f"  {f.detail}")
                if f.evidence:
                    for ev in f.evidence[:3]:
                        lines.append(f"    {ev}")
            lines.append("")

        lines.append("This tool is READ-ONLY. No session files were modified.")
        return "\n".join(lines)
