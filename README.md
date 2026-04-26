# llm-relay

Unified LLM usage management — API proxy, session diagnostics, multi-CLI orchestration.

[한국어](README.ko.md) | [llms.txt](llms.txt)

## Why

This project started from a need to escape deep vendor lock-in with a single AI coding tool. After [investigating hidden behaviors in Claude Code](https://github.com/ArkNill/claude-code-hidden-problem-analysis) — silent token inflation, false rate limits, context stripping, and opaque feature flags — it became clear that relying on one vendor's black box was a risk. llm-relay was built to take back visibility and control: monitor what's actually happening, diagnose problems independently, and orchestrate across multiple CLI tools (Claude Code, Codex, Gemini) so no single provider becomes a single point of failure.

## Features

- **Proxy**: Transparent API proxy with cache/token monitoring and 12-strategy pruning
- **Detect**: 7 detectors (orphan, stuck, synthetic, bloat, cache, resume, microcompact)
- **Recover**: Session recovery and doctor (7 health checks)
- **Guard**: 4-tier threshold daemon with dual-zone classification
- **Cost**: Per-1% cost calculation and rate-limit header analysis
- **Orch**: Multi-CLI orchestration (Claude Code, Codex CLI, Gemini CLI)
- **Display**: Multi-CLI session monitor with context composition pie chart, connection type badges (SSH/tmux/tailscale/mosh), and provider liveness detection
- **History**: Proxy-level conversation capture with delta/full storage, compaction detection, and web replay viewer
- **Composition**: Real-time context window analysis — classifies content into 6 categories (user/assistant/tool_use/tool_result/thinking/system) with SNR metrics and duplicate read tracking
- **TUI**: `llm-relay top` — btop-style terminal monitor with Rich Live (works over SSH, no browser needed)
- **i18n**: Browser locale detection with en/ko support; server-side override via `LLM_RELAY_LANG`
- **MCP**: 8 tools via stdio transport (cli_delegate, cli_status, cli_probe, orch_delegate, orch_history, relay_stats, session_turns, session_history)

## Install

```bash
# CLI only (diagnostics, recovery, orchestration)
pip install llm-relay

# With Rich TUI (llm-relay top)
pip install llm-relay[cli]

# With proxy + web dashboard
pip install llm-relay[proxy]

# With MCP server (Python 3.10+)
pip install llm-relay[mcp]

# Everything
pip install llm-relay[all]
```

## Quick Start

### One-command setup (recommended)

```bash
pip install llm-relay[all]
llm-relay init
```

This single command:
1. Detects installed CLIs (Claude Code, Codex, Gemini)
2. Initializes the database (`~/.llm-relay/usage.db`)
3. Configures Claude Code to route through the proxy (`ANTHROPIC_BASE_URL`)
4. Registers the MCP server in Claude Code (8 tools)
5. Starts the proxy server with history enabled
6. Runs a health check to verify everything works

After init, open: **http://localhost:8083/dashboard/**

Options: `--dry-run` (preview without changes), `--skip-server` (configure only), `--port 9090` (custom port).

### Manual setup

```bash
# CLI diagnostics only (no server needed)
pip install llm-relay
llm-relay scan              # Session health check (7 detectors)
llm-relay doctor            # Configuration health check (7 checks)
llm-relay top               # Live terminal monitor (btop-style TUI)

# Web dashboard
pip install llm-relay[proxy]
llm-relay serve             # Starts proxy + dashboard on port 8083

# Then configure Claude Code to use the proxy:
# In ~/.claude/settings.json, add:
#   "env": { "ANTHROPIC_BASE_URL": "http://localhost:8083" }
```

Web pages:
- `/dashboard/` — CLI status, cost, quota, error rate, cache hit rate, Turn Monitor
- `/display/` — Turn counter with context composition, connection type badges
- `/history/` — Session conversation replay with compaction timeline

### MCP server

```bash
llm-relay-mcp               # stdio transport, 8 tools
```

## CLI Status

| CLI | Status |
|-----|--------|
| Claude Code | Fully supported |
| OpenAI Codex | Fully supported |
| Gemini CLI | Display supported, oauth-personal has known 403 server-side bug ([#25425](https://github.com/google-gemini/gemini-cli/issues/25425)) |

## Requirements

- Python >= 3.9
- MCP tools require Python >= 3.10

## License

MIT

## Ecosystem

Part of the [QuartzUnit](https://github.com/QuartzUnit) open-source ecosystem.
