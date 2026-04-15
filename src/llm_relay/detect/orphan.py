"""Detector: Orphan tool calls -- tool_use without matching tool_result and vice versa.

Absorbs the two-pass correlation algorithm from kolkov/ccdiag.
"""

from __future__ import annotations

from llm_relay.detect.base import BaseDetector
from llm_relay.detect.models import Finding, ParsedSession, Severity


class OrphanDetector(BaseDetector):
    detector_id = "orphan"
    display_name = "Orphan Tool Calls"

    def check(self, session: ParsedSession) -> list[Finding]:
        # Pass 1: index tool_use and tool_result by ID
        tool_uses: dict[str, tuple[int, str]] = {}   # tool_use_id → (entry_idx, tool_name)
        tool_results: dict[str, int] = {}              # tool_use_id → entry_idx

        for idx, entry in enumerate(session.entries):
            msg = entry.raw.get("message", {})
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue

            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")

                if btype == "tool_use":
                    tid = block.get("id", "")
                    name = block.get("name", "unknown")
                    if tid:
                        tool_uses[tid] = (idx, name)

                elif btype == "tool_result":
                    tid = block.get("tool_use_id", "")
                    if tid:
                        tool_results[tid] = idx

        # Pass 2: find orphans
        orphan_uses: list[tuple[str, str, int]] = []  # (id, name, entry_idx)
        orphan_results: list[tuple[str, int]] = []     # (id, entry_idx)

        for tid, (eidx, name) in tool_uses.items():
            if tid not in tool_results:
                orphan_uses.append((tid, name, eidx))

        for tid, eidx in tool_results.items():
            if tid not in tool_uses:
                orphan_results.append((tid, eidx))

        findings: list[Finding] = []

        if orphan_uses:
            names = {}
            for _, name, _ in orphan_uses:
                names[name] = names.get(name, 0) + 1
            breakdown = ", ".join(f"{n}x {name}" for name, n in sorted(names.items(), key=lambda x: -x[1]))

            findings.append(Finding(
                detector_id=self.detector_id,
                severity=Severity.WARN if len(orphan_uses) > 3 else Severity.INFO,
                title="Orphan tool_use (no result)",
                detail=(
                    f"{len(orphan_uses)} tool_use blocks have no matching tool_result. "
                    f"Breakdown: {breakdown}. "
                    f"These calls may have timed out, been interrupted, or lost during compaction."
                ),
                recommendation=(
                    "Orphan tool_use blocks can cause 400 errors on --resume. "
                    "Consider running llm-relay doctor --fix to clean them up."
                ),
                evidence=[f"tool_use id={tid[:16]} name={name}" for tid, name, _ in orphan_uses[:5]],
            ))

        if orphan_results:
            findings.append(Finding(
                detector_id=self.detector_id,
                severity=Severity.WARN,
                title="Orphan tool_result (no matching use)",
                detail=(
                    f"{len(orphan_results)} tool_result blocks have no matching tool_use. "
                    f"This can cause API errors when the session is resumed."
                ),
                recommendation=(
                    "Run llm-relay doctor --fix to remove orphaned tool_result blocks."
                ),
                evidence=[f"tool_use_id={tid[:16]}" for tid, _ in orphan_results[:5]],
            ))

        return findings
