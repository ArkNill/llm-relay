"""Data models for llm-relay -- all stdlib, zero dependencies."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class Severity(enum.Enum):
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Severity):
            return NotImplemented
        order = {Severity.INFO: 0, Severity.WARN: 1, Severity.CRITICAL: 2}
        return order[self] < order[other]


class Health(enum.Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class UsageData:
    """Token usage from a single API response (provider-agnostic)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    reasoning_tokens: int = 0  # OpenAI reasoning_tokens / Gemini thoughtsTokenCount
    total_tokens: int = 0  # provider-reported total (0 = auto-compute)

    @property
    def total_cache(self) -> int:
        return self.cache_creation_input_tokens + self.cache_read_input_tokens

    @property
    def cache_read_ratio(self) -> float:
        if self.total_cache == 0:
            return 0.0
        return self.cache_read_input_tokens / self.total_cache

    @property
    def computed_total(self) -> int:
        if self.total_tokens > 0:
            return self.total_tokens
        return self.input_tokens + self.output_tokens

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> UsageData:
        """Parse Anthropic API usage format."""
        return cls(
            input_tokens=d.get("input_tokens", 0),
            output_tokens=d.get("output_tokens", 0),
            cache_creation_input_tokens=d.get("cache_creation_input_tokens", 0),
            cache_read_input_tokens=d.get("cache_read_input_tokens", 0),
        )

    @classmethod
    def from_openai(cls, d: dict[str, Any]) -> UsageData:
        """Parse OpenAI API usage format."""
        input_details = d.get("input_tokens_details", {}) or {}
        output_details = d.get("output_tokens_details", {}) or {}
        return cls(
            input_tokens=d.get("input_tokens", 0),
            output_tokens=d.get("output_tokens", 0),
            cache_read_input_tokens=input_details.get("cached_tokens", 0),
            reasoning_tokens=output_details.get("reasoning_tokens", 0),
            total_tokens=d.get("total_tokens", 0),
        )

    @classmethod
    def from_gemini(cls, d: dict[str, Any]) -> UsageData:
        """Parse Gemini API usageMetadata format."""
        return cls(
            input_tokens=d.get("promptTokenCount", 0),
            output_tokens=d.get("candidatesTokenCount", 0),
            cache_read_input_tokens=d.get("cachedContentTokenCount", 0),
            reasoning_tokens=d.get("thoughtsTokenCount", 0),
            total_tokens=d.get("totalTokenCount", 0),
        )


@dataclass
class Entry:
    """Single JSONL line from a session file."""

    type: str  # user, assistant, system, queue-operation, etc.
    uuid: str = ""
    parent_uuid: str = ""
    timestamp: str = ""
    session_id: str = ""
    subtype: str = ""  # for system entries
    version: str = ""
    request_id: str = ""
    is_compact_summary: bool = False
    usage: UsageData | None = None
    model: str = ""
    stop_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_synthetic(self) -> bool:
        return self.model == "<synthetic>"

    @property
    def is_final(self) -> bool:
        return self.stop_reason != "" and self.stop_reason is not None

    def get_content_text(self) -> str:
        """Extract all text content from the message."""
        msg = self.raw.get("message", {})
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    parts.append(block.get("text", ""))
                    parts.append(block.get("content", "") if isinstance(block.get("content"), str) else "")
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(p for p in parts if p)
        return ""

    def get_tool_results(self) -> list[dict[str, Any]]:
        """Extract tool_result blocks from user message content."""
        msg = self.raw.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            return []
        return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]


@dataclass
class ParsedSession:
    """Fully parsed session with indexed entries."""

    path: str
    session_id: str
    project_path: str
    entries: list[Entry]
    file_size_bytes: int
    parse_errors: int = 0
    null_bytes_found: bool = False
    provider: str = "claude-code"  # provider ID that parsed this session

    @property
    def entry_count(self) -> int:
        return len(self.entries)

    @property
    def first_timestamp(self) -> str:
        for e in self.entries:
            if e.timestamp:
                return e.timestamp
        return ""

    @property
    def last_timestamp(self) -> str:
        for e in reversed(self.entries):
            if e.timestamp:
                return e.timestamp
        return ""

    @property
    def version(self) -> str:
        versions: list[str] = []
        for e in self.entries:
            if e.version and e.version not in versions:
                versions.append(e.version)
        return versions[-1] if versions else ""

    @property
    def all_versions(self) -> list[str]:
        seen: list[str] = []
        for e in self.entries:
            if e.version and e.version not in seen:
                seen.append(e.version)
        return seen

    def entries_by_type(self, entry_type: str) -> list[Entry]:
        return [e for e in self.entries if e.type == entry_type]

    def group_by_request_id(self) -> dict[str, list[Entry]]:
        groups: dict[str, list[Entry]] = {}
        for e in self.entries:
            if e.request_id:
                groups.setdefault(e.request_id, []).append(e)
        return groups


@dataclass
class Finding:
    """Single issue detected by a detector."""

    detector_id: str
    severity: Severity
    title: str
    detail: str
    recommendation: str
    evidence: list[str] = field(default_factory=list)
    bug_ref: str = ""


@dataclass
class SessionReport:
    """Findings for one session."""

    session: ParsedSession
    findings: list[Finding] = field(default_factory=list)

    @property
    def health(self) -> Health:
        if any(f.severity == Severity.CRITICAL for f in self.findings):
            return Health.UNHEALTHY
        if any(f.severity == Severity.WARN for f in self.findings):
            return Health.DEGRADED
        return Health.HEALTHY

    @property
    def cache_read_ratio(self) -> float | None:
        groups = self.session.group_by_request_id()
        ratios: list[float] = []
        for entries in groups.values():
            final = None
            for e in entries:
                if e.is_final:
                    final = e
                    break
            if final is None and entries:
                final = entries[-1]
            if final and final.usage and final.usage.total_cache > 0:
                ratios.append(final.usage.cache_read_ratio)
        if not ratios:
            return None
        return sum(ratios) / len(ratios)


@dataclass
class FullReport:
    """Complete scan report."""

    session_reports: list[SessionReport] = field(default_factory=list)
    global_findings: list[Finding] = field(default_factory=list)
    scan_timestamp: str = ""
    relay_version: str = ""
    sessions_scanned: int = 0
    total_sessions: int = 0

    @property
    def healthy_count(self) -> int:
        return sum(1 for r in self.session_reports if r.health == Health.HEALTHY)

    @property
    def degraded_count(self) -> int:
        return sum(1 for r in self.session_reports if r.health == Health.DEGRADED)

    @property
    def unhealthy_count(self) -> int:
        return sum(1 for r in self.session_reports if r.health == Health.UNHEALTHY)

    @property
    def worst_health(self) -> Health:
        if self.unhealthy_count > 0:
            return Health.UNHEALTHY
        if self.degraded_count > 0:
            return Health.DEGRADED
        return Health.HEALTHY

    @property
    def exit_code(self) -> int:
        if self.unhealthy_count > 0:
            return 2
        if self.degraded_count > 0:
            return 1
        return 0
