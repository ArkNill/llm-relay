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


@cli.command()
@click.option("--host", default="127.0.0.1", help="Proxy host.")
@click.option("--port", "-p", default=8083, type=int, help="Proxy port.")
@click.option("--refresh", "-r", default=2.0, type=float, help="Refresh interval in seconds.")
def top(host: str, port: int, refresh: float) -> None:
    """Live terminal monitor -- btop-style session dashboard."""
    try:
        from rich.console import Console
        from rich.live import Live
    except ImportError:
        click.echo("Error: rich not installed. Run: pip install llm-relay[cli]", err=True)
        raise SystemExit(1)

    import time

    from llm_relay.detect.tui import render_top

    console = Console()
    console.clear()

    try:
        with Live(render_top(host, port), console=console, refresh_per_second=1, screen=False) as live:
            while True:
                time.sleep(refresh)
                live.update(render_top(host, port))
    except KeyboardInterrupt:
        console.print("\n[grey62]  Stopped.[/grey62]")


@cli.command()
@click.option("--port", "-p", default=8083, type=int, help="Proxy port (default: 8083).")
@click.option("--skip-server", is_flag=True, help="Configure only, don't start the server.")
@click.option("--dry-run", is_flag=True, help="Show what would be done without making changes.")
def init(port: int, skip_server: bool, dry_run: bool) -> None:
    """One-command setup — detect CLIs, configure proxy, start server.

    \b
    What this does:
      1. Detect installed CLIs (Claude Code, Codex, Gemini)
      2. Initialize DB (~/.llm-relay/usage.db)
      3. Configure Claude Code to route through proxy
      4. Register llm-relay MCP server in Claude Code
      5. Start the proxy server (with history enabled)
      6. Verify everything works

    \b
    After running this, open the dashboard:
      http://localhost:8083/dashboard/
    """
    from llm_relay.setup_init import run_init

    if dry_run:
        click.echo("=== llm-relay init (dry run) ===\n")
    else:
        click.echo("=== llm-relay init ===\n")

    summary = run_init(port=port, skip_server=skip_server, dry_run=dry_run)

    # Display results
    click.echo("Version: {}".format(summary["version"]))
    click.echo()

    # CLIs detected
    click.echo("CLIs detected:")
    if summary["clis"]:
        for c in summary["clis"]:
            ver = " v{}".format(c["version"]) if c.get("version") else ""
            click.echo("  {} {}{}".format(
                click.style("OK", fg="green"), c["name"], ver,
            ))
    else:
        click.echo("  {} No AI CLI tools found".format(click.style("!!", fg="yellow")))
    click.echo()

    # DB
    click.echo("Database: {}".format(summary["db"]))

    # Config
    click.echo("Config:   {}".format(summary["config"]))
    click.echo()

    # Claude Code configuration
    click.echo("Claude Code configuration:")
    for action in summary["claude_code"]:
        if "skipped" in action.lower():
            click.echo("  {} {}".format(click.style("--", fg="yellow"), action))
        else:
            click.echo("  {} {}".format(click.style("OK", fg="green"), action))
    click.echo()

    # Server
    click.echo("Server: {}".format(summary["server"] or "not started"))

    # Health check
    if isinstance(summary["health"], dict):
        click.echo()
        click.echo("Health check:")
        all_ok = True
        for name, result in summary["health"].items():
            if result.get("ok"):
                click.echo("  {} {}".format(click.style("OK", fg="green"), name))
            else:
                click.echo("  {} {} — {}".format(
                    click.style("FAIL", fg="red"), name, result.get("error", ""),
                ))
                all_ok = False
        if all_ok:
            click.echo()
            click.echo(click.style("All checks passed.", fg="green", bold=True))

    # URLs
    if summary["urls"]:
        click.echo()
        click.echo("Ready! Open in browser:")
        click.echo("  Dashboard:  {}".format(summary["urls"]["dashboard"]))
        click.echo("  Display:    {}".format(summary["urls"]["display"]))
        click.echo("  History:    {}".format(summary["urls"]["history"]))
        click.echo()
        click.echo("Claude Code will automatically route through the proxy.")
        click.echo("To stop: kill the uvicorn process or press Ctrl+C.")

    if dry_run:
        click.echo()
        click.echo("(dry run — no changes were made)")


