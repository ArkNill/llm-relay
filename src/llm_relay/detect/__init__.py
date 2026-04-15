"""llm-relay detect -- Diagnostic health-check for LLM CLI sessions."""

from __future__ import annotations

from typing import TYPE_CHECKING

__version__ = "0.3.0"
__all__ = ["__version__", "get_all_detectors", "get_detectors_for_provider"]

if TYPE_CHECKING:
    from llm_relay.detect.base import BaseDetector


def get_all_detectors() -> list[BaseDetector]:
    """Return all Claude Code detectors (backward-compatible)."""
    return get_detectors_for_provider("claude-code")


def get_common_detectors() -> list[BaseDetector]:
    """Detectors that work across all providers."""
    from llm_relay.detect.cache import CacheDetector

    return [
        CacheDetector(),
    ]


def get_detectors_for_provider(provider_id: str) -> list[BaseDetector]:
    """Return detectors appropriate for the given provider."""
    common = get_common_detectors()

    if provider_id == "claude-code":
        from llm_relay.detect.bloat import BloatDetector
        from llm_relay.detect.microcompact import MicrocompactDetector
        from llm_relay.detect.orphan import OrphanDetector
        from llm_relay.detect.resume import ResumeDetector
        from llm_relay.detect.stuck import StuckDetector
        from llm_relay.detect.synthetic import SyntheticDetector

        return [
            SyntheticDetector(),
            *common,
            MicrocompactDetector(),
            BloatDetector(),
            ResumeDetector(),
            OrphanDetector(),
            StuckDetector(),
        ]

    if provider_id == "openai-codex":
        return [*common]

    if provider_id == "gemini-cli":
        return [*common]

    return common
