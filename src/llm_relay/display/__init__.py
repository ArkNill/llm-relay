"""Turn counter display page — static file serving helper."""

from __future__ import annotations

from pathlib import Path


def get_static_dir() -> Path:
    """Return the path to the display page's static files."""
    return Path(__file__).parent / "static"
