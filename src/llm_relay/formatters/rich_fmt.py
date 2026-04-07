"""Rich console formatter — requires click+rich (optional [cli] extra)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from llm_relay.formatters.base import BaseFormatter
from llm_relay.detect.models import FullReport, Health, Severity

if TYPE_CHECKING:
    pass


SEVERITY_COLORS = {
    Severity.CRITICAL: "bold red",
    Severity.WARN: "bold yellow",
    Severity.INFO: "dim",
}

HEALTH_COLORS = {
    Health.UNHEALTHY: "bold red",
    Health.DEGRADED: "bold yellow",
    Health.HEALTHY: "bold green",
}


class RichFormatter(BaseFormatter):
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose

    def format(self, report: FullReport) -> str:
        """Return empty string — use print_report() for rich output."""
        return ""

    def print_report(self, report: FullReport) -> None:
        from rich.console import Console
        from rich.text import Text
        from rich.tree import Tree

        console = Console()

        # Header
        console.print(f"\n[bold]llm-relay v{report.relay_version}[/bold] — AI CLI Session Health Check")
        console.print(f"Scanned {report.sessions_scanned} of {report.total_sessions} sessions.\n")

        # Summary
        parts = []
        if report.healthy_count:
            parts.append(f"[green]{report.healthy_count} healthy[/green]")
        if report.degraded_count:
            parts.append(f"[yellow]{report.degraded_count} degraded[/yellow]")
        if report.unhealthy_count:
            parts.append(f"[red]{report.unhealthy_count} unhealthy[/red]")
        console.print(f"Overall: {', '.join(parts)}\n")

        # Session details
        for health_filter in [Health.UNHEALTHY, Health.DEGRADED, Health.HEALTHY]:
            for sr in report.session_reports:
                if sr.health != health_filter:
                    continue
                if sr.health == Health.HEALTHY and not self.verbose:
                    continue

                s = sr.session
                sid = s.session_id[:8]
                date = s.first_timestamp[:10] if s.first_timestamp else "?"
                size = (
                    f"{s.file_size_bytes / 1_048_576:.1f} MB"
                    if s.file_size_bytes >= 1_048_576
                    else f"{s.file_size_bytes / 1024:.1f} KB"
                )

                health_color = HEALTH_COLORS.get(sr.health, "white")
                label = Text()
                prov_tag = f" [{s.provider}]" if s.provider != "claude-code" else ""
                label.append(sr.health.value.upper(), style=health_color)
                label.append(f"{prov_tag}  {sid}  ({date}, {size}, {s.entry_count} entries)")

                if not sr.findings:
                    console.print(label)
                    ratio = sr.cache_read_ratio
                    if ratio is not None:
                        console.print(f"   Cache read ratio: {ratio:.0%} — No issues detected.\n")
                    continue

                tree = Tree(label)
                for finding in sr.findings:
                    sev_color = SEVERITY_COLORS.get(finding.severity, "white")
                    node_text = Text()
                    node_text.append(finding.severity.value.upper(), style=sev_color)
                    node_text.append(f"  {finding.title}")

                    branch = tree.add(node_text)
                    branch.add(Text(finding.detail, style="dim"))
                    if finding.recommendation:
                        branch.add(Text(f"→ {finding.recommendation}", style="italic"))
                    if finding.bug_ref:
                        branch.add(Text(f"Ref: {finding.bug_ref}", style="dim cyan"))

                console.print(tree)
                console.print()

        # Healthy count summary
        if report.healthy_count > 0 and not self.verbose:
            n = report.healthy_count
            s = "s" if n != 1 else ""
            console.print(f"  [green]{n} healthy session{s}[/green] (use --verbose to show)\n")

        # Global findings
        if report.global_findings:
            console.print("[bold]— Global —[/bold]")
            for f in report.global_findings:
                sev_color = SEVERITY_COLORS.get(f.severity, "white")
                console.print(f"  [{sev_color}]{f.severity.value.upper()}[/{sev_color}]  {f.title}")
                console.print(f"  {f.detail}", style="dim")
                for ev in f.evidence[:3]:
                    console.print(f"    {ev}", style="dim")
            console.print()

        # Footer
        console.print("[dim]This tool is READ-ONLY. No session files were modified.[/dim]\n")
