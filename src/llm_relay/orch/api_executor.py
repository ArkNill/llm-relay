"""HTTP API delegation for providers without a CLI binary -- stdlib only.

Mirrors the shape of executor.py (subprocess CLI execution) but targets
HTTP-only providers. Initial provider: xAI Grok (chat-completions API).

Auth key is read from a file path (default ~/.llm-relay/<provider>.key) or
an environment variable, in that order. Key file content is the bearer
token literal.

Result shape matches DelegationResult so callers (MCP tool layer, DB
logger, history capture) work identically against CLI and API providers.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Optional

from llm_relay.orch.models import AuthMethod, DelegationResult

logger = logging.getLogger(__name__)


# ── Provider config ──────────────────────────────────────────────────────────
# Each provider knows its endpoint, default model, key resolution, and how to
# extract the assistant text from its response. Adding a new HTTP provider
# means adding an entry here plus optional response-extraction logic.


def _xai_key() -> Optional[str]:
    """Resolve the xAI/Grok API key. File first (XAI_API_KEY_PATH), then env."""
    path = os.environ.get(
        "XAI_API_KEY_PATH",
        os.path.expanduser("~/.llm-relay/grok.key"),
    )
    # Backward-compat: pre-existing ~/grok.key also accepted.
    if not os.path.isfile(path):
        legacy = os.path.expanduser("~/grok.key")
        if os.path.isfile(legacy):
            path = legacy
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip() or None
        except OSError:
            logger.debug("xAI key file %s exists but is not readable", path)
    env_key = os.environ.get("XAI_API_KEY", "").strip()
    return env_key or None


def _xai_extract(payload: dict) -> str:
    """Extract assistant text from an xAI chat-completions response."""
    try:
        return payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


PROVIDERS = {
    "grok": {
        "provider_id": "xai-grok",
        "endpoint": "https://api.x.ai/v1/chat/completions",
        "default_model": "grok-4.3",
        "key_resolver": _xai_key,
        "extract": _xai_extract,
        "auth_method": AuthMethod.API_KEY,
        "api_key_name": "XAI_API_KEY (or ~/.llm-relay/grok.key)",
    },
}


# ── Public surface ───────────────────────────────────────────────────────────


def list_api_providers() -> list[str]:
    """Short names of API-only providers wired up here."""
    return list(PROVIDERS.keys())


def api_provider_status(short_name: str) -> dict:
    """Probe-style status for an API provider. Mirrors cli_probe output shape."""
    cfg = PROVIDERS.get(short_name)
    if cfg is None:
        return {
            "error": "Unknown API provider: {!r}. Available: {}".format(
                short_name, list_api_providers()
            )
        }
    key = cfg["key_resolver"]()
    return {
        "provider_id": cfg["provider_id"],
        "kind": "http-api",
        "endpoint": cfg["endpoint"],
        "default_model": cfg["default_model"],
        "auth_method": cfg["auth_method"].value,
        "api_key_name": cfg["api_key_name"],
        "api_key_available": bool(key),
        "usable": bool(key),
    }


def execute_api(
    provider: str,
    prompt: str,
    *,
    model: Optional[str] = None,
    system: Optional[str] = None,
    timeout: int = 120,
    max_tokens: int = 4000,
    temperature: float = 0.3,
) -> DelegationResult:
    """Delegate a prompt to an HTTP-only provider.

    Returns a DelegationResult shaped identically to CLI execution so the
    surrounding MCP/DB/history machinery can treat both paths uniformly.
    """
    started = time.monotonic()
    cfg = PROVIDERS.get(provider)
    if cfg is None:
        return DelegationResult(
            cli_id="unknown-api",
            auth_method=AuthMethod.NONE,
            success=False,
            output="",
            error="Unknown API provider: {!r}".format(provider),
            duration_ms=0.0,
            exit_code=2,
        )

    provider_id = cfg["provider_id"]
    key = cfg["key_resolver"]()
    if not key:
        return DelegationResult(
            cli_id=provider_id,
            auth_method=AuthMethod.NONE,
            success=False,
            output="",
            error="No API key available for {}; checked {}".format(
                provider, cfg["api_key_name"]
            ),
            duration_ms=round((time.monotonic() - started) * 1000.0, 1),
            exit_code=1,
        )

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    body = {
        "model": model or cfg["default_model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(
        cfg["endpoint"],
        data=data,
        method="POST",
        headers={
            "Authorization": "Bearer {}".format(key),
            "Content-Type": "application/json",
            "User-Agent": "llm-relay/api-delegate (stdlib)",
        },
    )

    # Explicit TLS context so urllib uses the system trust store; corporate-
    # proxy users running through an outbound MITM will need NODE_EXTRA_CA-
    # equivalent setup via SSL_CERT_FILE, the same as CLI binaries do.
    ctx = ssl.create_default_context()

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body_excerpt = ""
        try:
            body_excerpt = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        return DelegationResult(
            cli_id=provider_id,
            auth_method=cfg["auth_method"],
            success=False,
            output="",
            error="HTTP {} from {}: {}".format(e.code, cfg["endpoint"], body_excerpt or e.reason),
            duration_ms=round((time.monotonic() - started) * 1000.0, 1),
            exit_code=e.code,
            model_used=body["model"],
        )
    except urllib.error.URLError as e:
        return DelegationResult(
            cli_id=provider_id,
            auth_method=cfg["auth_method"],
            success=False,
            output="",
            error="Transport error to {}: {}".format(cfg["endpoint"], e.reason),
            duration_ms=round((time.monotonic() - started) * 1000.0, 1),
            exit_code=1,
            model_used=body["model"],
        )
    except Exception as e:
        return DelegationResult(
            cli_id=provider_id,
            auth_method=cfg["auth_method"],
            success=False,
            output="",
            error="Unhandled exception: {}".format(e),
            duration_ms=round((time.monotonic() - started) * 1000.0, 1),
            exit_code=1,
            model_used=body["model"],
        )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        return DelegationResult(
            cli_id=provider_id,
            auth_method=cfg["auth_method"],
            success=False,
            output="",
            error="Provider returned non-JSON (HTTP {}): {}: {}".format(status, e, raw[:200]),
            duration_ms=round((time.monotonic() - started) * 1000.0, 1),
            exit_code=status,
            model_used=body["model"],
        )

    output = cfg["extract"](payload)
    duration_ms = round((time.monotonic() - started) * 1000.0, 1)

    if not output:
        return DelegationResult(
            cli_id=provider_id,
            auth_method=cfg["auth_method"],
            success=False,
            output="",
            error="Provider returned no assistant text. Raw payload keys: {}".format(
                sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__
            ),
            duration_ms=duration_ms,
            exit_code=status,
            model_used=body["model"],
        )

    return DelegationResult(
        cli_id=provider_id,
        auth_method=cfg["auth_method"],
        success=True,
        output=output,
        error=None,
        duration_ms=duration_ms,
        exit_code=status,
        model_used=body["model"],
    )
