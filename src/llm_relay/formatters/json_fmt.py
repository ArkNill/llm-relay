"""JSON formatter -- stdlib only."""

from __future__ import annotations

import json
from typing import Any

from llm_relay.detect.models import Finding, FullReport, SessionReport
from llm_relay.formatters.base import BaseFormatter


def _finding_to_dict(f: Finding) -> dict[str, Any]:
    d: dict[str, Any] = {
        "detector": f.detector_id,
        "severity": f.severity.value,
        "title": f.title,
        "detail": f.detail,
        "recommendation": f.recommendation,
    }
    if f.evidence:
        d["evidence"] = f.evidence
    if f.bug_ref:
        d["bug_ref"] = f.bug_ref
    return d


def _session_report_to_dict(sr: SessionReport) -> dict[str, Any]:
    s = sr.session
    return {
        "session_id": s.session_id,
        "provider": s.provider,
        "project": s.project_path,
        "file_size_bytes": s.file_size_bytes,
        "entry_count": s.entry_count,
        "first_timestamp": s.first_timestamp,
        "last_timestamp": s.last_timestamp,
        "version": s.version,
        "health": sr.health.value,
        "cache_read_ratio": sr.cache_read_ratio,
        "findings": [_finding_to_dict(f) for f in sr.findings],
    }


class JsonFormatter(BaseFormatter):
    def format(self, report: FullReport) -> str:
        data: dict[str, Any] = {
            "relay_version": report.relay_version,
            "scan_timestamp": report.scan_timestamp,
            "summary": {
                "sessions_scanned": report.sessions_scanned,
                "total_sessions": report.total_sessions,
                "healthy": report.healthy_count,
                "degraded": report.degraded_count,
                "unhealthy": report.unhealthy_count,
            },
            "sessions": [_session_report_to_dict(sr) for sr in report.session_reports],
            "global_findings": [_finding_to_dict(f) for f in report.global_findings],
        }

        return json.dumps(data, indent=2, ensure_ascii=False)
