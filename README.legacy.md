# llm-relay-detect

> [한국어 문서](README.ko.md)

**Diagnostic health-check for Claude Code sessions** — read-only, zero-dep core.

Scans your local Claude Code session files and detects known bugs that cause excessive token consumption. Never modifies your data.

## Install

```bash
pip install llm-relay-detect            # zero dependencies, plain text output
pip install 'llm-relay-detect[cli]'     # adds click + rich for formatted output
```

Or download a standalone executable from [Releases](https://github.com/ArkNill/llm-relay-detect/releases).

## Quick Start

```bash
llm-relay-detect                # scan last 10 sessions, show health report
```

That's it. One command.

## What It Detects

| Detector | What | Severity | Reference |
|----------|------|----------|-----------|
| **False Rate Limiter** | `<synthetic>` entries — fake rate limits from client | CRITICAL | [#40584](https://github.com/anthropics/claude-code/issues/40584) |
| **Context Stripping** | Tool results silently replaced with `[Old tool result content cleared]` | WARN/CRIT | [#42542](https://github.com/anthropics/claude-code/issues/42542) |
| **Cache Efficiency** | Low cache read ratio + cold start waste | INFO~CRIT | [#42906](https://github.com/anthropics/claude-code/issues/42906) |
| **Log Inflation** | PRELIM/FINAL entry duplication (inflates local stats) | INFO/WARN | — |
| **Resume Corruption** | Timestamp reversals, null bytes, DAG breaks, cross-version issues | WARN | [#43044](https://github.com/anthropics/claude-code/issues/43044) |
| **FeatureFlags Flags** | Active server-side feature flags (tool result budget, per-tool caps) | INFO/WARN | [#42542](https://github.com/anthropics/claude-code/issues/42542) |
| **Orphan Tool Calls** | tool_use without matching tool_result (and vice versa) | INFO/WARN | — |
| **Stuck Tool Calls** | Tool calls that never received a result but session continued | INFO/WARN | — |

## Usage

```bash
llm-relay-detect                    # scan last 10 sessions
llm-relay-detect scan --all         # scan all sessions
llm-relay-detect scan --last 20     # scan last 20 sessions
llm-relay-detect scan --session 0348  # scan specific session (prefix match)
llm-relay-detect scan --json        # JSON output for automation
llm-relay-detect recover            # extract context from latest session (handoff format)
llm-relay-detect recover --format actions   # structured action list
llm-relay-detect doctor             # run 7 health checks on Claude Code config
llm-relay-detect doctor --fix       # attempt to fix issues
```

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | All sessions healthy |
| 1 | At least one WARN finding |
| 2 | At least one CRITICAL finding |

## Example Output

```
llm-relay-detect v0.1.0 — Claude Code Session Health Check
Scanned 5 of 271 sessions.

Overall: 3 healthy, 1 degraded, 1 unhealthy

UNHEALTHY  03480ffe  (Apr 3, 7.5 MB, 2188 entries)
├─ CRITICAL  False Rate Limiter (B3)
│  4 synthetic entries — client faked rate limit errors
│  → These are false alarms, not real API limits.
│  Ref: https://github.com/anthropics/claude-code/issues/40584
├─ WARN  Context Stripping (B4)
│  125 tool results replaced with [Old tool result content cleared]
│  → Start fresh sessions every 15-20 tool uses.
└─ INFO  Cache Efficiency
   Average cache read ratio: 87% (healthy: >80%).

— Global —
  WARN  Tool Result Budget Active
  Aggregate tool result cap is 200,000 characters.

This tool is READ-ONLY. No session files were modified.
```

## How It Works

1. Discovers session JSONL files in `~/.claude/projects/`
2. Streams each file line-by-line (handles multi-GB sessions)
3. Runs 6 independent detectors on each session
4. Extracts FeatureFlags feature flags from `~/.claude.json`
5. Generates a health report with actionable recommendations

```
~/.claude/projects/
  └── {project}/
      └── {session-uuid}.jsonl  ← llm-relay-detect reads these
```

## Design Principles

- **READ-ONLY** — never writes to or modifies session files
- **Zero runtime dependencies** — core runs on stdlib only (Python 3.9+)
- **CLI-first** — designed for terminal users, not a library
- **Actionable** — every finding includes a recommendation and GitHub issue link
- **Fast** — streams JSONL line-by-line, scans 5 sessions in <1 second

## Python API

```python
from llm-relay-detect.scanner import discover_sessions
from llm-relay-detect.parser import parse_session
from llm-relay-detect.analyzer import analyze_all

sessions = discover_sessions(limit=10)
parsed = [parse_session(s.path) for s in sessions]
report = analyze_all(parsed)

for sr in report.session_reports:
    print(f"{sr.session.session_id[:8]}: {sr.health.value}")
    for f in sr.findings:
        print(f"  {f.severity.value}: {f.title}")
```

## Background

This tool is built on findings from [claude-code-cache-analysis](https://github.com/ArkNill/claude-code-cache-analysis) — a comprehensive investigation of 6+ bugs causing excessive token consumption in Claude Code, backed by proxy-level measurements across 100+ GitHub issues.

## License

MIT
