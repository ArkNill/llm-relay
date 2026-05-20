# llm-relay

Unified LLM usage management — API proxy, session diagnostics, multi-CLI orchestration.

[한국어](README.ko.md) | [llms.txt](llms.txt)

## Features

- **Proxy**: Transparent API proxy with cache/token monitoring and 12-strategy pruning
- **Detect**: 7 detectors (orphan, stuck, bloat, synthetic, cache, resume, microcompact)
- **Recover**: Session recovery and doctor (7 health checks)
- **Guard**: 4-tier threshold daemon with dual-zone classification
- **Cost**: Per-1% cost calculation and rate-limit header analysis
- **Orch**: Multi-CLI orchestration (Claude Code, Codex CLI, Gemini CLI)
- **Display**: Multi-CLI session monitor with provider badges and liveness detection
- **I18n**: Multi-language support (English, Korean) with browser auto-detection and `LLM_RELAY_LANG` env
- **MCP**: 8 tools via stdio transport (cli_delegate, cli_status, cli_probe, orch_delegate, orch_history, relay_stats, session_turns, session_history)

## Install

### 1. Set up Python environment

<details>
<summary><b>Windows (pip)</b></summary>

```cmd
python -m venv .venv
.venv\Scripts\activate
```
</details>

<details>
<summary><b>Windows (conda)</b></summary>

```cmd
conda create -n llm-relay python=3.12
conda activate llm-relay
```
</details>

<details>
<summary><b>Linux / macOS (pip)</b></summary>

```bash
python3 -m venv .venv
source .venv/bin/activate
```
</details>

### 2. Install llm-relay

```bash
# Default (SQLite, zero-config)
pip install llm-relay

# With proxy + web dashboard
pip install llm-relay[proxy]

# With PostgreSQL support (long-term analytics + vector search)
pip install llm-relay[pg]

# With MCP server (Python 3.10+)
pip install llm-relay[mcp]

# Everything
pip install llm-relay[all]
```

### 3. Choose database

| | SQLite (default) | PostgreSQL |
|---|---|---|
| Setup | Zero-config | Requires PG server |
| Best for | Getting started, light usage | Long-term data analytics, vector search |
| Install | `pip install llm-relay` | `pip install llm-relay[pg]` |
| Config | (none needed) | `LLM_RELAY_DB=postgresql://user:pass@host/db` |

### 4. Initialize

```bash
llm-relay init
```

## Quick Start

### One-command setup

```bash
llm-relay init              # Auto-detect CLIs, configure proxy, start server
```

### CLI commands

```bash
llm-relay scan              # Session health check (7 detectors)
llm-relay doctor            # Configuration health check (7 checks)
llm-relay recover           # Extract session context for resumption
llm-relay serve             # Start proxy server + web dashboard
llm-relay top               # Live terminal monitor (btop-style)
llm-relay service install   # Windows: background service + auto-start (no console window)
llm-relay service stop      # Windows: stop background service
llm-relay service uninstall # Windows: remove service + cleanup
```

### Web dashboard

```bash
# Native (Linux/macOS/Windows)
llm-relay serve --port 8080
```

Then open:
- `/dashboard/` — CLI status, cost, delegation history, Turn Monitor (alive sessions only; `?include_dead=1` to bypass)
- `/display/` — Turn counter with CC/Codex/Gemini session cards (alive filter: CC via cc_pid+TTY fallback, Codex/Gemini via fd-open; Windows uses mtime+process detection)
- `/history/` — Session conversation history browser

### MCP server

```bash
llm-relay-mcp               # stdio transport, 8 tools
```

### API proxy for Claude Code

```bash
# Set in Claude Code
llm-relay connect   # Auto-configures Claude Code proxy
```

### Agent-driven setup

If you would rather have your existing coding agent (Claude Code, Codex,
Gemini) run the install for you, point it at
[`docs/AGENT_SETUP.md`](docs/AGENT_SETUP.md). It is a structured playbook
the agent follows step by step, using `llm-relay env-fingerprint` and
`llm-relay verify` to probe and check each step without scraping output.

```bash
llm-relay env-fingerprint --format json        # state snapshot
llm-relay verify install --format json         # is the package usable?
llm-relay verify config --format json          # is local state set up?
llm-relay verify integration --cli claude-code # is the CLI wired?
llm-relay verify all                            # everything at once
```

Exit code is 0 on pass/warn, 1 on fail.

## CLI Status

| CLI | Status |
|-----|--------|
| Claude Code | Fully supported |
| OpenAI Codex | Fully supported |
| Gemini CLI | Display supported, oauth-personal has known 403 server-side bug ([#25425](https://github.com/google-gemini/gemini-cli/issues/25425)) |

## Platform Support

| Platform | Mode | Notes |
|----------|------|-------|
| Linux | Native | Full feature set, systemd recommended |
| macOS | Native | Full feature set |
| Windows | Native | `llm-relay service install` for background daemon (no console window) |

## Requirements

- Python >= 3.9
- MCP tools require Python >= 3.10

## License

MIT

## Ecosystem

Part of the [QuartzUnit](https://github.com/QuartzUnit) open-source ecosystem.
