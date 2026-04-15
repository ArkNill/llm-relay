"""Base provider interface -- all providers must be stdlib-only."""

from __future__ import annotations

import abc
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llm_relay.detect.models import ParsedSession
    from llm_relay.detect.scanner import SessionFile


class ProviderAdapter(abc.ABC):
    """Abstract base for CLI tool session providers.

    Each provider knows how to discover and parse sessions
    for a specific CLI tool (Claude Code, OpenAI Codex, Gemini CLI).
    """

    provider_id: str  # e.g. "claude-code", "openai-codex", "gemini-cli"
    display_name: str  # e.g. "Claude Code", "OpenAI Codex", "Gemini CLI"

    @abc.abstractmethod
    def detect(self) -> bool:
        """Return True if this provider's session directory exists on disk."""
        ...

    @abc.abstractmethod
    def discover_sessions(
        self,
        limit: int | None = None,
        project_filter: str | None = None,
        session_filter: str | None = None,
    ) -> list[SessionFile]:
        """Find session files, sorted by mtime descending."""
        ...

    @abc.abstractmethod
    def parse_session(self, path: Path) -> ParsedSession:
        """Parse a session file into a ParsedSession."""
        ...

    def total_session_count(self, project_filter: str | None = None) -> int:
        """Return total number of sessions (no limit)."""
        return len(self.discover_sessions(project_filter=project_filter))
