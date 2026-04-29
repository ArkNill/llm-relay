"""CLI entry point for cc-relay."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table


def cmd_serve(args):
    """Start the proxy server."""
    import logging
    import os

    import uvicorn

    os.environ.setdefault("LLM_RELAY_UPSTREAM", args.upstream)
    os.environ.setdefault("LLM_RELAY_WARN_RATIO", str(args.warn_ratio))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    console = Console()
    console.print(f"[bold green]cc-relay[/] starting on :{args.port}")
    console.print(f"  upstream: {args.upstream}")
    console.print(f"  warn ratio: <{args.warn_ratio}%")
    console.print("  db: ~/.cc-relay/usage.db")
    console.print()
    console.print("[dim]Set in Claude Code:[/]")
    console.print(f"  [bold]ANTHROPIC_BASE_URL=http://localhost:{args.port}[/]")
    console.print()

    uvicorn.run(
        "llm_relay.proxy.proxy:app",
        host=args.host,
        port=args.port,
        log_level="warning",
    )


def cmd_stats(args):
    """Show session summaries."""
    from .db import DEFAULT_DB, get_conn, get_session_summary

    if not DEFAULT_DB.exists():
        print("No data yet. Start the proxy first.")
        return

    conn = get_conn()
    summaries = get_session_summary(conn, window_hours=args.window)

    if not summaries:
        print(f"No requests in the last {args.window}h.")
        return

    console = Console()
    table = Table(title=f"Sessions (last {args.window}h)")
    table.add_column("Session", style="dim", max_width=12)
    table.add_column("Turns", justify="right")
    table.add_column("Input", justify="right")
    table.add_column("Output", justify="right")
    table.add_column("Cache Create", justify="right")
    table.add_column("Cache Read", justify="right")
    table.add_column("Read %", justify="right")
    table.add_column("Status", justify="center")
    table.add_column("Last", style="dim")

    for s in summaries:
        ratio = s["avg_read_ratio"] or 0
        status = "[green]healthy[/]" if ratio >= 70 else "[red]poor[/]" if ratio < 40 else "[yellow]warn[/]"
        sid = (s["session_id"] or "?")[:12]
        last = datetime.fromtimestamp(s["last_ts"]).strftime("%H:%M:%S") if s["last_ts"] else "?"

        table.add_row(
            sid,
            str(s["turns"]),
            f"{s['total_input'] or 0:,}",
            f"{s['total_output'] or 0:,}",
            f"{s['total_creation'] or 0:,}",
            f"{s['total_read'] or 0:,}",
            f"{ratio:.1f}%",
            status,
            last,
        )

    console.print(table)


def cmd_recent(args):
    """Show recent requests."""
    from .db import DEFAULT_DB, get_conn, get_recent

    if not DEFAULT_DB.exists():
        print("No data yet. Start the proxy first.")
        return

    conn = get_conn()
    rows = get_recent(conn, limit=args.limit)

    if not rows:
        print("No recent requests.")
        return

    console = Console()
    table = Table(title=f"Recent {len(rows)} requests")
    table.add_column("Time", style="dim")
    table.add_column("Model", max_width=20)
    table.add_column("In", justify="right")
    table.add_column("Out", justify="right")
    table.add_column("C.Create", justify="right")
    table.add_column("C.Read", justify="right")
    table.add_column("Read %", justify="right")
    table.add_column("Latency", justify="right")
    table.add_column("Stream", justify="center")

    for r in rows:
        ts = datetime.fromtimestamp(r["ts"]).strftime("%H:%M:%S")
        ratio = r["read_ratio"] or 0
        style_ratio = "green" if ratio >= 70 else "red" if ratio < 40 else "yellow"

        table.add_row(
            ts,
            r["model"] or "?",
            f"{r['input_tokens']:,}",
            f"{r['output_tokens']:,}",
            f"{r['cache_creation']:,}",
            f"{r['cache_read']:,}",
            f"[{style_ratio}]{ratio:.1f}%[/]",
            f"{r['latency_ms']:.0f}ms",
            "✓" if r["is_stream"] else "",
        )

    console.print(table)


def cmd_prune(args):
    """Prune a session JSONL file."""
    from pathlib import Path

    from .pruner import PruneConfig, prune_session_file

    if args.path == "--latest":
        path = _find_latest_session()
        if path is None:
            print("No session JSONL files found.")
            return
    else:
        path = Path(args.path)

    if not path.exists():
        print(f"File not found: {path}")
        return

    config = PruneConfig(
        tier=args.tier,
        thinking_mode=args.thinking_mode,
        tool_output_max_chars=args.tool_max,
        max_age_turns=args.max_age,
        mega_block_chars=args.mega_threshold,
    )

    output_path = Path(args.output) if args.output else None
    dry_run = not args.execute

    try:
        report = prune_session_file(
            path, tier=args.tier, config=config,
            dry_run=dry_run, output_path=output_path,
        )
    except BlockingIOError:
        print("Another process is pruning this file. Try again later.")
        return
    except RuntimeError as e:
        print(f"Conflict: {e}")
        return

    console = Console()
    console.print(report.summary())

    if dry_run:
        console.print("\n[dim]Dry run -- no changes written. Use --execute to apply.[/]")
    elif output_path:
        console.print(f"\n[green]Written to {output_path}[/]")
    else:
        console.print(f"\n[green]Pruned in place: {path}[/]")


def cmd_strategies(args):
    """List registered pruning strategies."""
    from .strategies import TIER_ORDER, get_strategies

    console = Console()
    table = Table(title="Pruning Strategies")
    table.add_column("Tier", style="bold")
    table.add_column("Name")
    table.add_column("Description")
    table.add_column("Est. Savings", justify="right")

    for tier in TIER_ORDER:
        for s in get_strategies(tier):
            tier_style = {"gentle": "green", "standard": "yellow", "aggressive": "red"}
            table.add_row(
                f"[{tier_style.get(tier, '')}]{tier}[/]",
                s.name,
                s.description,
                s.estimated_savings or "--",
            )

    console.print(table)


def _find_latest_session() -> Optional[Path]:
    """Find the most recently modified session JSONL in ~/.claude/projects/."""
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return None

    candidates = sorted(base.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def cmd_cost(args):
    """Show estimated cost per session."""
    from .cost import estimate_cost
    from .db import DEFAULT_DB, get_conn

    if not DEFAULT_DB.exists():
        print("No data yet. Start the proxy first.")
        return

    conn = get_conn()
    cutoff = time.time() - args.window * 3600

    rows = conn.execute(
        """SELECT session_id, model, input_tokens, output_tokens,
                  cache_creation, cache_read
           FROM requests WHERE ts > ? ORDER BY ts""",
        (cutoff,),
    ).fetchall()

    if not rows:
        print(f"No requests in the last {args.window}h.")
        return

    # Aggregate by session
    sessions: dict[str, list] = {}
    for r in rows:
        sid = r["session_id"] or "unknown"
        sessions.setdefault(sid, []).append(dict(r))

    console = Console()
    table = Table(title=f"Estimated Costs (last {args.window}h)")
    table.add_column("Session", style="dim", max_width=12)
    table.add_column("Turns", justify="right")
    table.add_column("Input $", justify="right")
    table.add_column("Output $", justify="right")
    table.add_column("Cache $", justify="right")
    table.add_column("Total $", justify="right", style="bold")

    grand_total = 0.0

    for sid, reqs in sessions.items():
        total_in = total_out = total_cache = 0.0
        for r in reqs:
            est = estimate_cost(
                r.get("model", ""),
                r.get("input_tokens", 0),
                r.get("output_tokens", 0),
                r.get("cache_creation", 0),
                r.get("cache_read", 0),
            )
            total_in += est.input_cost
            total_out += est.output_cost
            total_cache += est.cache_create_cost + est.cache_read_cost

        session_total = total_in + total_out + total_cache
        grand_total += session_total

        table.add_row(
            sid[:12],
            str(len(reqs)),
            f"${total_in:.4f}",
            f"${total_out:.4f}",
            f"${total_cache:.4f}",
            f"${session_total:.4f}",
        )

    console.print(table)
    console.print(f"\n[bold]Grand total: ${grand_total:.4f}[/]")


def cmd_guard(args):
    """Show guard daemon status (or start standalone)."""
    from .guard import Guard, GuardConfig

    guard = Guard(GuardConfig(enabled=True, mode=args.mode))
    console = Console()
    console.print("[bold]cc-relay guard[/] -- context monitor")
    console.print(f"  mode: {args.mode}")
    console.print("  thresholds: 25% checkpoint / 55% gentle / 80% standard / 90% aggressive")
    console.print("\n[dim]Guard runs automatically when proxy is started with LLM_RELAY_GUARD=1[/]")
    console.print("[dim]Example: LLM_RELAY_GUARD=1 cc-relay serve[/]")

    statuses = guard.get_all_status()
    if statuses:
        for s in statuses:
            console.print(f"  {s['session_id'][:12]}: {s['context_pct']}% ({s['turn_count']} turns)")
    else:
        console.print("\n[dim]No active sessions tracked.[/]")


def cmd_watch(args):
    """Live tail of requests."""
    from .db import DEFAULT_DB, get_conn

    if not DEFAULT_DB.exists():
        print("No data yet. Start the proxy first.")
        return

    conn = get_conn()
    console = Console()
    last_id = 0

    # Get max id
    row = conn.execute("SELECT MAX(id) as max_id FROM requests").fetchone()
    if row and row["max_id"]:
        last_id = row["max_id"]

    console.print("[bold]cc-relay watch[/] -- Ctrl+C to stop\n")

    try:
        while True:
            rows = conn.execute(
                "SELECT * FROM requests WHERE id > ? ORDER BY id", (last_id,)
            ).fetchall()

            for r in rows:
                last_id = r["id"]
                ts = datetime.fromtimestamp(r["ts"]).strftime("%H:%M:%S")
                ratio = r["read_ratio"] or 0
                tag = "✓" if ratio >= 70 else "⚠" if ratio >= 40 else "✗"
                stream = "SSE" if r["is_stream"] else "   "

                console.print(
                    f"[dim]{ts}[/] {stream} "
                    f"in={r['input_tokens']:>6,} out={r['output_tokens']:>6,} "
                    f"create={r['cache_creation']:>7,} read={r['cache_read']:>7,} "
                    f"ratio={ratio:5.1f}% {tag} "
                    f"[dim]{r['latency_ms']:.0f}ms {r['model'] or ''}[/]"
                )

            time.sleep(1)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/]")


def main():
    parser = argparse.ArgumentParser(
        prog="cc-relay",
        description="Transparent API proxy for Claude Code -- cache monitoring + cost tracking",
    )
    sub = parser.add_subparsers(dest="command")

    # serve
    p_serve = sub.add_parser("serve", help="Start proxy server")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--upstream", default="https://api.anthropic.com")
    p_serve.add_argument("--warn-ratio", type=float, default=50.0)

    # stats
    p_stats = sub.add_parser("stats", help="Show session summaries")
    p_stats.add_argument("--window", type=float, default=8, help="Hours to look back")

    # recent
    p_recent = sub.add_parser("recent", help="Show recent requests")
    p_recent.add_argument("--limit", type=int, default=20)

    # watch
    sub.add_parser("watch", help="Live tail of requests")

    # prune
    p_prune = sub.add_parser("prune", help="Prune session JSONL file")
    p_prune.add_argument("path", nargs="?", default="--latest", help="Session JSONL path (default: latest)")
    p_prune.add_argument("--tier", choices=["gentle", "standard", "aggressive"], default="standard")
    p_prune.add_argument("--execute", action="store_true", help="Actually write changes (default: dry-run)")
    p_prune.add_argument("--output", help="Write to this path instead of in-place")
    p_prune.add_argument("--thinking-mode", choices=["remove", "truncate"], default="remove")
    p_prune.add_argument("--tool-max", type=int, default=5000, help="Max tool output chars")
    p_prune.add_argument("--max-age", type=int, default=30, help="Max turn age for tool results")
    p_prune.add_argument("--mega-threshold", type=int, default=20000, help="Mega block char threshold")

    # strategies
    sub.add_parser("strategies", help="List pruning strategies")

    # cost
    p_cost = sub.add_parser("cost", help="Show estimated costs per session")
    p_cost.add_argument("--window", type=float, default=8, help="Hours to look back")

    # guard
    p_guard = sub.add_parser("guard", help="Guard daemon status")
    p_guard.add_argument("--mode", choices=["passive", "active"], default="passive")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    cmd = {
        "serve": cmd_serve, "stats": cmd_stats, "recent": cmd_recent,
        "watch": cmd_watch, "prune": cmd_prune, "strategies": cmd_strategies,
        "cost": cmd_cost, "guard": cmd_guard,
    }
    cmd[args.command](args)


if __name__ == "__main__":
    main()
