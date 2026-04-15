"""Detector: FeatureFlags Feature Flag Analysis -- global (not per-session)."""

from __future__ import annotations

from llm_relay.detect.base import BaseDetector
from llm_relay.detect.models import Finding, FeatureFlagsConfig, ParsedSession, Severity


class FeatureFlagsDetector(BaseDetector):
    detector_id = "featureflags"
    display_name = "FeatureFlags Flags"

    def check(self, session: ParsedSession, featureflags: FeatureFlagsConfig | None = None) -> list[Finding]:
        if featureflags is None:
            return []

        findings: list[Finding] = []
        evidence: list[str] = []

        # Tool result budget cap
        if featureflags.budget_window_window is not None:
            cap = featureflags.budget_window_window
            evidence.append(f"Tool result budget: {cap:,} chars")
            if cap <= 200000:
                findings.append(
                    Finding(
                        detector_id=self.detector_id,
                        severity=Severity.WARN,
                        title="Tool Result Budget Active",
                        detail=(
                            f"Aggregate tool result cap is {cap:,} characters. "
                            f"Results beyond this limit are silently truncated."
                        ),
                        recommendation=(
                            "Long sessions with many file reads will hit this cap. "
                            "Start fresh sessions every 15-20 tool uses."
                        ),
                        bug_ref="https://github.com/anthropics/claude-code/issues/42542",
                    )
                )

        # Per-tool caps
        if featureflags.per_tool_caps and isinstance(featureflags.per_tool_caps, dict):
            caps = featureflags.per_tool_caps
            parts = [f"{k}={v:,}" for k, v in caps.items() if isinstance(v, int)]
            if parts:
                evidence.append(f"Per-tool caps: {', '.join(parts)}")

        # Report all config_ flags as info
        if featureflags.raw_flags:
            flag_names = sorted(featureflags.raw_flags.keys())
            findings.append(
                Finding(
                    detector_id=self.detector_id,
                    severity=Severity.INFO,
                    title="Active FeatureFlags Flags",
                    detail=f"{len(flag_names)} config_ flags detected in ~/.claude.json.",
                    recommendation="These flags control server-side context management behavior.",
                    evidence=evidence or [f"{k}: {v}" for k, v in list(featureflags.raw_flags.items())[:5]],
                )
            )

        return findings
