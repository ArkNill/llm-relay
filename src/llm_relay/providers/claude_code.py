"""Claude Code session provider -- wraps existing scanner + parser."""

from __future__ import annotations

from pathlib import Path

from llm_relay.detect.models import ParsedSession
from llm_relay.detect.parser import parse_session
from llm_relay.detect.scanner import SessionFile, discover_sessions, find_projects_dir
from llm_relay.providers.base import ProviderAdapter


class ClaudeCodeProvider(ProviderAdapter):
    provider_id = "claude-code"
    display_name = "Claude Code"

    def detect(self) -> bool:
        return find_projects_dir().is_dir()

    def discover_sessions(
        self,
        limit: int | None = None,
        project_filter: str | None = None,
        session_filter: str | None = None,
    ) -> list[SessionFile]:
        return discover_sessions(
            limit=limit,
            project_filter=project_filter,
            session_filter=session_filter,
        )

    def parse_session(self, path: Path) -> ParsedSession:
        session = parse_session(path)
        session.provider = self.provider_id
        return session
