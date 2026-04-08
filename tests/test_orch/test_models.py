"""Tests for orch/models.py — dataclass validation."""

from llm_relay.orch.models import (
    AuthMethod,
    CLIStatus,
    DelegationRequest,
    DelegationResult,
    DelegationStrategy,
)


def test_auth_method_values():
    assert AuthMethod.CLI_OAUTH.value == "cli_oauth"
    assert AuthMethod.API_KEY.value == "api_key"
    assert AuthMethod.NONE.value == "none"


def test_delegation_strategy_values():
    assert DelegationStrategy.AUTO.value == "auto"
    assert DelegationStrategy.FASTEST.value == "fastest"
    assert DelegationStrategy.CHEAPEST.value == "cheapest"
    assert DelegationStrategy.STRONGEST.value == "strongest"
    assert DelegationStrategy.ROUND_ROBIN.value == "round_robin"


def test_cli_status_usable_with_oauth():
    s = CLIStatus(
        cli_id="claude-code",
        binary_name="claude",
        binary_path="/usr/bin/claude",
        installed=True,
        cli_authenticated=True,
        api_key_name="ANTHROPIC_API_KEY",
        api_key_available=False,
        preferred_auth=AuthMethod.CLI_OAUTH,
    )
    assert s.is_usable() is True


def test_cli_status_usable_with_api_key():
    s = CLIStatus(
        cli_id="gemini-cli",
        binary_name="gemini",
        binary_path=None,
        installed=False,
        cli_authenticated=False,
        api_key_name="GEMINI_API_KEY",
        api_key_available=True,
        preferred_auth=AuthMethod.API_KEY,
    )
    assert s.is_usable() is True


def test_cli_status_not_usable():
    s = CLIStatus(
        cli_id="openai-codex",
        binary_name="codex",
        binary_path=None,
        installed=False,
        cli_authenticated=False,
        api_key_name="OPENAI_API_KEY",
        api_key_available=False,
        preferred_auth=AuthMethod.NONE,
    )
    assert s.is_usable() is False


def test_delegation_request_defaults():
    r = DelegationRequest(prompt="hello")
    assert r.prompt == "hello"
    assert r.preferred_cli is None
    assert r.strategy == DelegationStrategy.AUTO
    assert r.model is None
    assert r.timeout == 120


def test_delegation_result_defaults():
    r = DelegationResult(
        cli_id="claude-code",
        auth_method=AuthMethod.CLI_OAUTH,
        success=True,
        output="done",
    )
    assert r.error is None
    assert r.duration_ms == 0.0
    assert r.exit_code == 0
    assert r.model_used is None
