"""Terminal UI renderer for `llm-relay top` -- btop-style session monitor.

Fetches /api/v1/display and renders Rich panels for each active session.
Designed for SSH environments where a browser is not available.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from rich.console import Group
from rich.panel import Panel
from rich.text import Text

# Zone → Rich color mapping
ZONE_COLORS = {
    "green": "green",
    "yellow": "yellow",
    "orange": "dark_orange",
    "red": "red",
    "hard": "bold red",
}

# Composition category colors (matching web display)
COMP_COLORS = {
    "user_text": "dodger_blue2",
    "assistant_text": "green",
    "tool_use": "dark_orange",
    "tool_result": "red",
    "thinking_overhead": "grey62",
    "system": "grey42",
}

COMP_LABELS = {
    "user_text": "User",
    "assistant_text": "Asst",
    "tool_use": "Call",
    "tool_result": "Result",
    "thinking_overhead": "Think",
    "system": "Sys",
}


def _fmt_tokens(n: int) -> str:
    if not n:
        return "0"
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}K" if n < 100_000 else f"{n // 1000}K"
    return f"{n / 1_000_000:.2f}M"


def _fmt_duration(seconds: float) -> str:
    if not seconds or seconds < 60:
        return f"{int(seconds or 0)}s"
    m = int(seconds) // 60
    if m < 60:
        return f"{m}m"
    h = m // 60
    return f"{h}h{m % 60}m"


def _zone_style(zone: str) -> str:
    return ZONE_COLORS.get(zone, "white")


def _render_session_panel(s: Dict[str, Any]) -> Panel:
    """Render a single session as a Rich Panel."""
    lines = Text()

    # ── Prompt (first line, truncated) ──
    prompt = s.get("last_prompt", "")
    if prompt:
        prompt_display = prompt[:100].replace("\n", " ")
        if len(prompt) > 100:
            prompt_display += "..."
        lines.append(f'"{prompt_display}"', style="italic grey70")
        lines.append("\n\n")

    # ── Token metrics ──
    current = s.get("current_ctx", 0)
    peak = s.get("peak_ctx", 0)
    ceiling = s.get("ceiling", 1_000_000)
    recent = s.get("recent_peak", 0)
    cumul = s.get("cumul_unique", 0)
    zone = s.get("zone", "green")
    zone_a = s.get("zone_a", "green")
    zone_b = s.get("zone_b", "green")

    # Current line
    lines.append("Current  ", style="grey62")
    lines.append(f"{_fmt_tokens(current)}", style=f"bold {_zone_style(zone)}")
    lines.append(f" / {_fmt_tokens(ceiling)}  ", style="grey42")
    lines.append("A:", style="grey62")
    lines.append(f"{zone_a.title()}", style=_zone_style(zone_a))
    lines.append("  ", style="grey42")
    lines.append("B:", style="grey62")
    lines.append(f"{zone_b.title()}", style=_zone_style(zone_b))

    # Peak on same line
    lines.append("     Peak  ", style="grey62")
    lines.append(f"{_fmt_tokens(peak)}", style="grey70")
    lines.append("\n")

    # Secondary metrics
    lines.append("Recent5  ", style="grey62")
    lines.append(f"{_fmt_tokens(recent)}", style="grey70")
    lines.append("        Cumul  ", style="grey62")
    lines.append(f"{_fmt_tokens(cumul)}", style="grey70")
    lines.append("\n")

    # ── Composition ──
    comp = s.get("composition")
    if comp and comp.get("categories"):
        cats = comp["categories"]
        lines.append("\n")

        order = ["user_text", "assistant_text", "tool_use", "tool_result", "thinking_overhead"]
        for cat in order:
            pct = cats.get(cat, {}).get("pct", 0)
            if pct < 0.5:
                continue
            label = COMP_LABELS.get(cat, cat)
            style = COMP_COLORS.get(cat, "white")
            # Highlight dangerous values
            if cat == "tool_result" and pct > 50:
                style = "bold red"
            lines.append(f"{label} ", style=f"bold {style}")
            lines.append(f"{pct:.0f}%  ", style="grey70")

        lines.append("\n")

        snr = comp.get("snr", 0)
        dupes = comp.get("duplicate_read_count", 0)
        snr_style = "dark_orange" if snr < 0.5 else "grey70"
        lines.append("  SNR ", style="grey62")
        lines.append(f"{snr:.2f}", style=snr_style)

        if dupes > 0:
            lines.append(f" · {dupes} dupes", style="grey62")

    # Duration
    duration = s.get("duration_s", 0)
    if duration:
        lines.append(f" · {_fmt_duration(duration)}", style="grey62")

    # ── Panel title ──
    provider = s.get("provider_name", s.get("provider", ""))
    sid_short = s.get("session_id", "")[:8]
    conn = s.get("connection_type", "")
    turns = s.get("turns", 0)

    title_parts = [provider, sid_short]
    if conn and conn != "unknown":
        title_parts.append(conn)
    title = " · ".join(title_parts)
    subtitle = f"{turns} turns"

    border_style = _zone_style(zone)

    return Panel(
        lines,
        title=f"[bold]{title}[/bold]",
        subtitle=subtitle,
        border_style=border_style,
        padding=(0, 1),
    )


def fetch_display_data(host: str, port: int) -> Optional[dict]:
    """Fetch /api/v1/display data from the proxy."""
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"http://{host}:{port}/api/v1/display?window=4")
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError:
        return None
    except Exception:
        return None


def render_top(host: str, port: int) -> Group:
    """Render the full TUI display as a Rich Group."""
    data = fetch_display_data(host, port)
    now = datetime.now().strftime("%H:%M:%S")

    if data is None:
        header = Text()
        header.append(" llm-relay top", style="bold dodger_blue2")
        header.append(f"  ·  proxy not reachable at {host}:{port}", style="red")
        header.append(f"  ·  {now}\n", style="grey62")

        msg = Text()
        msg.append("\n  Start the proxy first:\n\n", style="grey70")
        msg.append("    llm-relay serve", style="bold green")
        msg.append(f"  --port {port}\n\n", style="grey62")
        msg.append("  Ctrl+C to quit\n", style="grey42")

        return Group(header, msg)

    sessions = data.get("sessions", [])

    header = Text()
    header.append(" llm-relay top", style="bold dodger_blue2")
    count = len(sessions)
    header.append(f"  ·  {count} session{'s' if count != 1 else ''}", style="grey70")
    header.append(f"  ·  {now}\n", style="grey62")

    if not sessions:
        empty = Text("\n  No active sessions\n", style="grey62")
        footer = Text("\n  Ctrl+C to quit\n", style="grey42")
        return Group(header, empty, footer)

    panels: List[Panel] = [_render_session_panel(s) for s in sessions]
    footer = Text("\n  Ctrl+C to quit\n", style="grey42")

    return Group(header, *panels, footer)
