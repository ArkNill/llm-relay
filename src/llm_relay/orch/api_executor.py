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


def _read_key_file_if_safe(path: str) -> Optional[str]:
    """Read a key file iff its mode is private (owner-only access).

    On POSIX systems, key files MUST NOT be readable by group or other
    (mode bits 0o077 must be zero). A key file with looser permissions
    is silently skipped with a debug log — the caller then falls through
    to the env-var path. On Windows, where st_mode bits don't carry the
    same meaning, mode checking is skipped.

    Returns the stripped key contents, or None if the file is missing,
    has insecure permissions, or is unreadable.
    """
    if not os.path.isfile(path):
        return None
    if os.name == "posix":
        try:
            mode = os.stat(path).st_mode & 0o777
            if mode & 0o077:
                logger.debug(
                    "xAI key file %s has insecure mode %o (must be 0600 or stricter); skipping",
                    path, mode,
                )
                return None
        except OSError:
            logger.debug("xAI key file %s could not be stat'd; skipping", path)
            return None
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        logger.debug("xAI key file %s exists but is not readable", path)
        return None


def _xai_key() -> Optional[str]:
    """Resolve the xAI/Grok API key. File first (XAI_API_KEY_PATH), then env.

    Key files are accepted only when their POSIX mode is 0600 or stricter
    (see _read_key_file_if_safe). Loose-permission files are silently
    skipped so a misconfigured key never leaks into outbound requests.
    """
    path = os.environ.get(
        "XAI_API_KEY_PATH",
        os.path.expanduser("~/.llm-relay/grok.key"),
    )
    key = _read_key_file_if_safe(path)
    if key:
        return key
    # Backward-compat: pre-existing ~/grok.key also accepted (same perm check).
    legacy = os.path.expanduser("~/grok.key")
    if legacy != path:
        key = _read_key_file_if_safe(legacy)
        if key:
            return key
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

    # Endpoint scheme guard: bearer-token-bearing requests MUST go over HTTPS.
    # The PROVIDERS dict is in-tree so a misconfigured http:// endpoint here
    # would be a code bug, not external input — but a one-line guard prevents
    # the worst-case credential leak if someone ever adds or edits an entry
    # without noticing.
    endpoint = cfg["endpoint"]
    if not endpoint.startswith("https://"):
        return DelegationResult(
            cli_id=provider_id,
            auth_method=AuthMethod.NONE,
            success=False,
            output="",
            error="Refusing non-HTTPS endpoint for bearer-token request: {}".format(endpoint),
            duration_ms=round((time.monotonic() - started) * 1000.0, 1),
            exit_code=2,
        )

    # Provider-supplied hook; defensively wrap in case a future provider's
    # key_resolver raises (e.g., environment lookup that misuses the API).
    try:
        key = cfg["key_resolver"]()
    except Exception as e:
        return DelegationResult(
            cli_id=provider_id,
            auth_method=AuthMethod.NONE,
            success=False,
            output="",
            error="API key resolution failed for {}: {}".format(provider, e),
            duration_ms=round((time.monotonic() - started) * 1000.0, 1),
            exit_code=1,
        )
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

    # Capture model_used early so all exception branches can reference it
    # without depending on body[] still being well-formed at exception time.
    model_used = model or cfg["default_model"]
    body = {
        "model": model_used,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(
        endpoint,
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
            content_type = resp.headers.get("Content-Type", "")
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
            error="HTTP {} from {}: {}".format(e.code, endpoint, body_excerpt or e.reason),
            duration_ms=round((time.monotonic() - started) * 1000.0, 1),
            exit_code=e.code,
            model_used=model_used,
        )
    except urllib.error.URLError as e:
        return DelegationResult(
            cli_id=provider_id,
            auth_method=cfg["auth_method"],
            success=False,
            output="",
            error="Transport error to {}: {}".format(endpoint, e.reason),
            duration_ms=round((time.monotonic() - started) * 1000.0, 1),
            exit_code=1,
            model_used=model_used,
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
            model_used=model_used,
        )

    # Sentinel for parse / extract failures that arrive over a 2xx HTTP status —
    # we can't reuse `status` (200) as exit_code because callers treating
    # non-zero-as-failure would then see a payload error as success.
    PAYLOAD_FAILURE_EXIT = 502  # "bad gateway" — upstream returned an unusable body

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        return DelegationResult(
            cli_id=provider_id,
            auth_method=cfg["auth_method"],
            success=False,
            output="",
            error="Provider returned non-JSON (HTTP {}, Content-Type {!r}): {}: {}".format(
                status, content_type, e, raw[:200]
            ),
            duration_ms=round((time.monotonic() - started) * 1000.0, 1),
            exit_code=PAYLOAD_FAILURE_EXIT,
            model_used=model_used,
        )

    # Provider-supplied hook; defensively wrap so a misbehaving extract()
    # returns a clean DelegationResult instead of bubbling an exception.
    try:
        output = cfg["extract"](payload)
    except Exception as e:
        return DelegationResult(
            cli_id=provider_id,
            auth_method=cfg["auth_method"],
            success=False,
            output="",
            error="Provider response extraction failed: {}".format(e),
            duration_ms=round((time.monotonic() - started) * 1000.0, 1),
            exit_code=PAYLOAD_FAILURE_EXIT,
            model_used=model_used,
        )
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
            exit_code=PAYLOAD_FAILURE_EXIT,
            model_used=model_used,
        )

    # Success: exit_code follows the CLI-executor convention (0 == success);
    # the HTTP status code is implicit (any 2xx that reached this branch is
    # a successful round-trip). proxy/composition.py and other consumers
    # rely on exit_code == 0 to flag success.
    return DelegationResult(
        cli_id=provider_id,
        auth_method=cfg["auth_method"],
        success=True,
        output=output,
        error=None,
        duration_ms=duration_ms,
        exit_code=0,
        model_used=model_used,
    )
