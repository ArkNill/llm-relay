"""Detector: Stuck tool calls -- tool_use followed by session continuation but no result.

Absorbs stuck-call detection and per-tool duration analysis from kolkov/ccdiag.
"""

from __future__ import annotations

import statistics
from datetime import datetime

from llm_relay.detect.base import BaseDetector
from llm_relay.detect.models import Finding, ParsedSession, Severity


def _parse_ts(ts_str: str) -> float | None:
    """Parse ISO timestamp to epoch seconds.  Returns None on failure."""
    if not ts_str:
        return None
    try:
        # Handle various ISO formats
        ts_str = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_str)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


class StuckDetector(BaseDetector):
    detector_id = "stuck"
    display_name = "Stuck Tool Calls"

    STUCK_THRESHOLD_S = 120  # 2 minutes without result = stuck

    def check(self, session: ParsedSession) -> list[Finding]:
        # Build tool_use index: id → (entry_idx, name, timestamp)
        tool_uses: dict[str, tuple[int, str, float | None]] = {}
        tool_results: set[str] = set()

        for idx, entry in enumerate(session.entries):
            msg = entry.raw.get("message", {})
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue

            ts = _parse_ts(entry.timestamp)

            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "tool_use":
                    tid = block.get("id", "")
                    name = block.get("name", "unknown")
                    if tid:
                        tool_uses[tid] = (idx, name, ts)
                elif btype == "tool_result":
                    tid = block.get("tool_use_id", "")
                    if tid:
                        tool_results.add(tid)

        # Compute per-tool duration stats
        durations: dict[str, list[float]] = {}
        for tid, (use_idx, name, use_ts) in tool_uses.items():
            if tid not in tool_results or use_ts is None:
                continue
            # Find result entry timestamp
            for entry in session.entries[use_idx + 1 :]:
                msg = entry.raw.get("message", {})
                content = msg.get("content", [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if isinstance(block, dict) and block.get("tool_use_id") == tid:
                        result_ts = _parse_ts(entry.timestamp)
                        if result_ts is not None:
                            dur = result_ts - use_ts
                            durations.setdefault(name, []).append(dur)
                        break

        # Detect stuck calls: tool_use with no result + session continued afterward
        last_entry_idx = len(session.entries) - 1
        stuck_calls: list[tuple[str, str, int]] = []  # (id, name, entry_idx)

        for tid, (use_idx, name, use_ts) in tool_uses.items():
            if tid in tool_results:
                continue
            # Session continued after this tool_use?
            if use_idx < last_entry_idx - 1:
                stuck_calls.append((tid, name, use_idx))

        findings: list[Finding] = []

        if stuck_calls:
            names: dict[str, int] = {}
            for _, name, _ in stuck_calls:
                names[name] = names.get(name, 0) + 1
            breakdown = ", ".join(f"{n}x {name}" for name, n in sorted(names.items(), key=lambda x: -x[1]))

            findings.append(Finding(
                detector_id=self.detector_id,
                severity=Severity.WARN if len(stuck_calls) > 2 else Severity.INFO,
                title="Stuck tool calls",
                detail=(
                    f"{len(stuck_calls)} tool calls never received a result but the session "
                    f"continued. Breakdown: {breakdown}. "
                    f"Claude may have abandoned these calls or they timed out silently."
                ),
                recommendation=(
                    "Stuck calls waste context space. The session should recover normally, "
                    "but large numbers may indicate connectivity issues."
                ),
                evidence=[f"tool_use id={tid[:16]} name={name}" for tid, name, _ in stuck_calls[:5]],
            ))

        # Report per-tool duration stats
        if durations:
            slow_tools: list[str] = []
            for name, durs in sorted(durations.items()):
                if len(durs) < 2:
                    continue
                med = statistics.median(durs)
                mx = max(durs)
                if mx > self.STUCK_THRESHOLD_S:
                    slow_tools.append(f"{name}: median={med:.1f}s max={mx:.1f}s (n={len(durs)})")

            if slow_tools:
                findings.append(Finding(
                    detector_id=self.detector_id,
                    severity=Severity.INFO,
                    title="Slow tool execution",
                    detail="Tools with max duration >" + f"{self.STUCK_THRESHOLD_S}s: " + "; ".join(slow_tools),
                    recommendation="Consider whether these tools are timing out or encountering issues.",
                ))

        return findings
