"""Discover Claude Code session files on disk."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from llm_relay.detect.models import GrowthBookConfig


def find_claude_home() -> Path:
    """Locate the Claude Code data directory."""
    override = os.environ.get("CCPULSE_CLAUDE_HOME")
    if override:
        return Path(override)
    return Path.home() / ".claude"


def find_projects_dir() -> Path:
    return find_claude_home() / "projects"


@dataclass
class SessionFile:
    """Metadata about a discovered session file."""

    path: Path
    project_dir: str  # e.g. "-home-user-myproject"
    session_id: str  # UUID portion of filename
    size_bytes: int
    mtime: float

    @property
    def short_id(self) -> str:
        return self.session_id[:8]


def discover_sessions(
    projects_dir: Path | None = None,
    limit: int | None = None,
    project_filter: str | None = None,
    session_filter: str | None = None,
) -> list[SessionFile]:
    """Find all session JSONL files, sorted by mtime descending (newest first)."""
    if projects_dir is None:
        projects_dir = find_projects_dir()

    if not projects_dir.is_dir():
        return []

    results: list[SessionFile] = []

    for project_entry in projects_dir.iterdir():
        if not project_entry.is_dir():
            continue
        if project_filter and project_filter not in project_entry.name:
            continue

        for jsonl_file in project_entry.glob("*.jsonl"):
            # Skip subagent files (they live in subdirectories)
            if "/subagents/" in str(jsonl_file):
                continue

            session_id = jsonl_file.stem
            if session_filter and not session_id.startswith(session_filter):
                continue

            try:
                stat = jsonl_file.stat()
            except OSError:
                continue

            results.append(
                SessionFile(
                    path=jsonl_file,
                    project_dir=project_entry.name,
                    session_id=session_id,
                    size_bytes=stat.st_size,
                    mtime=stat.st_mtime,
                )
            )

    results.sort(key=lambda s: s.mtime, reverse=True)

    if limit is not None:
        results = results[:limit]

    return results


def total_session_size(sessions: list[SessionFile]) -> int:
    return sum(s.size_bytes for s in sessions)


def load_growthbook_config() -> GrowthBookConfig | None:
    """Extract GrowthBook feature flags from ~/.claude.json."""
    claude_json_path = find_claude_home().parent / ".claude.json"
    # Also check home directory directly
    alt_path = Path.home() / ".claude.json"

    target = None
    for p in [claude_json_path, alt_path]:
        if p.is_file():
            target = p
            break

    if target is None:
        return None

    try:
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    features: dict[str, Any] = data.get("cachedGrowthBookFeatures", {})
    if not features:
        return None

    config = GrowthBookConfig(raw_flags={})

    for key, value in features.items():
        if "hawthorn" in key:
            config.hawthorn_window = value if isinstance(value, int) else None
        elif "pewter" in key and "kestrel" in key:
            config.pewter_kestrel = value if isinstance(value, dict) else None
        elif "slate" in key and "heron" in key:
            config.slate_heron = value
        elif "sm_compact" in key and "config" not in key:
            config.sm_compact = value

        # Keep all tengu_ flags
        if key.startswith("tengu_") or "tengu" in key.lower():
            config.raw_flags[key] = value

    return config


def load_stats_cache() -> dict[str, Any] | None:
    """Load stats-cache.json for global usage data."""
    stats_path = find_claude_home() / "stats-cache.json"
    if not stats_path.is_file():
        return None
    try:
        with open(stats_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
