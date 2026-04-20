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
- **Display**: Multi-CLI session monitor with provider badges and liveness detection
- **MCP**: 7 tools via stdio transport (cli_delegate, cli_status, cli_probe, orch_delegate, orch_history, relay_stats, session_turns)

## Install

```bash
# CLI only (diagnostics, recovery, orchestration)
pip install llm-relay

# With proxy + web dashboard
pip install llm-relay[proxy]

# With MCP server (Python 3.10+)
pip install llm-relay[mcp]

# Everything
pip install llm-relay[all]
```

## Quick Start

### CLI diagnostics (no server needed)

```bash
llm-relay scan              # Session health check (7 detectors)
llm-relay doctor            # Configuration health check (7 checks)
llm-relay recover           # Extract session context for resumption
```

### Web dashboard

```bash
# Option 1: Direct
pip install llm-relay[proxy]
uvicorn llm_relay.proxy.proxy:app --host 0.0.0.0 --port 8083

# Option 2: Docker
cp .env.example .env        # Edit as needed
docker compose up -d
```

Then open:
- `/dashboard/` — CLI status, cost, delegation history, Turn Monitor (alive sessions only; `?include_dead=1` to bypass)
- `/display/` — Turn counter with CC/Codex/Gemini session cards (alive filter: CC via cc_pid+TTY fallback, Codex/Gemini via fd-open)

### MCP server

```bash
llm-relay-mcp               # stdio transport, 7 tools
```

### API proxy for Claude Code

```bash
# Set in Claude Code
export ANTHROPIC_BASE_URL=http://localhost:8080
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
