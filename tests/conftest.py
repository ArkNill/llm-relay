"""Shared test fixtures for llm-relay."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def tmp_claude_home(tmp_path: Path) -> Path:
    """Create a temporary ~/.claude/ structure."""
    claude_home = tmp_path / ".claude"
    projects = claude_home / "projects" / "test-project"
    projects.mkdir(parents=True)
    return claude_home
