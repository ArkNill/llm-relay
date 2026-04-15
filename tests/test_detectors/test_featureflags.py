"""Tests for FeatureFlags flag detector."""

from __future__ import annotations

from pathlib import Path

from helpers import make_entry, write_session_file

from llm_relay.detect.featureflags import FeatureFlagsDetector
from llm_relay.detect.models import FeatureFlagsConfig, ParsedSession, Severity
from llm_relay.detect.parser import parse_session


class TestFeatureFlagsDetector:
    def _make_session(self, tmp_path: Path) -> ParsedSession:
        session = tmp_path / "test.jsonl"
        write_session_file(session, [make_entry()])
        return parse_session(session)

    def test_warns_on_budget_cap(self, tmp_path: Path) -> None:
        parsed = self._make_session(tmp_path)
        gb = FeatureFlagsConfig(
            budget_window_window=200000,
            raw_flags={"config_budget_window_window": 200000},
        )
        findings = FeatureFlagsDetector().check(parsed, featureflags=gb)
        budget_findings = [f for f in findings if "Budget" in f.title]
        assert len(budget_findings) == 1
        assert budget_findings[0].severity == Severity.WARN

    def test_reports_flags_as_info(self, tmp_path: Path) -> None:
        parsed = self._make_session(tmp_path)
        gb = FeatureFlagsConfig(
            budget_window_window=300000,  # above threshold
            raw_flags={"config_budget_window_window": 300000, "config_ctx_gate": True},
        )
        findings = FeatureFlagsDetector().check(parsed, featureflags=gb)
        info_findings = [f for f in findings if f.severity == Severity.INFO]
        assert len(info_findings) >= 1

    def test_no_findings_without_config(self, tmp_path: Path) -> None:
        parsed = self._make_session(tmp_path)
        findings = FeatureFlagsDetector().check(parsed, featureflags=None)
        assert findings == []
