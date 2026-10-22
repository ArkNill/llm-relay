# Changelog

All notable changes to llm-relay are documented here.

## [Unreleased]

### Added
- **Context growth rate and ETA on `/display/`** (2026-08-05): Claude Code session cards show
  the median context growth per turn over the last 40 requests, the recent turn rate, and the
  estimated time to the next zone threshold (`db.get_session_growth`; `/api/v1/display` fields
  `growth_per_turn`, `turns_per_min`, `eta_next_s`). Idle sessions show no ETA.
- **Stream stall diagnostics** (2026-09-28): streamed requests record `content_deltas`,
  `first_delta_ms`, `last_delta_ms`, `message_stop` and `stream_end` (`completed` /
  `client_disconnect` / `upstream_error`) in `requests.raw_usage`, so an aborted or stalled
  stream can be told apart from a long generation. No schema change.
- **`LLM_RELAY_MAX_REWRITE_BYTES`** (2026-09-29): request bodies above this many bytes skip
  the cache-fix / effort-fix / tokpress rewrite and go upstream unchanged, flagged as
  `rewrite_skipped` in `requests.raw_usage`; history capture is unaffected. Default 0 (off):
  the rewrite reorders tools on most requests, so skipping it on one large request changes
  the prompt prefix and costs a cache miss. A valve for image-heavy bursts, not for normal
  traffic.
- **`LLM_RELAY_DB_QUEUE_MAX_BYTES`** (2026-09-29): the background DB writer bounds the bytes its
  queue keeps alive (default 256 MB; 0 disables). Every job carries a size estimate (a
  conversation capture: request body minus the stripped inline base64, plus the response text),
  and a job that would push the queued total over the cap is written to the spill file at once
  (`reason: queue bytes`) instead of growing the process while the database is slow or down.
  The count cap `LLM_RELAY_DB_QUEUE_MAX` stays. `GET /_health` reports `db_writer.queued_bytes`
  and `queue_max_bytes`.
- **Request start lines and in-flight counters** (2026-09-29): uvicorn's access log is written
  when a response completes, so the request that took the relay down (the dashboard poll behind
  the OOM loop) left no trace. An ASGI middleware now logs one line when a request starts
  (`request start POST /v1/messages 818767B client=127.0.0.1 session=e285e2ec`, logger
  `llm-relay.access`, `/_health` excluded; `LLM_RELAY_LOG_REQUEST_START=0` turns it off), and
  `GET /_health` reports `in_flight`, `proxy_in_flight` (`/v1/` requests, a stream counts until
  it ends: 0 means a restart cuts no client stream), `memory_rss_mb` and `memory_cgroup_mb`.
- **Server-side tool counts in `raw_usage`** (2026-10-19): streamed requests record
  `web_search_requests` and `web_fetch_requests` from the final `message_delta` usage
  (`server_tool_use`) in `requests.raw_usage`, so search fees and search-heavy requests can be
  accounted for. No schema change; non-streamed rows already store the whole `usage` object.
- **Request body admission gate** (2026-10-22): a `/v1/` request whose `content-length` is at
  least `LLM_RELAY_BODY_GATE_MIN_BYTES` (4 MiB) waits, before its body is read, while the bodies
  already admitted plus its own would exceed `LLM_RELAY_MAX_INFLIGHT_BODY_BYTES` (unset = a
  quarter of the cgroup `memory.max`, 256 MiB without one; 0 = off). Admission is in arrival
  order, a body alone is always admitted, and the bytes are given back as soon as the upstream
  response headers arrive. A request still waiting after `LLM_RELAY_BODY_GATE_TIMEOUT_S` (120;
  0 = answer at once) gets a 503 API error (`stream_end: body_gate_timeout`) that the client
  retries. Requests that waited carry `body_gate_wait_ms` in `requests.raw_usage`; `GET
  /_health` reports `body_gate`. Isolated check, 25 MB bodies x 40 with 16 in flight under a
  1 GiB limit: the relay was OOM-killed at 1005 MB before; with the gate it peaks at 372 MB
  (10 waits, no timeouts, same wall time) and survives 24 in flight at 374 MB.

### Fixed
- **Web tool calls failing with 400 at effort `xhigh` / `max`** (2026-07-27): Claude Code sends
  WebSearch/WebFetch subrequests with thinking disabled but keeps the session's effort level,
  which the API rejects (`output_config.effort 'xhigh' is not supported when thinking is
  disabled`). Requests that disable thinking now have `output_config.effort` clamped to `high`
  (`proxy/cc_effort_fix.py`); requests with thinking enabled are left untouched.
  `LLM_RELAY_EFFORT_FIX=0` turns it off, `LLM_RELAY_EFFORT_FIX_TARGET` changes the target.
