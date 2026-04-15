"""Click-based CLI -- requires [cli] extra (click + rich)."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from llm_relay.detect import __version__
from llm_relay.detect.analyzer import analyze_all
from llm_relay.formatters.json_fmt import JsonFormatter
from llm_relay.providers import CLAUDE_CODE, detect_providers, get_provider, list_provider_ids


def _format_size(size_bytes: int) -> str:
    if size_bytes >= 1_048_576:
        return f"{size_bytes / 1_048_576:.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """AI CLI Session Health Check -- read-only diagnostics."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(scan)


@cli.command()
@click.option("--all", "-a", "scan_all", is_flag=True, help="Scan all sessions (default: last 10).")
@click.option("--last", "-n", "last_n", type=int, default=None, help="Scan last N sessions by modification time.")
@click.option("--session", "-s", "session_id", default=None, help="Scan specific session (prefix match).")
@click.option("--project", "-p", "project_filter", default=None, help="Filter by project directory name.")
@click.option(
    "--provider",
    type=click.Choice([*list_provider_ids(), "all", "auto"], case_sensitive=False),
    default="auto",
    help="Which CLI tool to scan (default: auto-detect).",
)
@click.option("--json", "-j", "json_output", is_flag=True, help="Output as JSON.")
@click.option("--verbose", "-v", is_flag=True, help="Show all sessions including healthy.")
@click.option("--no-color", is_flag=True, help="Disable rich formatting.")
@click.version_option(__version__, prog_name="llm-relay")
def scan(
    scan_all: bool,
    last_n: int | None,
    session_id: str | None,
    project_filter: str | None,
    provider: str,
    json_output: bool,
    verbose: bool,
    no_color: bool,
) -> None:
    """AI CLI Session Health Check -- read-only diagnostics."""
    # Determine limit
    limit = None
    if session_id:
        limit = None
    elif scan_all:
        limit = None
    elif last_n:
        limit = last_n
    else:
        limit = 10

    # Resolve providers
    if provider == "auto":
        providers = detect_providers()
        if not providers:
            # Fall back to Claude Code for legacy behavior
            providers = [get_provider(CLAUDE_CODE)]
    elif provider == "all":
        providers = [get_provider(pid) for pid in list_provider_ids()]
    else:
        providers = [get_provider(provider)]

    # Discover sessions across all providers
    all_session_files = []
    for prov in providers:
        all_session_files.extend(
            (prov, sf) for sf in prov.discover_sessions(project_filter=project_filter)
        )

    total = len(all_session_files)

    if total == 0:
        provider_names = ", ".join(p.display_name for p in providers)
        click.echo(f"No sessions found for: {provider_names}")
        click.echo("Make sure the CLI tool has been used at least once.")
        sys.exit(0)

    # Apply limit and session filter
    # Sort all sessions by mtime descending
    all_session_files.sort(key=lambda x: x[1].mtime, reverse=True)

    if session_id:
        all_session_files = [(p, sf) for p, sf in all_session_files if sf.session_id.startswith(session_id)]
    elif limit is not None:
        all_session_files = all_session_files[:limit]

    if not all_session_files:
        if session_id:
            click.echo(f"No session matching '{session_id}' found.")
        else:
            click.echo("No sessions to scan.")
        sys.exit(0)

    scan_size = sum(sf.size_bytes for _, sf in all_session_files)

    if not json_output and not no_color:
        provider_label = "/".join(p.display_name for p in providers)
        try:
            from rich.console import Console

            console = Console()
            console.print(
                f"\n[bold]llm-relay v{__version__}[/bold] [{provider_label}] "
                f"-- scanning {_format_size(scan_size)} ..."
            )
        except ImportError:
            click.echo(f"llm-relay v{__version__} [{provider_label}] -- scanning {_format_size(scan_size)} ...")

    # Parse sessions
    parsed_sessions = [prov.parse_session(sf.path) for prov, sf in all_session_files]

    # Analyze
    report = analyze_all(parsed_sessions, total_sessions=total)

    # Format output
    if json_output:
        click.echo(JsonFormatter().format(report))
    elif no_color:
        from llm_relay.formatters.plain import PlainFormatter

        click.echo(PlainFormatter(verbose=verbose).format(report))
    else:
        try:
            from llm_relay.formatters.rich_fmt import RichFormatter

            RichFormatter(verbose=verbose).print_report(report)
        except ImportError:
            from llm_relay.formatters.plain import PlainFormatter

            click.echo(PlainFormatter(verbose=verbose).format(report))

    sys.exit(report.exit_code)


