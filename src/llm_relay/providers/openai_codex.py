"""OpenAI Codex CLI session provider."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from llm_relay.detect.models import Entry, ParsedSession, UsageData
from llm_relay.detect.scanner import SessionFile
from llm_relay.providers.base import ProviderAdapter


def _find_codex_home() -> Path:
    override = os.environ.get("CCPULSE_CODEX_HOME")
    if override:
        return Path(override)
    return Path.home() / ".codex"


def _find_sessions_dir() -> Path:
    return _find_codex_home() / "sessions"


def _parse_codex_entry(raw: dict[str, Any]) -> Entry:
    """Convert a Codex JSONL dict into an Entry."""
    entry_type = raw.get("type", raw.get("role", ""))

    model = raw.get("model", "")
    usage = None
    tokens = raw.get("tokens", raw.get("usage"))
    if isinstance(tokens, dict):
        usage = UsageData.from_openai(tokens)

    return Entry(
        type=entry_type,
        uuid=raw.get("message_id", raw.get("id", "")),
        timestamp=raw.get("timestamp", raw.get("created_at", "")),
        session_id=raw.get("session_id", ""),
        usage=usage,
        model=model,
        stop_reason=raw.get("status", ""),
        raw=raw,
    )


class OpenAICodexProvider(ProviderAdapter):
    provider_id = "openai-codex"
    display_name = "OpenAI Codex"

    def detect(self) -> bool:
        sessions_dir = _find_sessions_dir()
        if sessions_dir.is_dir():
            # Check if there are any jsonl files anywhere under sessions/
            return any(sessions_dir.rglob("*.jsonl"))
        return False

    def discover_sessions(
        self,
        limit: int | None = None,
        project_filter: str | None = None,
        session_filter: str | None = None,
    ) -> list[SessionFile]:
        sessions_dir = _find_sessions_dir()
        if not sessions_dir.is_dir():
            return []

        results: list[SessionFile] = []

        # Codex stores sessions as: sessions/YYYY/MM/DD/rollout-*.jsonl
        for jsonl_file in sessions_dir.rglob("*.jsonl"):
            session_id = jsonl_file.stem
            if session_filter and not session_id.startswith(session_filter):
                continue

            # Use relative path from sessions_dir as "project" identifier
            project_dir = str(jsonl_file.parent.relative_to(sessions_dir))
            if project_filter and project_filter not in project_dir:
                continue

            try:
                stat = jsonl_file.stat()
            except OSError:
                continue

            results.append(
                SessionFile(
                    path=jsonl_file,
                    project_dir=project_dir,
                    session_id=session_id,
                    size_bytes=stat.st_size,
                    mtime=stat.st_mtime,
                )
            )

        results.sort(key=lambda s: s.mtime, reverse=True)

        if limit is not None:
            results = results[:limit]

        return results

    def parse_session(self, path: Path) -> ParsedSession:
        entries: list[Entry] = []
        parse_errors = 0

        try:
            file_size = path.stat().st_size
        except OSError:
            file_size = 0

        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        raw = json.loads(stripped)
                    except json.JSONDecodeError:
                        parse_errors += 1
                        continue
                    if not isinstance(raw, dict):
                        parse_errors += 1
                        continue
                    entries.append(_parse_codex_entry(raw))
        except OSError:
            pass

        # Derive project from path structure
        sessions_dir = _find_sessions_dir()
        try:
            project_path = str(path.parent.relative_to(sessions_dir))
        except ValueError:
            project_path = path.parent.name

        return ParsedSession(
            path=str(path),
            session_id=path.stem,
            project_path=project_path,
            entries=entries,
            file_size_bytes=file_size,
            parse_errors=parse_errors,
            provider=self.provider_id,
        )
