"""Shared test fixtures for llm-relay."""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest

# Isolate the suite from the operator's real databases (2026-09-28).
# llm_relay.proxy.db picks PG-vs-SQLite from LLM_RELAY_DB at import time and now
# also loads the repo's .env.local (which points at the production PG). Without
# this block the suite wrote into the real stores: test delegation rows landed in
# the frozen ~/.llm-relay/usage.db on 5/20, 5/21, 8/5 and 9/28, and a stray
# SQLite file appeared in the repo root. conftest is imported before any test
# module and the env loaders use setdefault, so an explicit value here wins.
# Set LLM_RELAY_TEST_DB to run the suite against a dedicated database instead.
_TEST_DB_DIR = tempfile.mkdtemp(prefix="llm-relay-tests-")
os.environ["LLM_RELAY_DB"] = os.environ.get("LLM_RELAY_TEST_DB") or str(Path(_TEST_DB_DIR) / "usage.db")
os.environ["LLM_RELAY_SPILL_DIR"] = _TEST_DB_DIR
atexit.register(shutil.rmtree, _TEST_DB_DIR, True)

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
