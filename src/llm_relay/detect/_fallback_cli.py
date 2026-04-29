"""Argparse-based CLI fallback -- zero dependencies."""

from __future__ import annotations

import argparse
import sys

from llm_relay.detect import __version__
from llm_relay.detect.analyzer import analyze_all
from llm_relay.formatters.json_fmt import JsonFormatter
from llm_relay.formatters.plain import PlainFormatter
from llm_relay.providers import CLAUDE_CODE, detect_providers, get_provider, list_provider_ids


def _format_size(size_bytes: int) -> str:
    if size_bytes >= 1_048_576:
        return f"{size_bytes / 1_048_576:.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def _serve_fallback() -> None:
    """Fallback serve command when click is not available."""
    import argparse as _ap

    p = _ap.ArgumentParser(prog="llm-relay serve", description="Start proxy server with dashboard.")
    p.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    p.add_argument("--port", "-p", type=int, default=8083, help="Listen port (default: 8083)")
    args = p.parse_args(sys.argv[2:])  # skip "llm-relay serve"

    try:
        import uvicorn
    except ImportError:
        print("Error: uvicorn not installed. Run: pip install llm-relay[proxy]", file=sys.stderr)
        raise SystemExit(1)

    print(f"llm-relay v{__version__} -- starting on http://{args.host}:{args.port}")
    print("  /dashboard/  -- CLI status, cost, delegation history")
    print("  /display/    -- turn counter with CC/Codex/Gemini sessions")
    print(f"  Proxy:       ANTHROPIC_BASE_URL=http://localhost:{args.port}")
    print()
    uvicorn.run("llm_relay.proxy.proxy:app", host=args.host, port=args.port, log_level="info")


def main() -> None:
    """Entry point for zero-dep CLI."""
    # Handle "serve" subcommand before argparse (which doesn't support subcommands well)
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        _serve_fallback()
        return

    valid_providers = [*list_provider_ids(), "all", "auto"]

    parser = argparse.ArgumentParser(
        prog="llm-relay",
        description="AI CLI Session Health Check -- read-only diagnostics.",
    )
    parser.add_argument("--all", "-a", action="store_true", dest="scan_all", help="Scan all sessions")
    parser.add_argument("--last", "-n", type=int, default=None, dest="last_n", help="Scan last N sessions")
    parser.add_argument("--session", "-s", default=None, dest="session_id", help="Scan specific session (prefix match)")
    parser.add_argument("--project", "-p", default=None, dest="project_filter", help="Filter by project directory")
    parser.add_argument(
        "--provider",
        default="auto",
        choices=valid_providers,
        help="Which CLI tool to scan (default: auto-detect)",
    )
    parser.add_argument("--json", "-j", action="store_true", dest="json_output", help="JSON output")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show all sessions including healthy")
    parser.add_argument("--version", action="version", version=f"llm-relay {__version__}")

    args = parser.parse_args()

    # Determine limit
    limit = None
    if args.session_id:
        limit = None
    elif args.scan_all:
        limit = None
    elif args.last_n:
        limit = args.last_n
    else:
        limit = 10

    # Resolve providers
    if args.provider == "auto":
        providers = detect_providers()
        if not providers:
            providers = [get_provider(CLAUDE_CODE)]
    elif args.provider == "all":
        providers = [get_provider(pid) for pid in list_provider_ids()]
    else:
        providers = [get_provider(args.provider)]

    # Discover sessions across all providers
    all_session_files = []
    for prov in providers:
        all_session_files.extend(
            (prov, sf) for sf in prov.discover_sessions(project_filter=args.project_filter)
        )

    total = len(all_session_files)

    if total == 0:
        provider_names = ", ".join(p.display_name for p in providers)
        print(f"No sessions found for: {provider_names}")
        print("Make sure the CLI tool has been used at least once.")
        sys.exit(0)

    # Sort all sessions by mtime descending, apply filters
    all_session_files.sort(key=lambda x: x[1].mtime, reverse=True)

    if args.session_id:
        all_session_files = [(p, sf) for p, sf in all_session_files if sf.session_id.startswith(args.session_id)]
    elif limit is not None:
        all_session_files = all_session_files[:limit]

    if not all_session_files:
        if args.session_id:
            print(f"No session matching '{args.session_id}' found.")
        else:
            print("No sessions to scan.")
        sys.exit(0)

    scan_size = sum(sf.size_bytes for _, sf in all_session_files)
    provider_label = "/".join(p.display_name for p in providers)

    if not args.json_output:
        print(f"llm-relay v{__version__} [{provider_label}] -- scanning {_format_size(scan_size)} ...")

    # Parse sessions
    parsed_sessions = [prov.parse_session(sf.path) for prov, sf in all_session_files]

    report = analyze_all(parsed_sessions, total_sessions=total)

    if args.json_output:
        print(JsonFormatter().format(report))
    else:
        print(PlainFormatter(verbose=args.verbose).format(report))

    sys.exit(report.exit_code)
