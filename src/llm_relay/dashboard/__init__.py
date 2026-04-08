"""Dashboard static file serving helper."""

from __future__ import annotations

from pathlib import Path


def get_static_dir() -> Path:
    """Return the path to the dashboard's pre-built static files."""
    return Path(__file__).parent / "static"
