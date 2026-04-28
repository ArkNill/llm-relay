"""llm-relay: Unified LLM usage management -- proxy, diagnostics, orchestration."""

from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("llm-relay")
except Exception:
    __version__ = "0.0.0"
