"""Domain models for CLI orchestration -- stdlib only."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional


class AuthMethod(enum.Enum):
    """How we authenticate with a CLI provider."""

    CLI_OAUTH = "cli_oauth"  # Subscription-based, CLI binary handles auth
    API_KEY = "api_key"  # Environment variable API key
    NONE = "none"


class DelegationStrategy(enum.Enum):
    """Strategy for selecting which CLI to use."""

    AUTO = "auto"  # Smart selection based on task type
    FASTEST = "fastest"  # Typically shortest response time
    CHEAPEST = "cheapest"  # Prefer subscription CLI (free), then cheapest API
    STRONGEST = "strongest"  # Most capable model first
    ROUND_ROBIN = "round_robin"  # Rotate through available CLIs


@dataclass
class CLIStatus:
    """Installation and authentication status of a single CLI tool."""

    cli_id: str  # "claude-code" | "openai-codex" | "gemini-cli"
    binary_name: str  # "claude" | "codex" | "gemini"
    binary_path: Optional[str]  # Absolute path from shutil.which()
    installed: bool
    cli_authenticated: bool  # Headless probe succeeded
    api_key_name: Optional[str]  # Environment variable name (e.g. "ANTHROPIC_API_KEY")
    api_key_available: bool  # Whether the env var is set
    preferred_auth: AuthMethod  # CLI_OAUTH > API_KEY > NONE
    version: Optional[str] = None  # CLI version string

    def is_usable(self) -> bool:
        """Return True if this CLI can be used in any auth mode."""
        return self.preferred_auth != AuthMethod.NONE


@dataclass
class DelegationRequest:
    """Request to delegate a task to a CLI tool."""

    prompt: str
    preferred_cli: Optional[str] = None  # cli_id to prefer
    strategy: DelegationStrategy = DelegationStrategy.AUTO
    model: Optional[str] = None  # Model override
    working_dir: Optional[str] = None  # Working directory for CLI
    max_budget_usd: Optional[float] = None  # Budget limit (claude only)
    timeout: int = 120  # Execution timeout in seconds


@dataclass
class DelegationResult:
    """Result of a CLI delegation."""

    cli_id: str
    auth_method: AuthMethod
    success: bool
    output: str
    error: Optional[str] = None
    duration_ms: float = 0.0
    exit_code: int = 0
    model_used: Optional[str] = None
