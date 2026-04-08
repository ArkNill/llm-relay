# Open-Network CLI Orchestration Architecture

> 2026-04-07 설계 확정. llm-relay open-network 확장 레이어.
> related docs: `design-opennet-cli-orchestration.md`, `design-strategy.md`

## Context

multi-CLI는 local-network(local-GPU + 로컬 LLM) 기반 설계. 대부분의 개발자는 open-network 구독 서비스(CC Pro, ChatGPT Pro, Gemini Pro) 사용.
이 갭을 메우기 위해 llm-relay에 open-network 확장 레이어를 추가하여, 월정액 구독자가 3개 CLI를 통합 관리할 수 있게 함.

### 3 Scenarios

| # | Name | Main | Inference | CLI Role | MA Required |
|---|------|------|-----------|----------|-------------|
| 1 | CC Main | Claude Code | CC itself | Codex/Gemini = auxiliary | No |
| 2 | MA + CLI | multi-CLI | Gemini/OpenAI API (user choice) | 3 CLIs = MCP tools | Yes |
| 3 | MA + local-GPU + CLI | multi-CLI | local inference | 3 CLIs = delegation | Yes |

### Legal Verification (2026-04-07)

All 3 CLIs officially support headless mode. Subprocess invocation of official binaries = legal.
- `claude -p` — official, confirmed in Anthropic Legal & Compliance docs
- `codex exec` — official, documented for CI/CD
- `gemini -p` — official, GitHub #24011 confirms wrapper is legitimate use

Prohibited: extracting OAuth tokens to call backend services directly.

## Directory Structure

```
src/llm_relay/
├── (existing: detect/, providers/, proxy/, recover/, guard/, cost/, strategies/, formatters/)
│
├── orch/                    [NEW] CLI orchestration (stdlib only)
│   ├── __init__.py
│   ├── models.py            — CLIStatus, DelegationRequest, DelegationResult, AuthMethod, DelegationStrategy
│   ├── discovery.py         — shutil.which() + auth probe (cached)
│   ├── executor.py          — subprocess.run() wrapper (per-CLI cmd build + output parsing)
│   ├── router.py            — routing (strategy: auto/fastest/cheapest/strongest)
│   └── db.py                — delegations table (extends existing SQLite DB)
│
├── mcp/                     [NEW] FastMCP server (optional dep: mcp[cli])
│   ├── __init__.py          — run_server() entry point
│   ├── __main__.py          — python -m llm_relay.mcp
│   └── server.py            — 6 MCP tools
│
├── api/                     [NEW] HTTP API (starlette, reuses proxy dep)
│   ├── __init__.py
│   └── routes.py            — GET /api/v1/{cli/status, delegations, cost, health, sessions}
│
└── dashboard/               [NEW] Static SPA
    ├── __init__.py          — get_static_dir() helper
    └── static/              — pre-built HTML/JS/CSS (no npm at runtime)
```

## Core Interfaces

### orch/models.py (stdlib only)

```python
class AuthMethod(enum.Enum):
    CLI_OAUTH = "cli_oauth"     # Subscription CLI binary call
    API_KEY = "api_key"         # Environment variable API key
    NONE = "none"

class DelegationStrategy(enum.Enum):
    AUTO = "auto"
    FASTEST = "fastest"
    CHEAPEST = "cheapest"
    STRONGEST = "strongest"

@dataclass
class CLIStatus:
    cli_id: str                 # "claude-code" | "openai-codex" | "gemini-cli"
    binary_name: str            # "claude" | "codex" | "gemini"
    binary_path: Optional[str]  # shutil.which() result
    installed: bool
    cli_authenticated: bool     # headless probe succeeded
    api_key_available: bool     # env var exists
    preferred_auth: AuthMethod  # CLI > API > NONE

@dataclass
class DelegationRequest:
    prompt: str
    preferred_cli: Optional[str] = None
    strategy: DelegationStrategy = DelegationStrategy.AUTO
    model: Optional[str] = None
    working_dir: Optional[str] = None
    timeout: int = 120

@dataclass
class DelegationResult:
    cli_id: str
    auth_method: AuthMethod
    success: bool
    output: str
    error: Optional[str] = None
    duration_ms: float = 0.0
    exit_code: int = 0
```

### orch/discovery.py — CLI Detection

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

### orch/executor.py — Subprocess Execution

Per-CLI command build:
- `claude -p "{prompt}" --output-format=json [--model X] [--max-budget-usd Y]`
- `codex exec "{prompt}" --json --full-auto [--model X] [-C dir]`
- `gemini -p "{prompt}" --output-format=json -y [--model X]`

