"""Provider registry -- auto-detect and load session providers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llm_relay.providers.base import ProviderAdapter

# Canonical provider IDs
CLAUDE_CODE = "claude-code"
OPENAI_CODEX = "openai-codex"
GEMINI_CLI = "gemini-cli"

_PROVIDER_CLASSES: list[type[ProviderAdapter]] | None = None


def _load_provider_classes() -> list[type[ProviderAdapter]]:
    global _PROVIDER_CLASSES
    if _PROVIDER_CLASSES is not None:
        return _PROVIDER_CLASSES

    from llm_relay.providers.claude_code import ClaudeCodeProvider
    from llm_relay.providers.gemini_cli import GeminiCliProvider
    from llm_relay.providers.openai_codex import OpenAICodexProvider

    _PROVIDER_CLASSES = [ClaudeCodeProvider, OpenAICodexProvider, GeminiCliProvider]
    return _PROVIDER_CLASSES


def get_provider(provider_id: str) -> ProviderAdapter:
    """Get a specific provider by ID."""
    for cls in _load_provider_classes():
        if cls.provider_id == provider_id:
            return cls()
    raise ValueError(f"Unknown provider: {provider_id!r}. Available: {list_provider_ids()}")


def detect_providers() -> list[ProviderAdapter]:
    """Return providers whose session directories exist on disk."""
    return [cls() for cls in _load_provider_classes() if cls().detect()]


def get_all_providers() -> list[ProviderAdapter]:
    """Return all registered providers regardless of detection."""
    return [cls() for cls in _load_provider_classes()]


def list_provider_ids() -> list[str]:
    """List all known provider IDs."""
    return [cls.provider_id for cls in _load_provider_classes()]
