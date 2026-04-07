# Changelog

## 0.2.0 (2026-04-05)

### Added

- 2 new detectors: **orphan** (tool_use/tool_result mismatch), **stuck** (abandoned tool calls with per-tool duration stats)
- `recover` command — extract session context (files, git, GitHub actions, URLs, issues) for resumption in a new session
- `doctor` command — 7 health checks: trust-dialog-hang, hooks-trust-flag, claude-json-corruption, corrupted-tool-use, orphaned-tool-results, zombie-sessions, relay-health
- CLI restructured as click group: `llm-relay-detect scan`, `llm-relay-detect recover`, `llm-relay-detect doctor` (bare `llm-relay-detect` still works as scan)

## 0.1.0 (2026-04-04)

Initial release.

### Features

- Session file discovery across all `~/.claude/projects/`
- JSONL streaming parser with error tolerance
- 6 detectors: synthetic, microcompact, cache, bloat, resume, featureflags
- Plain text, JSON, and rich console output formats
- Click CLI with argparse fallback (zero-dep mode)
- Exit codes: 0 (healthy), 1 (warn), 2 (critical)
- FeatureFlags feature flag extraction and analysis
- Bilingual documentation (EN/KO)