Output parsing: per-CLI JSON/JSONL format → DelegationResult.

### orch/router.py — Routing

```python
def route(request: DelegationRequest) -> DelegationResult
```

Priority: (1) preferred_cli if specified (2) strategy-based selection (3) auth: OAuth CLI > API key > skip.

### orch/db.py — DB Extension

Adds table to existing proxy DB (`~/.llm-relay/usage.db`):

```sql
CREATE TABLE IF NOT EXISTS delegations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    cli_id TEXT NOT NULL,
    auth_method TEXT,
    prompt_hash TEXT,
    prompt_preview TEXT,
    model TEXT,
    working_dir TEXT,
    success INTEGER DEFAULT 0,
    exit_code INTEGER DEFAULT 0,
    duration_ms REAL DEFAULT 0.0,
    output_chars INTEGER DEFAULT 0,
    error TEXT,
    strategy TEXT
);
CREATE INDEX IF NOT EXISTS idx_deleg_ts ON delegations(ts);
CREATE INDEX IF NOT EXISTS idx_deleg_cli ON delegations(cli_id);
```

### mcp/server.py — 6 MCP Tools

| Tool | Purpose | Read-only |
|------|---------|-----------|
| `cli_delegate(cli, prompt, ...)` | Delegate to specific CLI | No |
| `cli_status()` | Check install/auth status | Yes |
| `cli_probe(cli)` | Deep probe specific CLI | Yes |
| `orch_delegate(prompt, strategy)` | Smart routing delegation | No |
| `orch_history(limit)` | Delegation history | Yes |
| `relay_stats(window_hours)` | Token/cost statistics | Yes |

Entry: `llm-relay-mcp` (pyproject.toml scripts) → stdio transport.

### api/routes.py — HTTP API

```
GET /api/v1/cli/status          — CLI detection status
GET /api/v1/delegations         — delegation history
GET /api/v1/delegations/stats   — aggregate statistics
GET /api/v1/sessions            — proxy session summaries
GET /api/v1/cost                — cost breakdown
GET /api/v1/health              — combined health check
```

### proxy/proxy.py — Unified Starlette App

Mount API + dashboard routes BEFORE catch-all proxy:

```python
routes = [
    Route("/_health", ...), Route("/_stats", ...), Route("/_recent", ...),
    *get_api_routes(),                                          # NEW
    Mount("/dashboard", StaticFiles(directory=static_dir)),     # NEW
    Route("/{path:path}", _proxy, ...),                        # catch-all LAST
]
```

## Data Flow

### Scenario 1: CC Main (lightweight)

```
User → Claude Code session
  ├─ codex-plugin-cc (installed) → Codex slash command
  ├─ MCP tool: cli_delegate("gemini", ...) → llm-relay-mcp (stdio)
  │     └─ orch/executor → gemini -p "..." → result
  ├─ HTTP proxy (localhost:8082) → Anthropic API → DB logging
  └─ Dashboard: http://localhost:8082/dashboard/
```

### Scenario 2: MA + CLI only

```
User → MA → inference_provider (MIRROR_INFERENCE_PROVIDER=gemini|openai)
  └─ react loop → MCP tool dispatch
       └─ relay_cli_delegate → llm-relay-mcp (mcp_servers.yaml)
            └─ orch/router → best CLI → subprocess execution
```

MA inference.yaml already has gemini/openai profiles (line 42-58) — no changes needed.

### Scenario 3: MA + local-GPU + CLI

Same as scenario 2. local inference = main inference, CLIs = auxiliary/delegation.

## Dependency Layers (no cycles)

```
Layer 0 (stdlib only): orch/, detect/, recover/, guard/, cost/, providers/
Layer 1 (httpx+starlette): proxy/, api/
Layer 2 (fastmcp): mcp/
Layer 3 (click+rich): CLI formatters
```

## Auth Priority (llm-relay orch)

```
1st: Subscription OAuth available → CLI binary call (no extra cost)
2nd: API Key available → REST/SDK call (pay-as-you-go)
3rd: Neither → skip provider
```

## pyproject.toml Changes

```toml
[project.optional-dependencies]
proxy = ["httpx>=0.28", "uvicorn>=0.34", "starlette>=0.46"]
mcp = ["mcp[cli]>=1.0"]                        # NEW
cli = ["click>=8.0", "rich>=13.0"]
all = [proxy + mcp + cli]                       # NEW: includes mcp

[project.scripts]
llm-relay = "llm_relay.detect.__main__:main"
llm-relay-mcp = "llm_relay.mcp:run_server"      # NEW
```