- **Database writes on the event loop** (2026-09-28): usage, diagnostic and history rows are
  now written by a background thread (`proxy/dbwriter.py`). `submit()` is a synchronous
  enqueue that response teardown cannot cut short; rows are written in submission order,
  retried on a fresh connection (`LLM_RELAY_DB_WRITE_RETRIES`, 3) and spilled to
  `~/.llm-relay/spill-YYYYMMDD.jsonl` (`LLM_RELAY_SPILL_DIR`) instead of being dropped. The
  queue drains on shutdown, and `GET /_health` reports the writer counters.
- **Requests hanging when the upstream pool is exhausted** (2026-09-28): the upstream pool grows
  from 20 to 100 connections (keep-alive 10 to 20; `LLM_RELAY_MAX_CONNECTIONS`,
  `LLM_RELAY_MAX_KEEPALIVE`), and a pool wait longer than `LLM_RELAY_POOL_TIMEOUT_S` (30 s)
  returns a 503 API error instead of hanging. Transport failures return 502/504 API errors
  instead of an opaque 500, and a client that disconnects before the upstream response headers
  arrive cancels the upstream request (checked every `LLM_RELAY_DISCONNECT_POLL_S`). Both cases
  are recorded in `requests` (status 499 / `stream_end=upstream_error`).
- **MCP server, CLI and test suite using a different database than the relay** (2026-09-28):
  only the proxy loaded `.env.public` / `.env.local`, so entry points started with an empty
  environment (`llm-relay-mcp` spawned by Claude Code, the CLI, pytest) fell back to the SQLite
  file while the relay wrote to PostgreSQL. `proxy/db.py` now loads both files (explicit
  variables still win) before choosing the backend, and `orch/db.py` reuses that decision.
  `tests/conftest.py` forces the suite onto a temporary SQLite database (`LLM_RELAY_TEST_DB`
  to override).
- **Duplicate turn numbers after a relay restart** (2026-09-28): the first capture of a session
  after a restart seeds its turn counter and delta baseline from the newest stored turn
  (`db.get_last_turn_state`) instead of starting again at turn 1 with a full snapshot.
- **Relay OOM crash loop on dashboard polls** (2026-09-29): with `LLM_RELAY_HISTORY=1`,
  the composition analysis behind `/api/v1/display` and `/api/v1/turns` loaded every
  stored turn of a session on a cold cache (one image-heavy session held 3.67 GB), so
  the first dashboard poll after each restart pushed the process past `MemoryMax` and
  systemd's start limit left the port closed. Cold loads now start at the newest `full`
  snapshot, which yields the same result (`composition._cold_start_turn`).
- **Session history stored inline base64 payloads** (images / PDF documents returned by
  tools) verbatim — 24 MB rows, 3.7 GB in a single day. `request_messages` now keeps the
  block shape with `data: ""` plus `source.omitted_bytes`; `request_size_bytes` still
  records the real request body size (`history._strip_binary_payloads`).
- **False compaction events and repeated full snapshots when several conversations share
  one session_id** (Claude Code subagents, WebSearch/WebFetch helper calls, title
  generation): the delta baseline is now kept per conversation (`session_id` + fingerprint
  of the first message). Only a message-count drop inside the same conversation is
  recorded as a compaction; a new conversation starts with a full snapshot and no event.
- `/display/` no longer starts a new poll while the previous one is still in flight.
- **Composition caches growing for the lifetime of the relay** (2026-09-29): nothing evicted an
  entry (`clear_cache()` has no production caller), and every cached session kept its whole
  message list although only the aggregates are read (49 sessions: 45 MB with it, 0.2 MB
  without). The session state now keeps aggregates only; the per-turn analysis keeps running
  totals instead of re-classifying the whole context at every turn (3.75 s to 1.59 s on the
  event loop for a 692-turn session); and the session, per-turn and session-file caches are
  bounded by entry count and idle time (`LLM_RELAY_COMPOSITION_CACHE_MAX`, default 64;
  `LLM_RELAY_COMPOSITION_CACHE_TTL_S`, default 28800; 0 disables a bound). `GET /_health`
  reports `composition_cache`.
