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
- **Monitoring**: Quota utilization (Q5h/Q7d), cache hit rate, error rate (2xx/4xx/5xx/429), TTL tier detection (1h/5m) — all surfaced from data already collected by the proxy
- **TUI**: `llm-relay top` — btop-style terminal monitor with Rich Live (works over SSH, no browser needed)
- **i18n**: Browser locale detection with en/ko support; server-side override via `LLM_RELAY_LANG`
- **MCP**: 8 tools via stdio transport (cli_delegate, cli_status, cli_probe, orch_delegate, orch_history, relay_stats, session_turns, session_history)

## Quick Start (Docker — recommended)

Runs on **Linux, macOS, and Windows** with Docker. No Python or pip required on the host.

```bash
# 1. Download docker-compose.yml
curl -sL https://raw.githubusercontent.com/ArkNill/llm-relay/main/docker-compose.yml -o docker-compose.yml

# 2. Start the proxy
docker compose up -d

# 3. Open the dashboard
#    http://localhost:8083/dashboard/
```

To route Claude Code through the proxy, add to `~/.claude/settings.json`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:8083"
  }
}
```

Web pages:
- `/dashboard/` — CLI status, cost, quota, error rate, cache hit rate, Turn Monitor
- `/display/` — Turn counter with context composition, connection type badges
- `/history/` — Session conversation replay with compaction timeline

### Clean removal

```bash
docker compose down -v    # Stops container + removes data volume
rm docker-compose.yml     # Remove compose file
```

No files are left on the host. To stop routing Claude Code, remove `ANTHROPIC_BASE_URL` from `~/.claude/settings.json`.

## Install (CLI only — optional)

For lightweight diagnostics without the proxy server:

```bash
pip install llm-relay          # Core diagnostics
pip install llm-relay[cli]     # With Rich TUI (llm-relay top)
pip install llm-relay[mcp]     # MCP server (Python 3.10+)
```

```bash
llm-relay scan                 # Session health check (7 detectors)
llm-relay doctor               # Configuration health check (7 checks)
llm-relay top                  # Live terminal monitor (btop-style TUI)
llm-relay init                 # Check Docker status + setup guide
```

### MCP server

```bash
llm-relay-mcp                  # stdio transport, 8 tools
```

## API Endpoints

All endpoints are served by the proxy at `http://localhost:8083/api/v1/`.

| Endpoint | Description |
|----------|-------------|
| `GET /api/v1/turns` | Turn counts + token metrics + zone classification for active sessions |
| `GET /api/v1/turns/{session_id}` | Per-session metrics with cache hit rate and TTL tier |
| `GET /api/v1/display` | Session cards with prompts, terminal info, composition |
| `GET /api/v1/quota` | Anthropic Q5h/Q7d quota utilization and overage status |
| `GET /api/v1/errors` | Error rate breakdown (2xx/4xx/5xx/429) |
| `GET /api/v1/cache` | Cache hit rate (global or per-session) |
| `GET /api/v1/ttl` | Cache TTL tier detection (1h/5m/mixed) |
| `GET /api/v1/health` | CLI + proxy + orchestration DB health |
| `GET /api/v1/cost` | Cost breakdown by model |
| `GET /api/v1/sessions` | Proxy session summaries |
| `GET /api/v1/cli/status` | CLI installation and auth status |
| `GET /api/v1/delegations` | Multi-CLI delegation history |
| `GET /api/v1/delegations/stats` | Delegation aggregate statistics |
| `GET /api/v1/history` | Sessions with conversation history |
| `GET /api/v1/history/{session_id}` | Conversation turns for a session |
| `GET /api/v1/history/{session_id}/compactions` | Compaction events |
| `GET /api/v1/history/{session_id}/composition` | Per-turn context composition |
| `GET /api/v1/i18n` | Locale-specific UI messages |

## CLI Status

| CLI | Status |
|-----|--------|
| Claude Code | Fully supported |
| OpenAI Codex | Fully supported |
| Gemini CLI | Display supported, oauth-personal has known 403 server-side bug ([#25425](https://github.com/google-gemini/gemini-cli/issues/25425)) |

## Development

For local development without GHCR image:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

## Requirements

- Docker (recommended) — Linux, macOS, or Windows with Docker Desktop
- Python >= 3.9 (CLI diagnostics only)
- MCP tools require Python >= 3.10

## License

MIT

## Ecosystem

Part of the [QuartzUnit](https://github.com/QuartzUnit) open-source ecosystem.
