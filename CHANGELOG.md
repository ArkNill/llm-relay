# Changelog

All notable changes to llm-relay are documented here.

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
