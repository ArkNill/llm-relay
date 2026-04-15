# Open-Network CLI Orchestration Architecture

> Multi-CLI orchestration layer for llm-relay.

## Overview

Most developers use subscription-based AI coding CLIs (Claude Code, Codex, Gemini CLI). llm-relay's orchestration layer provides unified management across all three, so no single provider becomes a single point of failure.

### Legal Verification

All 3 CLIs officially support headless mode. Subprocess invocation of official binaries is legitimate use.
- `claude -p` — official, confirmed in Anthropic docs
- `codex exec` — official, documented for CI/CD
- `gemini -p` — official, GitHub #24011 confirms wrapper is legitimate use

Prohibited: extracting OAuth tokens to call backend services directly.

## Module Structure

```
src/llm_relay/orch/          CLI orchestration (stdlib only)
├── models.py                CLIStatus, DelegationRequest, DelegationResult, AuthMethod, DelegationStrategy
├── discovery.py             shutil.which() + auth probe (cached)
├── executor.py              subprocess.run() wrapper (per-CLI cmd build + output parsing)
├── router.py                routing (strategy: auto/fastest/cheapest/strongest)
└── db.py                    delegations table (extends existing SQLite DB)

src/llm_relay/mcp/           FastMCP server (optional dep: mcp[cli])
├── __init__.py              run_server() entry point
├── __main__.py              python -m llm_relay.mcp
└── server.py                7 MCP tools

src/llm_relay/api/           HTTP API (starlette, reuses proxy dep)
└── routes.py                GET /api/v1/{cli/status, delegations, cost, health, sessions}

src/llm_relay/dashboard/     Static SPA (vanilla JS, no npm at runtime)
└── static/                  pre-built HTML/JS/CSS
```

## Core Interfaces

### CLI Detection (orch/discovery.py)

```python
_CLI_REGISTRY = [
    ("claude-code", "claude", "ANTHROPIC_API_KEY"),
    ("openai-codex", "codex", "OPENAI_API_KEY"),
    ("gemini-cli",  "gemini", "GEMINI_API_KEY"),
]

def discover_all() -> list[CLIStatus]        # full discovery (cached)
def refresh() -> list[CLIStatus]             # clear cache + re-discover
def get_available(require_auth=True)         # authenticated CLIs only
```

Detection: `shutil.which()` → binary exists → short headless probe for auth check.

### Subprocess Execution (orch/executor.py)

Per-CLI command build:
- `claude -p "{prompt}" --output-format=json [--model X] [--max-budget-usd Y]`
- `codex exec "{prompt}" --json --full-auto [--model X] [-C dir]`
- `gemini -p "{prompt}" --output-format=json -y [--model X]`

Output parsing: per-CLI JSON/JSONL format → DelegationResult.

### Routing (orch/router.py)

```python
def route(request: DelegationRequest) -> DelegationResult
```

Priority: (1) preferred_cli if specified (2) strategy-based selection (3) auth: OAuth CLI > API key > skip.

### MCP Tools

| Tool | Purpose | Read-only |
|------|---------|-----------|
| `cli_delegate(cli, prompt, ...)` | Delegate to specific CLI | No |
| `cli_status()` | Check install/auth status | Yes |
| `cli_probe(cli)` | Deep probe specific CLI | Yes |
| `orch_delegate(prompt, strategy)` | Smart routing delegation | No |
| `orch_history(limit)` | Delegation history | Yes |
| `relay_stats(window_hours)` | Token/cost statistics | Yes |
| `session_turns(session_id)` | Turn count for sessions | Yes |

### HTTP API

```
GET /api/v1/cli/status          — CLI detection status
GET /api/v1/delegations         — delegation history
GET /api/v1/delegations/stats   — aggregate statistics
GET /api/v1/sessions            — proxy session summaries
GET /api/v1/cost                — cost breakdown
GET /api/v1/health              — combined health check
```

## Auth Priority

```
1st: Subscription OAuth available → CLI binary call (no extra cost)
2nd: API Key available → REST/SDK call (pay-as-you-go)
3rd: Neither → skip provider
```

## Dependency Layers (no cycles)

```
Layer 0 (stdlib only): orch/, detect/, recover/, guard/, cost/, providers/
Layer 1 (httpx+starlette): proxy/, api/
Layer 2 (fastmcp): mcp/
Layer 3 (click+rich): CLI formatters
```

## Key Design Decisions

1. **Single DB file**: orchestration tables in existing `~/.llm-relay/usage.db`
2. **Sync MCP tools**: CLI execution is subprocess.run() (blocking)
3. **Prompt hashing**: no full prompt storage (hash + 200-char preview only)
4. **stdin=DEVNULL**: Codex exec waits on stdin without it — all subprocess calls must close stdin
5. **--skip-git-repo-check**: Codex requires git repo by default, orch adds this flag automatically
6. **UX**: `pip install llm-relay[all]` installs everything, no npm/docker needed