@cli.command()
@click.argument("session_path", required=False)
@click.option("--format", "-f", "fmt", type=click.Choice(["handoff", "actions", "full"]), default="handoff")
def recover(session_path: str | None, fmt: str) -> None:
    """Extract session context for resumption in a new session."""
    from llm_relay.recover.recover import extract_context, format_actions, format_full, format_handoff

    if session_path is None:
        # Find latest session
        base = Path.home() / ".claude" / "projects"
        if not base.exists():
            click.echo("No sessions found.")
            sys.exit(1)
        candidates = sorted(base.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            click.echo("No session JSONL files found.")
            sys.exit(1)
        path = candidates[0]
    else:
        path = Path(session_path)

    if not path.exists():
        click.echo(f"File not found: {path}")
        sys.exit(1)

    ctx = extract_context(path)
    formatters = {"handoff": format_handoff, "actions": format_actions, "full": format_full}
    click.echo(formatters[fmt](ctx))


@cli.command()
@click.option("--fix", is_flag=True, help="Attempt to fix issues (not yet implemented).")
def doctor(fix: bool) -> None:
    """Run health checks on Claude Code configuration and sessions."""
    from llm_relay.recover.doctor import run_doctor

    report = run_doctor(fix=fix)

    try:
        from rich.console import Console
        from rich.table import Table

        console = Console()
        table = Table(title="Doctor Report")
        table.add_column("Check")
        table.add_column("Status")
        table.add_column("Detail")
        table.add_column("Recommendation")

        for r in report.results:
            status_style = {"ok": "green", "warning": "yellow", "issue": "red"}
            table.add_row(
                r.name,
                f"[{status_style.get(r.status, '')}]{r.status}[/]",
                r.detail,
                r.recommendation or "--",
            )

        console.print(table)

        if report.issues:
            console.print(f"\n[red bold]{len(report.issues)} issue(s) found.[/]")
        elif report.warnings:
            console.print(f"\n[yellow]{len(report.warnings)} warning(s).[/]")
        else:
            console.print("\n[green]All checks passed.[/]")
    except ImportError:
        for r in report.results:
            click.echo(f"[{r.status:7s}] {r.name}: {r.detail}")
            if r.recommendation:
                click.echo(f"          -> {r.recommendation}")


@cli.command()
@click.option("--host", default="0.0.0.0", help="Bind address.")
@click.option("--port", "-p", default=8083, type=int, help="Listen port.")
@click.option("--workers", "-w", default=1, type=int, help="Number of worker processes.")
def serve(host: str, port: int, workers: int) -> None:
    """Start the proxy server with dashboard and display pages."""
    try:
        import uvicorn
    except ImportError:
        click.echo("Error: uvicorn not installed. Run: pip install llm-relay[proxy]", err=True)
        raise SystemExit(1)

    click.echo(f"llm-relay v{__version__} -- starting on http://{host}:{port}")
    click.echo("  /dashboard/  -- CLI status, cost, delegation history")
    click.echo("  /display/    -- turn counter with CC/Codex/Gemini sessions")
    click.echo(f"  Proxy:       ANTHROPIC_BASE_URL=http://localhost:{port}")
    click.echo()
    uvicorn.run(
        "llm_relay.proxy.proxy:app",
        host=host,
        port=port,
        workers=workers,
        log_level="info",
    )


def main() -> None:
    """Entry point."""
    cli()
