# Changelog

All notable changes to llm-relay are documented here.

## [Unreleased]

### Added
- **`llm-relay env-fingerprint` command** (`env_fingerprint.py`): single-shot, idempotent snapshot of the local LLM CLI environment for agent-driven onboarding. Emits a structured JSON (or YAML) document with sections for `llm_relay` package state, per-CLI install/auth/version, port availability, filesystem layout, redacted environment variables (API keys reported as `set`/`empty`/`null` only), and an optional `doctor` summary. Designed so an agent (Claude Code / Codex / Gemini) automating an llm-relay install can parse the environment instead of scraping `init` output. Schema versioned (`schema_version: "1"`); sub-probe failures surface as `_error` markers without crashing the snapshot. Options: `--format {json,yaml}`, `--no-doctor`, `--ports`.

### Performance
- **Incremental composition cache** (`composition.py`, #16): `analyze_session_composition` and `analyze_session_composition_per_turn` now fold delta turns into cached state instead of re-walking the full session on every new turn. Previously the cache invalidated whenever any new turn arrived, forcing an O(n²) replay; on long-running sessions this caused `/api/v1/turns` to balloon to tens of seconds and daemon RSS to climb above 10 GB within ~30 min of normal multi-agent traffic. Cache now keys on `session_id` alone with `(max_turn_processed, accumulated, totals, …)` state; new turns trigger a `WHERE turn_number > max_turn_processed` fetch. Compaction events (`storage_mode="full"`) reset accumulated state and re-absorb the snapshot. Exception during fold falls back to a full rebuild so an incremental error can't poison the cache.

### Changed
- **Claude Code zone defaults restored to 1M model window**: Yellow 500K / Orange 700K / Red 900K / Hard 1M (`_zones.py`, `tui.py`, `display.py`, `.env.public`). Reverts the 665K rescale that was applied on the basis of a single 5/13 observation of auto-compact firing around 650K. Subsequent multi-hundred-K sessions did not reproduce that trigger point — Opus 4.7 [1m] reaches ~95% (~950K) before client-side auto-compact engages, so the 665K ceiling was unnecessarily conservative. Stock deployments without 1M context entitlement can override via `LLM_TOKEN_CEILING=200000`.
- **Red-zone message split for CC vs Codex** (`i18n.py`): new keys `zone.abs.red.cc` and `zone.ratio.red.cc` carry an explicit "Auto-compact imminent — hand off to a new session" guideline used by CC paths. Codex paths keep the existing "Session rotation required" wording (Codex has no client-side compaction; the 400K hard limit semantics differ).
- **`duplicate_reads` no longer accumulates across compaction** (`composition.py`): when a `storage_mode="full"` turn arrives, read counts reset together with the accumulated context. Previously reads from pre-compaction turns were counted as duplicates of post-compaction reads, inflating `duplicate_read_count` and triggering spurious `duplicate_read_warning` flips. Visible in `/api/v1/turns`, `/api/v1/display`, dashboard Context Health, and TUI duplicate-file listings.

### Tests
- `tests/test_api/test_turns.py::_zone_env` autouse fixture now patches both `_zones` and `routes` modules so legacy 1M-scale assertions stay stable against new production defaults.

## [0.9.3] - 2026-05-06

### Added
- **Windows native support**: SelectorEventLoop policy for uvicorn, UTF-8 .env encoding
- **Windows background service**: `llm-relay service install/start/stop/status/uninstall` — no console window, auto-start on login, PID file management, clean uninstall (zero traces)
- **Background CLI scanner**: Daemon thread (10s interval) for instant process lookup on Windows; Linux unaffected (uses /proc directly)
- **psutil TTL cache**: 30s cache for `collect_open_session_paths/path_pids` on non-Linux platforms
- **CC session file parser**: `_parse_cc_session_raw()` for Windows file-based detection (turns, tokens, cache hit rate from JSONL)
- **Loading overlay**: Spinner + fade-out on dashboard, display, and history pages

### Changed
- **Zone classification refactored**: Extracted to `api/_zones.py` (CC + Codex dual-scale A/B + token metrics) — resolves circular import between display.py and routes.py
- **Shared DB connection**: `_get_db_conn()` prefers proxy's WAL connection for read consistency
- **Windows liveness fallback**: mtime + process existence when psutil `open_files()` is denied
- **Deduplication**: Proxy-DB CC sessions no longer duplicated by file-based discovery on Windows

### Fixed
- Page transition infinite loading on Windows (ProactorEventLoop incompatibility)
- CP949 decode error on Korean Windows when reading .env files
- Service process dying with parent (CREATE_BREAKAWAY_FROM_JOB flag)

### Removed
- Dead code: `_any_cli_process_running()` (replaced by `is_cli_running_cached`)
- Dead code: `get_shared_conn()` wrapper in proxy.py
- Redundant zone logic duplication between routes.py and display.py

## [0.9.2] - 2026-04-30

### Added
- **I18n**: Lightweight en/ko locale support via `i18n.py` message catalog (27 keys)
  - Backend: `t()` translation function wired into all zone classifiers (routes.py, display.py, mcp/server.py)
  - Frontend: `msg()` helper with browser `navigator.language` auto-detection (dashboard, display, history)
  - API: `/api/v1/i18n?lang=ko` endpoint for frontend message loading
  - Env: `LLM_RELAY_LANG` (default `en`)
- **MCP tool**: `session_history` — conversation replay with turn filtering (8 tools total)
- **Auto-load .env**: Native uvicorn startup now reads `.env.public` → `.env.local` automatically (Docker `env_file` parity)

### Fixed
- Hardcoded Korean zone labels replaced with i18n `t()` calls (PR #12/#13 by @cnighswonger)
- Composition pie chart missing after native restart (LLM_RELAY_HISTORY not loaded without .env autoload)

## [0.9.1] - 2026-04-29

### Added
- **Zone A↔B alignment**: Recalibrated CC/Codex dual-zone thresholds
- **Codex 400K unification**: Official context window constants
- **Display enhancements**: Placeholders, font normalization, dead code removal
- 3-provider status tiles (Anthropic/OpenAI/Gemini) on dashboard
- `term_name` surfacing on `/turns` + dashboard render
- GH App token injection for Codex via `cli_delegate`
- API parameter validation (`_ParamError` → 400)

## [0.7.1] - 2026-04-26

### Added
- **`llm-relay init`**: One-command setup — auto-detects CLIs, configures Claude Code proxy + MCP, initializes DB, starts server, runs health check. Options: `--dry-run`, `--skip-server`, `--port`
- Updated README with `llm-relay init` as primary quick start
- 18 new tests for init module

## [0.7.0] - 2026-04-26

### Added
- **Quota monitoring**: `/api/v1/quota` endpoint surfaces Q5h/Q7d utilization and overage status from stored ratelimit headers
- **Error rate tracking**: `/api/v1/errors` endpoint with 2xx/4xx/5xx/429 breakdown and error rate percentage
- **Cache hit rate**: `/api/v1/cache` endpoint and per-session `cache_hit_rate` field in `/turns/{id}` and `/display` responses
- **TTL tier detection**: `/api/v1/ttl` endpoint detects 1h/5m/mixed ephemeral cache tiers from SSE `message_start` events
- **Dashboard API Health section**: 4-card grid showing quota, error rate, cache hit rate, and TTL tier with 30s auto-refresh
- **Display page badges**: Cache hit rate and TTL tier badges on session cards
- Ephemeral token extraction from SSE `cache_creation.ephemeral_1h/5m_input_tokens` (streaming and non-streaming)
- DB migration: `ephemeral_1h_tokens` and `ephemeral_5m_tokens` columns on `requests` table
- 28 new tests (quota 5, error 5, cache 6, TTL 7, log_request 2, API endpoints 3)

## [0.6.0] - 2026-04-26

### Added
- **Dashboard Context Health section**: Real-time summary of SNR, duplicate reads, and tool_result% across all active sessions with per-session health cards
- **Duplicate read details**: `duplicate_reads` dict in composition API returns `{filepath: count}` instead of count-only; top file basenames shown in Display, Dashboard, and TUI
- **Duplicate read warning**: `DUPLICATE_READ_WARN_THRESHOLD` env var (default 5) triggers visual warning when any file exceeds threshold
- **SNR recommendation**: `CC_SNR_WARNING` env var (default 0.3) adds session-split recommendation message when SNR drops below threshold
- **Per-turn composition chart**: SVG stacked area chart on `/history/` session detail page showing how context composition evolves across turns, with compaction markers and hover tooltips
- **Per-turn composition API**: `GET /api/v1/history/{session_id}/composition` endpoint with automatic sampling for large sessions (>50 turns)
- `composition` field added to `/api/v1/turns` response (was only on `/api/v1/display`)
- 13 new tests (composition, turns, history)

### Fixed
- History test mock leaks: `discover_external_cli_sessions` was not mocked, causing tests to find real session files on disk
- Codex session file fallback test: path detection now works with `patch()` instead of monkey-patching
- 2 ruff lint issues in `scripts/context_composition.py`

## [0.5.0] - 2026-04-24

### Added
- **Context composition analysis**: Real-time 6-category breakdown (user/assistant/tool_use/tool_result/thinking/system) with SNR metrics and duplicate read tracking
- **`llm-relay top`**: btop-style terminal monitor using Rich Live — works over SSH without a browser
- **Connection type detection**: Automatically detects SSH, tmux, screen, mosh, tailscale, native (and combinations like ssh+tmux) from `/proc/PID/environ` + parent process tree
- **SVG pie chart** on `/display/` page showing context composition with popover tooltips
- **Connection type badges** on session cards
- **i18n support** (contributed by [@cnighswonger](https://github.com/cnighswonger)): Browser locale detection with en/ko; server override via `LLM_RELAY_LANG`
- `/api/v1/i18n` endpoint for locale-specific messages
- `scripts/context_composition.py` CLI analysis tool
- 51 new tests (composition 24, connection type 14, TUI 13)

### Changed
- Display page: prompt moved to top of session cards
- Display page: border-radius 4px, border-left 2px
- Development status upgraded from Alpha to Beta

## [0.4.0] - 2026-04-23

### Added
- **Session history capture**: Proxy-level conversation recording for CC/Codex/Gemini
- Delta/full storage with compaction detection
- `/history/` web replay viewer with compaction timeline
- 3 history API endpoints (`/api/v1/history`, `/{session_id}`, `/{session_id}/compactions`)
- `session_history` MCP tool (8th tool)
- Alive filter for `/api/v1/turns` endpoint with shared liveness helpers

### Changed
- MCP tools: 7 → 8

## [0.3.0] - 2026-04-15

### Changed
- Clean public release: removed internal references and legacy naming
- Unified branding to llm-relay
- Sanitized design documentation for public release
- Removed FeatureFlags detector (internal-only)
