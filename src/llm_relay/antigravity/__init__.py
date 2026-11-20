"""Antigravity CLI (agy) traffic relay -- isolated from the Claude Code proxy path.

A separate process (see ``llm_relay.antigravity.app``) forwards agy's Code Assist
backend calls (``/v1internal:*``) to Google and records requests, conversation
turns and quota snapshots in dedicated ``provider_*`` tables. Nothing in this
package writes to the tables the Claude Code proxy owns (``requests``,
``conversation_turns``), and ``llm_relay.proxy.proxy`` does not import it.
"""

PROVIDER = "antigravity-cli"