## MA Integration (Scenario 2, 3)

Add to `~/agent/config/mcp_servers.yaml`:

```yaml
servers:
  llm-relay:
    transport: stdio
    command: llm-relay-mcp
    enabled: true
    timeout: 180
    tier: 2
    namespace: relay
```

## Implementation Phases

1. **orch module** (stdlib only) — models, discovery, executor, db, router, tests ✅
2. **MCP server** (fastmcp) — 6 tools, pyproject.toml, tests ✅
3. **API endpoints** (starlette) — 6 GET routes, tests ✅
4. **Dashboard SPA** — static HTML+JS+CSS bundle ✅
5. **Unified serve** — proxy.py Mount integration ✅
6. **MA integration** — mcp_servers.yaml + CC MCP add ✅
7. **Scenario 1 E2E** — cli_status + Gemini delegate + Codex delegate + dashboard ✅

## Key Design Decisions

1. **Single DB file**: orchestration tables in existing `~/.llm-relay/usage.db`
2. **Sync MCP tools**: CLI execution is subprocess.run() (blocking)
3. **Prompt hashing**: no full prompt storage (hash + 200-char preview only)
4. **Catch-all proxy last**: existing pattern preserved (proxy.py:449)
5. **MA inference.yaml unchanged**: gemini/openai profiles already exist (line 42-58)
6. **UX**: `pip install llm-relay[all]` installs everything, no npm/docker needed
7. **stdin=DEVNULL**: Codex exec waits on stdin without it — all subprocess calls must close stdin
8. **--skip-git-repo-check**: Codex requires git repo by default, orch adds this flag automatically
9. **workstation direct** (not Docker): CLI binaries + OAuth are on host, orch/mcp/api run direct. Proxy stays in Docker (8082)

## Bugs Found During Verification

| Bug | Root Cause | Fix |
|-----|-----------|-----|
| Codex 120s timeout | `codex exec` waits on stdin for additional input | `stdin=subprocess.DEVNULL` in executor.py |
| Codex git repo required | Codex refuses to run outside git repos | `--skip-git-repo-check` flag in _build_codex_cmd |
| cli_delegate no DB logging | mcp/server.py called executor directly, bypassed router's log_delegation | Added log_delegation call in cli_delegate tool |
| MA _get_tiers() hardcoded | local-GPU+Desktop fixed, ignoring inference_provider.py config | Refactored to config-based dynamic generation |
| Gemini API frequency_penalty | Gemini OpenAI endpoint rejects frequency_penalty | Cloud tiers (is_local=false) exclude penalty params |

## MA Inference Refactoring (2026-04-07)

Scenario 2 blocker: `inference.py:_get_tiers()` hardcoded local-GPU+Desktop tiers, ignoring `inference_provider.py` config.

**Changes:**
- `inference_provider.py`: TierConfig extended with model, timeout, is_local, max_tokens_cap, extra_sampling
- `health.py`: Added `check_tier_health()`, `mark_tier_healthy()` generic functions (tier name → Redis key)
- `inference.py`: `_get_tiers()` now loads from `InferenceConfig.tiers` via `_create_tier_client()` factory
- `inference.py`: API calls exclude frequency_penalty/presence_penalty for cloud tiers (is_local=false)
- `inference.yaml`: Cloud profiles (gemini/openai/deepseek/anthropic) now have is_local:false, timeout:60, max_tokens_cap

**Scenario 2 E2E verified:** `MIRROR_INFERENCE_PROVIDER=gemini` → MA orchestrator → Gemini API (1.5s, 4480 tok) → response

## Test Summary (2026-04-07)

| Module | Tests | Coverage |
|--------|-------|----------|
| test_orch/test_models.py | 7 | dataclass validation, is_usable() |
| test_orch/test_discovery.py | 14 | CLI probing, caching, auth detection |
| test_orch/test_executor.py | 27 | cmd build, output parsing, subprocess mock |
| test_orch/test_db.py | 10 | schema, CRUD, aggregation |
| test_orch/test_router.py | 13 | strategy selection, fallback, DB failure |
| test_mcp/test_server.py | 14 | 6 tools, error handling, mock CLI |
| test_api/test_routes.py | 6 | HTTP endpoints, error responses |
| (existing) | 44 | detectors, parser, scanner |
| **Total** | **135** | — |