@cli.command()
@click.option("--port", "-p", default=8080, type=int, help="Proxy port (default: 8080).")
def connect(port: int) -> None:
    """Connect Claude Code, Codex, and Gemini to the llm-relay proxy.

    \b
    Sets ANTHROPIC_BASE_URL / OPENAI_BASE_URL in each CLI's settings
    so API requests route through the proxy. Does NOT start a server.
    Run 'llm-relay disconnect' to undo.
    """
    import json

    base_url = "http://localhost:{}".format(port)
    connected = []

    # Claude Code — ~/.claude/settings.json → env.ANTHROPIC_BASE_URL
    cc_settings = Path.home() / ".claude" / "settings.json"
    if (Path.home() / ".claude").exists():
        data = {}
        if cc_settings.exists():
            try:
                data = json.loads(cc_settings.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
        env = data.get("env", {})
        if not isinstance(env, dict):
            env = {}
        env["ANTHROPIC_BASE_URL"] = base_url
        data["env"] = env
        cc_settings.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        connected.append(("Claude Code", str(cc_settings), "ANTHROPIC_BASE_URL"))

    # Codex CLI — ~/.codex/settings.json → env.OPENAI_BASE_URL
    codex_dir = Path.home() / ".codex"
    if codex_dir.exists():
        codex_settings = codex_dir / "settings.json"
        data = {}
        if codex_settings.exists():
            try:
                data = json.loads(codex_settings.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
        env = data.get("env", {})
        if not isinstance(env, dict):
            env = {}
        env["OPENAI_BASE_URL"] = base_url
        data["env"] = env
        codex_settings.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        connected.append(("Codex CLI", str(codex_settings), "OPENAI_BASE_URL"))

    # Gemini CLI — ~/.gemini/settings.json → env.GEMINI_API_BASE_URL
    gemini_dir = Path.home() / ".gemini"
    if gemini_dir.exists():
        gemini_settings = gemini_dir / "settings.json"
        data = {}
        if gemini_settings.exists():
            try:
                data = json.loads(gemini_settings.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
        env = data.get("env", {})
        if not isinstance(env, dict):
            env = {}
        env["GEMINI_API_BASE_URL"] = base_url
        data["env"] = env
        gemini_settings.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        connected.append(("Gemini CLI", str(gemini_settings), "GEMINI_API_BASE_URL"))

    if connected:
        click.echo("Connected to proxy at {}:".format(base_url))
        for name, path, var in connected:
            click.echo("  {} {} ({} in {})".format(
                click.style("OK", fg="green"), name, var, path,
            ))
        click.echo()
        click.echo("To undo: llm-relay disconnect")
    else:
        click.echo(click.style("No CLI config directories found.", fg="yellow"))


@cli.command()
def disconnect() -> None:
    """Disconnect Claude Code, Codex, and Gemini from the llm-relay proxy.

    \b
    Removes ANTHROPIC_BASE_URL / OPENAI_BASE_URL from each CLI's settings.
    CLIs will revert to direct API connections.
    """
    import json

    disconnected = []

    # Claude Code — remove ANTHROPIC_BASE_URL
    cc_settings = Path.home() / ".claude" / "settings.json"
    if cc_settings.exists():
        try:
            data = json.loads(cc_settings.read_text(encoding="utf-8"))
            env = data.get("env", {})
            if isinstance(env, dict) and "ANTHROPIC_BASE_URL" in env:
                del env["ANTHROPIC_BASE_URL"]
                if not env:
                    del data["env"]
                else:
                    data["env"] = env
                cc_settings.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                disconnected.append(("Claude Code", str(cc_settings)))
        except (json.JSONDecodeError, OSError):
            click.echo(click.style("  Warning: could not parse {}".format(cc_settings), fg="yellow"))

    # Codex CLI — remove OPENAI_BASE_URL
    codex_settings = Path.home() / ".codex" / "settings.json"
    if codex_settings.exists():
        try:
            data = json.loads(codex_settings.read_text(encoding="utf-8"))
            env = data.get("env", {})
            if isinstance(env, dict) and "OPENAI_BASE_URL" in env:
                del env["OPENAI_BASE_URL"]
                if not env:
                    del data["env"]
                else:
                    data["env"] = env
                codex_settings.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                disconnected.append(("Codex CLI", str(codex_settings)))
        except (json.JSONDecodeError, OSError):
            click.echo(click.style("  Warning: could not parse {}".format(codex_settings), fg="yellow"))

    # Gemini CLI — remove GEMINI_API_BASE_URL
    gemini_settings = Path.home() / ".gemini" / "settings.json"
    if gemini_settings.exists():
        try:
            data = json.loads(gemini_settings.read_text(encoding="utf-8"))
            env = data.get("env", {})
            if isinstance(env, dict) and "GEMINI_API_BASE_URL" in env:
                del env["GEMINI_API_BASE_URL"]
                if not env:
                    del data["env"]
                else:
                    data["env"] = env
                gemini_settings.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                disconnected.append(("Gemini CLI", str(gemini_settings)))
        except (json.JSONDecodeError, OSError):
            click.echo(click.style("  Warning: could not parse {}".format(gemini_settings), fg="yellow"))

    if disconnected:
        click.echo("Disconnected from proxy:")
        for name, path in disconnected:
            click.echo("  {} {} ({})".format(
                click.style("OK", fg="green"), name, path,
            ))
        click.echo()
        click.echo("CLIs will now connect directly to their APIs.")
    else:
        click.echo("Nothing to disconnect (no proxy settings found).")


def main() -> None:
    """Entry point."""
    cli()
