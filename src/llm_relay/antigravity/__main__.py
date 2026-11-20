"""Run the Antigravity CLI relay: ``python -m llm_relay.antigravity``.

Listens on ``LLM_RELAY_AGY_HOST`` / ``LLM_RELAY_AGY_PORT`` (default
``127.0.0.1:8086``) -- its own process and port, apart from the Claude Code
relay. Point agy at it with ``CLOUD_CODE_URL=http://127.0.0.1:8086``.
"""

from __future__ import annotations

import os

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8086


def main() -> None:
    import uvicorn

    host = os.environ.get("LLM_RELAY_AGY_HOST") or DEFAULT_HOST
    port = int(os.environ.get("LLM_RELAY_AGY_PORT") or DEFAULT_PORT)
    uvicorn.run(
        "llm_relay.antigravity.app:app",
        host=host,
        port=port,
        # The upstream's own Date / Server headers are relayed; uvicorn adding its own would duplicate them.
        server_header=False,
        date_header=False,
    )


if __name__ == "__main__":
    main()