- **Request bodies held several times over per in-flight request** (2026-09-29): a
  `/v1/messages` body was parsed twice (once for the cache/effort rewrite, once more in the
  stream path for history capture), the Starlette-cached original stayed next to the
  rewritten copy, the streaming closure held the full parse (inline base64 included) until
  the upstream stream ended, and a large upload was collected as a list of 64 KB chunks
  before being joined. The body is now read into one growing buffer without the
  request-level cache, parsed once, and history capture receives a copy with inline base64
  replaced by size markers (`history.history_view`) before the upstream request starts, at
  which point the full parse is dropped. Five concurrent 24 MB image requests: peak 510 MB
  to 248 MB, resident after the burst 463 MB to 202 MB (isolated relay, fake upstream).
  Stored history rows are unchanged.
- **`.env.local` not overriding `.env.public` outside systemd** (2026-09-29): the native env
  loaders in `proxy/proxy.py` and `proxy/db.py` applied `.env.public` first with
  `setdefault`, so a key present in both files kept the public value when the process was
  started by hand, by pytest or as the MCP server (`LLM_RELAY_CACHE_FIX` was the only
  affected key; the systemd unit's `EnvironmentFile` order was already right). `.env.local`
  is now loaded first; explicit environment variables still win over both files.
- **Streamed turns stored `response_size_bytes = 0`** (2026-09-29): the streaming capture never
  measured its response, so the history page's per-session response total was 0 for every
  Claude Code session. It now records the UTF-8 size of the accumulated text, thinking and
  tool-input blocks (`history.content_bytes`).
- **Relay INFO diagnostics never reached the journal** (2026-09-29): under uvicorn the
  `llm-relay` logger had no handler and propagated to a bare root logger, so its INFO lines
  (cache fix, compaction, client gone before headers) were dropped and its warnings only
  surfaced through logging's last-resort handler. The proxy now installs a stderr handler for
  its own loggers when nothing else has configured logging (the CLI's `basicConfig` is left
  alone). The main logger still runs at WARNING; `LLM_RELAY_LOG_LEVEL=INFO` turns the
  diagnostics on.
- **Streamed requests with server-side tools stored only the first iteration's input**
  (2026-10-19): with server-side web search or web fetch, one request runs several model
  iterations; `message_start` reports the first one, and the cumulative totals arrive in the
  final `message_delta` usage, which the stream path read only for `output_tokens`. Over 36
  Claude Code runs the stored rows, priced at list rates, came to 51.3% of the cost the CLI
  reported (that figure also includes the per-search fee, which the relay does not price); runs
  without server-side searches matched. `message_delta` usage now raises the input, cache-write,
  cache-read and 1h/5m cache-tier counts to its final values. Earlier rows cannot be corrected.

### Notes
- Rows stored before this change can be rewritten in place with the same transform. The
  leftover check is `jsonb_path_exists(request_messages,
  'strict $.**.source ? (@.type == "base64" && @.data != "")')` — testing only
  `source.type` also matches already-stripped rows.

## [0.9.6] - 2026-05-21

> Single-command Windows install + atomic `llm-relay init`. Surfaced by
> 2026-05-21 hmj PC dogfood: the previous `init` would write
> `ANTHROPIC_BASE_URL` into `~/.claude/settings.json` before the proxy
> server was actually serving traffic, so any running Claude Code session
> would route to a dead port and the symptom looked like a generic API
> connection error. This release makes that ordering atomic and ships a
> PowerShell one-line installer.

### Added
- **One-command Windows installer** (`scripts/install.ps1`): for users
  with Python 3.9+ already installed, the entire install flow collapses
  to one PowerShell command:
  ```powershell
  irm https://raw.githubusercontent.com/ArkNill/llm-relay/main/scripts/install.ps1 | iex
  ```
  The script verifies Python, detects an active venv (uses it if
  present, else falls back to `--user` with a PATH hint), `pip install`s
  `llm-relay[all]`, then calls `llm-relay init` which handles
  daemon + health-gate + routing in the right order. Documented in
  `README.md` "One-command install (Windows native)".
- **README "Prerequisites" section** spelling out the Python 3.9+
  requirement, the recommended venv setup, and platform-specific install
  hints (winget / Homebrew / apt).

### Changed
- **`llm-relay init` is now atomic** (`setup_init.run_init`). New ordering:
  1. Detect CLIs, find port, init DB, write config, init knowledge dir.
  2. **Start the proxy server.**
  3. Health-gate (`/_health` polling).
  4. Configure Claude Code (`ANTHROPIC_BASE_URL`, MCP) **only after**
     the server is verified healthy.
  Routing is never activated unless the proxy actually responds, so an
  abort at step 2 or step 3 leaves `~/.claude/settings.json` untouched
  -- no more "ConnectionRefused on port 8083" surprises in your next
  Claude Code session.
- **`--skip-server` now also skips routing**. Previously it was a UX
  trap: server skipped, but `settings.json` still gained an
  `ANTHROPIC_BASE_URL` pointing at the not-running port. Now
  `--skip-server` skips the server, the health gate, AND the
  Claude Code routing change as a single decision. The summary message
  surfaces this explicitly so a follow-up `llm-relay serve` + re-run is
  the documented path to enabling routing.
- **Windows background daemon goes through `win_service.start_daemon`**
  (`_start_server` in `setup_init.py`). The previous Popen + 
  `CREATE_NEW_PROCESS_GROUP` left the proxy tied to its job object on
  Windows so it died when the parent SSH session disconnected (observed
  during the 2026-05-21 dogfood). The win_service path uses pythonw plus
  `CREATE_BREAKAWAY_FROM_JOB`, which detaches cleanly.

### Tests
- New `test_skip_server_does_not_write_settings_json` regression in
  `tests/test_api/test_init.py` pins the atomic contract.

### Fixed
- **Docker image build** (`Dockerfile`, 0.9.5 carry-over): removed the
  unconditional `COPY vendor/tokpress /tmp/tokpress` + `pip install`
  step. The vendor source is kept outside the repository, so the COPY
  always failed in GitHub Actions and the Docker workflow had been
  broken on every tag since v0.9.2. The proxy already imports `tokpress`
  inside a `try/except ImportError` guard, so the image runs unchanged
  when the package is absent (`_tokpress_available` simply stays
  `False`). The v0.9.5 image was rebuilt via
  `gh workflow run docker.yml -f tag=0.9.5` after the change landed on
  main; the v0.9.6 image rebuilds automatically on the new tag.

## [0.9.5] - 2026-05-21

> Hotfix release surfaced by dogfooding `verify install` and `--help` on a
> Windows host immediately after 0.9.4 went live on PyPI.

### Fixed
- **`__version__` was stale at `0.9.2`** (`src/llm_relay/__init__.py`,
  `src/llm_relay/detect/__init__.py`). The 0.9.4 release bumped only
  `pyproject.toml`, leaving the importable runtime version out of sync;
  `verify install`'s `version_consistency` check correctly flagged this
  as a warn. Both sources now read `0.9.5`.
- **`UnicodeEncodeError` on `--help` under Windows + Korean locale (cp949)**
  (`src/llm_relay/detect/cli.py`). Three occurrences of the em-dash
  character (`—`) inside Click docstrings / `click.echo` calls were
  unprintable on cp949 stdout and caused `llm-relay --help` to crash
  before showing any subcommands. Replaced with ASCII `--`. No behavior
  change beyond making the command actually usable on Windows-Korean
  hosts.

## [0.9.4] - 2026-05-21

> First PyPI release since 0.9.1. Collapses the locally-tagged 0.9.2 and 0.9.3
> work — neither of which was published to PyPI — into a single shipped
> version. Adds the agent-driven onboarding track (env-fingerprint + verify
> + AGENT_SETUP playbook) on top of the prior unreleased changes.

### Added
- **Agent-driven setup playbook** (`docs/AGENT_SETUP.md`): structured onboarding document targeted at AI coding agents (Claude Code / Codex / Gemini) automating an llm-relay install on a user's behalf. Sequences `env-fingerprint` + `verify` into a five-phase flow (probe → install → init → per-CLI integration → final acceptance) with explicit `[PERMISSION: …]` markers wherever the agent must pause for user consent, and explicit "stop and ask" rules for ambiguous states. README section "Agent-driven setup" links to it.
- **`llm-relay verify` command group** (`verify/`): four idempotent verification subcommands designed for agent-driven onboarding. `verify install` confirms the package itself (Python version, importability, entry points, optional extras, version consistency). `verify config` confirms local state (db dir, schema, writability, config file, knowledge dir, port availability, no deprecated env vars). `verify integration --cli {claude-code,openai-codex,gemini-cli,all}` confirms each CLI is wired through the relay (binary on PATH, settings file present, `ANTHROPIC_BASE_URL` routing to localhost, MCP server registered). `verify all` aggregates install + config + integration. Shared output schema (`schema_version: "1"`) emits per-check status (`pass`/`fail`/`warn`/`skipped`) with optional remediation strings; overall priority is `fail > warn > pass`. Skipped checks (e.g. CLI not installed) never escalate to fail. Optional `--live` probes `/_health` on the proxy. Common options: `--format {text,json}`, `--quiet`, `--no-remediation`. Exit code is 0 on pass/warn, 1 on fail.
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
