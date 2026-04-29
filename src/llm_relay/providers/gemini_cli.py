"""Gemini CLI session provider."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from llm_relay.detect.models import Entry, ParsedSession, UsageData
from llm_relay.detect.scanner import SessionFile
from llm_relay.providers.base import ProviderAdapter


def _find_gemini_home() -> Path:
    override = os.environ.get("CCPULSE_GEMINI_HOME")
    if override:
        return Path(override)
    return Path.home() / ".gemini"


def _find_chats_dirs() -> list[Path]:
    """Gemini stores chats in ~/.gemini/tmp/<project_hash>/chats/."""
    gemini_home = _find_gemini_home()
    tmp_dir = gemini_home / "tmp"
    if not tmp_dir.is_dir():
        return []
    dirs: list[Path] = []
    for project_dir in tmp_dir.iterdir():
        if not project_dir.is_dir():
            continue
        chats_dir = project_dir / "chats"
        if chats_dir.is_dir():
            dirs.append(chats_dir)
    return dirs


def _parse_gemini_entry(raw: dict[str, Any]) -> Entry:
    """Convert a Gemini JSONL dict into an Entry."""
    entry_type = raw.get("type", "")

    # Map Gemini types to generic types
    if entry_type == "gemini":
        entry_type = "assistant"

    model = raw.get("model", "")
    usage = None
    tokens = raw.get("tokens", raw.get("usageMetadata"))
    if isinstance(tokens, dict):
        usage = UsageData.from_gemini(tokens)

    return Entry(
        type=entry_type,
        uuid=raw.get("id", ""),
        timestamp=raw.get("timestamp", raw.get("createdAt", "")),
        session_id=raw.get("sessionId", ""),
        usage=usage,
        model=model,
        stop_reason=raw.get("finishReason", ""),
        raw=raw,
    )


class GeminiCliProvider(ProviderAdapter):
    provider_id = "gemini-cli"
    display_name = "Gemini CLI"

    def detect(self) -> bool:
        return len(_find_chats_dirs()) > 0

    def discover_sessions(
        self,
        limit: int | None = None,
        project_filter: str | None = None,
        session_filter: str | None = None,
    ) -> list[SessionFile]:
        results: list[SessionFile] = []

        for chats_dir in _find_chats_dirs():
            # project_hash is the parent directory name
            project_hash = chats_dir.parent.name
            if project_filter and project_filter not in project_hash:
                continue

            # Gemini uses both .jsonl and .json files
            for pattern in ("*.jsonl", "*.json"):
                for session_file in chats_dir.glob(pattern):
                    session_id = session_file.stem
                    if session_filter and not session_id.startswith(session_filter):
                        continue

                    try:
                        stat = session_file.stat()
                    except OSError:
                        continue

                    results.append(
                        SessionFile(
                            path=session_file,
                            project_dir=project_hash,
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
                content = f.read().strip()

            if not content:
                pass
            elif content.startswith("["):
                # Legacy JSON array format
                try:
                    records = json.loads(content)
                    if isinstance(records, list):
                        for raw in records:
                            if isinstance(raw, dict):
                                entries.append(_parse_gemini_entry(raw))
                except json.JSONDecodeError:
                    parse_errors += 1
            else:
                # JSONL format (one record per line)
                for line in content.split("\n"):
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
                    entries.append(_parse_gemini_entry(raw))
        except OSError:
            pass

        # Derive project from path structure
        project_path = path.parent.parent.name if path.parent.name == "chats" else path.parent.name

        return ParsedSession(
            path=str(path),
            session_id=path.stem,
            project_path=project_path,
            entries=entries,
            file_size_bytes=file_size,
            parse_errors=parse_errors,
            provider=self.provider_id,
        )
