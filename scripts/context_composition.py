#!/usr/bin/env python3
"""Context composition analyzer for llm-relay session history.

Analyzes what fills up the context window across session turns,
classifying content into: system, user_text, assistant_text,
tool_use, tool_result, thinking_overhead.

Reads from ~/.llm-relay/usage.db (conversation_turns + compaction_events).

Usage:
    python context_composition.py                     # all sessions summary
    python context_composition.py SESSION_ID          # single session detail
    python context_composition.py --top 5             # top 5 by context size
    python context_composition.py SESSION_ID --json   # JSON output
    python context_composition.py --turns SESSION_ID  # per-turn breakdown
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DB_PATH = Path(os.environ.get("LLM_RELAY_DB", Path.home() / ".llm-relay" / "usage.db"))

# ── Content categories ──

CATEGORIES = [
    "system",
    "user_text",
    "assistant_text",
    "tool_use",
    "tool_result",
    "thinking_overhead",
]


@dataclass
class Composition:
    """Byte counts per content category for a single turn's full context."""

    system: int = 0
    user_text: int = 0
    assistant_text: int = 0
    tool_use: int = 0
    tool_result: int = 0
    thinking_overhead: int = 0

    @property
    def total(self) -> int:
        return (
            self.system
            + self.user_text
            + self.assistant_text
            + self.tool_use
            + self.tool_result
            + self.thinking_overhead
        )

    def pct(self, cat: str) -> float:
        t = self.total
        if t == 0:
            return 0.0
        return getattr(self, cat) / t * 100

    def to_dict(self) -> Dict[str, Any]:
        t = self.total
        return {
            "total_bytes": t,
            "est_tokens": t // 4,
            "categories": {
                cat: {"bytes": getattr(self, cat), "pct": round(self.pct(cat), 1)}
                for cat in CATEGORIES
            },
        }


@dataclass
class TurnAnalysis:
    """Analysis result for a single turn."""

    turn_number: int
    storage_mode: str
    message_count: int
    composition: Composition
    compacted: bool = False


@dataclass
class SessionAnalysis:
    """Analysis result for a full session."""

    session_id: str
    total_turns: int
    compaction_count: int
    turns: List[TurnAnalysis] = field(default_factory=list)
    duplicate_reads: Dict[str, int] = field(default_factory=dict)

    @property
    def final_composition(self) -> Optional[Composition]:
        return self.turns[-1].composition if self.turns else None

    @property
    def peak_bytes(self) -> int:
        return max((t.composition.total for t in self.turns), default=0)

    @property
    def tool_result_peak_pct(self) -> float:
        return max(
            (t.composition.pct("tool_result") for t in self.turns), default=0.0
        )


# ── Message classification ──


def _block_size(block: Any) -> int:
    """Estimate the serialized size of a content block."""
    if isinstance(block, str):
        return len(block.encode("utf-8"))
    return len(json.dumps(block, ensure_ascii=False).encode("utf-8"))


def _classify_content_blocks(content: Any) -> Dict[str, int]:
    """Classify content (str or list of blocks) into categories with sizes."""
    sizes = defaultdict(int)  # type: Dict[str, int]

    if isinstance(content, str):
        sizes["text"] += len(content.encode("utf-8"))
        return dict(sizes)

    if not isinstance(content, list):
        sizes["text"] += _block_size(content)
        return dict(sizes)

    for block in content:
        if not isinstance(block, dict):
            sizes["text"] += _block_size(block)
            continue

        btype = block.get("type", "text")

        if btype == "thinking":
            # Content is encrypted/empty but signature takes space
            sizes["thinking"] += _block_size(block)
        elif btype == "tool_use":
            sizes["tool_use"] += _block_size(block)
        elif btype == "tool_result":
            sizes["tool_result"] += _block_size(block)
        else:
            # text, image, etc.
            sizes["text"] += _block_size(block)

    return dict(sizes)


def _classify_message(msg: dict) -> Dict[str, int]:
    """Classify a single message into composition categories."""
    role = msg.get("role", "")
    content = msg.get("content", "")

    block_sizes = _classify_content_blocks(content)

    result = defaultdict(int)  # type: Dict[str, int]

    if role == "system":
        # Everything in system messages counts as system
        for v in block_sizes.values():
            result["system"] += v
    elif role == "user":
        # User messages contain text and tool_result blocks
        result["user_text"] += block_sizes.get("text", 0)
        result["tool_result"] += block_sizes.get("tool_result", 0)
        # Sometimes user messages wrap tool results
        result["tool_use"] += block_sizes.get("tool_use", 0)
    elif role == "assistant":
        result["assistant_text"] += block_sizes.get("text", 0)
        result["tool_use"] += block_sizes.get("tool_use", 0)
        result["thinking_overhead"] += block_sizes.get("thinking", 0)
    else:
        result["user_text"] += sum(block_sizes.values())

    return dict(result)


def _extract_read_targets(msg: dict) -> List[str]:
    """Extract file paths from Read/Bash tool_use blocks for duplicate detection."""
    targets = []
    content = msg.get("content", "")
    if not isinstance(content, list):
        return targets

    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        name = block.get("name", "")
        inp = block.get("input", {})
        if name == "Read" and isinstance(inp, dict):
            fp = inp.get("file_path", "")
            if fp:
                targets.append(fp)
        elif name == "Grep" and isinstance(inp, dict):
            pat = inp.get("pattern", "")
            path = inp.get("path", ".")
            if pat:
                targets.append(f"grep:{pat}@{path}")
    return targets


# ── Context reconstruction ──


def _reconstruct_and_analyze(
    turns_data: List[dict],
) -> Tuple[List[TurnAnalysis], Dict[str, int]]:
    """Reconstruct full context at each turn and analyze composition.

    Handles delta/full storage modes and compaction events.
    Returns (turn_analyses, duplicate_read_counts).
    """
    accumulated = []  # type: List[dict]
    analyses = []
    read_counts = defaultdict(int)  # type: Dict[str, int]

    for row in turns_data:
        turn_number = row["turn_number"]
        storage_mode = row["storage_mode"]
        req_json = row["request_messages"]

        if not req_json:
            continue

        try:
            messages = json.loads(req_json)
        except (json.JSONDecodeError, TypeError):
            continue

        if not isinstance(messages, list):
            continue

        compacted = False
        if storage_mode == "full":
            if accumulated and len(messages) < len(accumulated):
                compacted = True
            accumulated = list(messages)
        else:
            # delta mode: append new messages
            accumulated.extend(messages)

        # Track duplicate reads
        for msg in messages:
            for target in _extract_read_targets(msg):
                read_counts[target] += 1

        # Classify the full accumulated context
        comp = Composition()
        for msg in accumulated:
            sizes = _classify_message(msg)
            comp.system += sizes.get("system", 0)
            comp.user_text += sizes.get("user_text", 0)
            comp.assistant_text += sizes.get("assistant_text", 0)
            comp.tool_use += sizes.get("tool_use", 0)
            comp.tool_result += sizes.get("tool_result", 0)
            comp.thinking_overhead += sizes.get("thinking_overhead", 0)

        analyses.append(
            TurnAnalysis(
                turn_number=turn_number,
                storage_mode=storage_mode,
                message_count=len(accumulated),
                composition=comp,
                compacted=compacted,
            )
        )

    # Filter duplicate reads to only those read more than once
    dupes = {k: v for k, v in read_counts.items() if v > 1}
    return analyses, dupes


# ── Database queries ──


def _get_sessions(conn: sqlite3.Connection) -> List[dict]:
    """Get all sessions with basic stats."""
    cur = conn.execute(
        """
        SELECT
            session_id,
            COUNT(*) as turn_count,
            MIN(datetime(ts, 'unixepoch', 'localtime')) as started,
            MAX(datetime(ts, 'unixepoch', 'localtime')) as ended,
            MAX(request_size_bytes) as peak_request_bytes
        FROM conversation_turns
        GROUP BY session_id
        ORDER BY MIN(ts) DESC
        """
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _get_session_turns(conn: sqlite3.Connection, session_id: str) -> List[dict]:
    """Get all turns for a session ordered by turn_number."""
    cur = conn.execute(
        """
        SELECT turn_number, storage_mode, request_messages,
               total_message_count, request_size_bytes, response_size_bytes
        FROM conversation_turns
        WHERE session_id = ?
        ORDER BY turn_number
        """,
        (session_id,),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _get_compaction_count(conn: sqlite3.Connection, session_id: str) -> int:
    cur = conn.execute(
        "SELECT COUNT(*) FROM compaction_events WHERE session_id = ?",
        (session_id,),
    )
    return cur.fetchone()[0]


def analyze_session(conn: sqlite3.Connection, session_id: str) -> SessionAnalysis:
    """Full analysis of a single session."""
    turns_data = _get_session_turns(conn, session_id)
    compaction_count = _get_compaction_count(conn, session_id)
    turn_analyses, dupes = _reconstruct_and_analyze(turns_data)

    return SessionAnalysis(
        session_id=session_id,
        total_turns=len(turns_data),
        compaction_count=compaction_count,
        turns=turn_analyses,
        duplicate_reads=dupes,
    )


# ── Formatting ──

BAR_WIDTH = 40


def _bar(pcts: List[Tuple[str, float]], width: int = BAR_WIDTH) -> str:
    """Render a horizontal stacked bar from percentages."""
    symbols = {
        "system": "S",
        "user_text": "U",
        "assistant_text": "A",
        "tool_use": "C",  # Call
        "tool_result": "R",  # Result
        "thinking_overhead": "T",
    }
    bar_chars = []
    for cat, pct in pcts:
        n = max(0, round(pct / 100 * width))
        bar_chars.append(symbols.get(cat, "?") * n)
    result = "".join(bar_chars)
    # Pad or truncate to exact width
    return result[:width].ljust(width)


def _fmt_bytes(b: int) -> str:
    if b < 1024:
        return f"{b}B"
    if b < 1024 * 1024:
        return f"{b / 1024:.1f}K"
    return f"{b / (1024 * 1024):.1f}M"


def _fmt_tokens(b: int) -> str:
    tokens = b // 4
    if tokens < 1000:
        return f"{tokens}"
    return f"{tokens / 1000:.1f}K"


def print_session_summary(analysis: SessionAnalysis) -> None:
    """Print a concise session summary."""
    fc = analysis.final_composition
    if not fc:
        print(f"  {analysis.session_id[:12]}  (no data)")
        return

    pcts = [(cat, fc.pct(cat)) for cat in CATEGORIES]
    bar = _bar(pcts)

    print(
        f"  {analysis.session_id[:12]}  "
        f"turns={analysis.total_turns:>3}  "
        f"ctx={_fmt_tokens(fc.total):>6}tok  "
        f"peak={_fmt_tokens(analysis.peak_bytes):>6}tok  "
        f"compact={analysis.compaction_count:>2}  "
        f"[{bar}]"
    )

    # Show top categories
    top = sorted(CATEGORIES, key=lambda c: fc.pct(c), reverse=True)
    parts = [f"{c}={fc.pct(c):.0f}%" for c in top if fc.pct(c) >= 1]
    print(f"  {'':>12}  {' | '.join(parts)}")

    if analysis.duplicate_reads:
        top_dupes = sorted(
            analysis.duplicate_reads.items(), key=lambda x: x[1], reverse=True
        )[:5]
        dupe_str = ", ".join(f"{Path(k).name}({v}x)" for k, v in top_dupes)
        print(f"  {'':>12}  repeated reads: {dupe_str}")


def print_turn_detail(analysis: SessionAnalysis) -> None:
    """Print per-turn composition breakdown."""
    print(f"\nSession: {analysis.session_id}")
    print(f"Turns: {analysis.total_turns}  Compactions: {analysis.compaction_count}")
    print()

    legend = "S=system U=user A=assistant C=tool_call R=tool_result T=thinking"
    print(f"  Legend: {legend}")
    print()

    header = (
        f"  {'Turn':>5} {'Mode':>5} {'Msgs':>5} {'Context':>8} "
        f"{'Sys':>5} {'User':>5} {'Asst':>5} {'Call':>5} {'Rslt':>5} {'Thnk':>5} "
        f"{'Bar':<{BAR_WIDTH}}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    prev_total = 0
    for ta in analysis.turns:
        c = ta.composition
        t = c.total
        marker = ""
        if ta.compacted:
            marker = " << COMPACTED"

        growth = ""
        if prev_total > 0 and t > prev_total:
            delta = t - prev_total
            growth = f" +{_fmt_bytes(delta)}"

        pcts = [(cat, c.pct(cat)) for cat in CATEGORIES]
        bar = _bar(pcts)

        print(
            f"  {ta.turn_number:>5} {ta.storage_mode:>5} {ta.message_count:>5} "
            f"{_fmt_tokens(t):>8} "
            f"{c.pct('system'):>4.0f}% {c.pct('user_text'):>4.0f}% "
            f"{c.pct('assistant_text'):>4.0f}% {c.pct('tool_use'):>4.0f}% "
            f"{c.pct('tool_result'):>4.0f}% {c.pct('thinking_overhead'):>4.0f}% "
            f"[{bar}]{marker}{growth}"
        )
        prev_total = t

    # Noise analysis
    print()
    print("  ── Noise analysis ──")
    if analysis.turns:
        final = analysis.turns[-1].composition
        noise = final.thinking_overhead + final.tool_result
        signal = final.user_text + final.assistant_text
        total = final.total
        if total > 0:
            print(f"  Signal (user+assistant text):   {_fmt_bytes(signal):>8} ({signal / total * 100:.1f}%)")
            print(f"  Noise  (tool_result+thinking):  {_fmt_bytes(noise):>8} ({noise / total * 100:.1f}%)")
            overhead = final.system + final.tool_use
            print(f"  Overhead (system+tool_call):    {_fmt_bytes(overhead):>8} ({overhead / total * 100:.1f}%)")
            print(f"  Signal-to-noise ratio:          {signal / max(noise, 1):.2f}")

    # Duplicate reads
    if analysis.duplicate_reads:
        print()
        print("  ── Duplicate reads ──")
        for target, count in sorted(
            analysis.duplicate_reads.items(), key=lambda x: x[1], reverse=True
        ):
            print(f"    {count}x  {target}")

    # Compaction impact
    compacted_turns = [t for t in analysis.turns if t.compacted]
    if compacted_turns:
        print()
        print("  ── Compaction events ──")
        for i, ct in enumerate(compacted_turns):
            # Find the turn just before compaction
            idx = analysis.turns.index(ct)
            if idx > 0:
                before = analysis.turns[idx - 1]
                drop_bytes = before.composition.total - ct.composition.total
                drop_pct = drop_bytes / max(before.composition.total, 1) * 100
                print(
                    f"    Turn {ct.turn_number}: "
                    f"{_fmt_tokens(before.composition.total)}tok -> "
                    f"{_fmt_tokens(ct.composition.total)}tok "
                    f"(-{_fmt_bytes(drop_bytes)}, -{drop_pct:.0f}%) "
                    f"msgs {before.message_count} -> {ct.message_count}"
                )


def print_cross_session_summary(analyses: List[SessionAnalysis]) -> None:
    """Print aggregate insights across all sessions."""
    # Filter to sessions with meaningful data (>5 turns)
    meaningful = [a for a in analyses if a.total_turns > 5]
    if not meaningful:
        print("No sessions with >5 turns found.")
        return

    print(f"\n{'=' * 80}")
    print(f"CONTEXT COMPOSITION ANALYSIS — {len(analyses)} sessions, {len(meaningful)} with >5 turns")
    print(f"{'=' * 80}\n")

    # Per-session summaries
    print("Session summaries (sorted by peak context):")
    print("  Legend: S=system U=user A=assistant C=tool_call R=tool_result T=thinking\n")
    for a in sorted(meaningful, key=lambda x: x.peak_bytes, reverse=True):
        print_session_summary(a)
        print()

    # Aggregate composition at final turn (weighted by context size)
    total_bytes = sum(
        a.final_composition.total for a in meaningful if a.final_composition
    )
    if total_bytes == 0:
        return

    agg = {cat: 0 for cat in CATEGORIES}
    for a in meaningful:
        fc = a.final_composition
        if not fc:
            continue
        for cat in CATEGORIES:
            agg[cat] += getattr(fc, cat)

    print(f"\n{'─' * 80}")
    print("AGGREGATE composition (final-turn weighted by context size):")
    print()
    for cat in sorted(CATEGORIES, key=lambda c: agg[c], reverse=True):
        pct = agg[cat] / total_bytes * 100
        bar_len = round(pct / 100 * 50)
        bar = "#" * bar_len
        print(f"  {cat:<20s} {_fmt_bytes(agg[cat]):>8} ({pct:>5.1f}%)  {bar}")

    signal = agg["user_text"] + agg["assistant_text"]
    noise = agg["tool_result"] + agg["thinking_overhead"]
    print(f"\n  Aggregate signal-to-noise ratio: {signal / max(noise, 1):.2f}")
    print(f"  Tool result share of total context: {agg['tool_result'] / total_bytes * 100:.1f}%")

    # Compaction stats
    total_compactions = sum(a.compaction_count for a in meaningful)
    sessions_with_compaction = sum(1 for a in meaningful if a.compaction_count > 0)
    print(f"\n  Compactions: {total_compactions} across {sessions_with_compaction}/{len(meaningful)} sessions")

    # Top duplicate reads across all sessions
    all_dupes = defaultdict(int)  # type: Dict[str, int]
    for a in meaningful:
        for target, count in a.duplicate_reads.items():
            all_dupes[target] += count

    if all_dupes:
        print(f"\n{'─' * 80}")
        print("TOP repeated reads across all sessions:")
        for target, count in sorted(all_dupes.items(), key=lambda x: x[1], reverse=True)[:15]:
            print(f"    {count:>3}x  {target}")


def output_json(analyses: List[SessionAnalysis]) -> None:
    """Output full analysis as JSON."""
    result = []
    for a in analyses:
        session_data = {
            "session_id": a.session_id,
            "total_turns": a.total_turns,
            "compaction_count": a.compaction_count,
            "peak_bytes": a.peak_bytes,
            "tool_result_peak_pct": round(a.tool_result_peak_pct, 1),
            "duplicate_reads": a.duplicate_reads,
        }
        fc = a.final_composition
        if fc:
            session_data["final_composition"] = fc.to_dict()

        # Per-turn data (sampled for large sessions)
        turn_data = []
        turns = a.turns
        if len(turns) > 50:
            # Sample: first 5, every 10th, last 5
            indices = set(range(5))
            indices.update(range(0, len(turns), 10))
            indices.update(range(max(0, len(turns) - 5), len(turns)))
            turns_to_include = sorted(indices)
        else:
            turns_to_include = list(range(len(turns)))

        for i in turns_to_include:
            if i >= len(a.turns):
                continue
            t = a.turns[i]
            turn_data.append(
                {
                    "turn": t.turn_number,
                    "mode": t.storage_mode,
                    "msgs": t.message_count,
                    "compacted": t.compacted,
                    "composition": t.composition.to_dict(),
                }
            )
        session_data["turns"] = turn_data
        result.append(session_data)

    print(json.dumps(result, indent=2, ensure_ascii=False))


# ── Main ──


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze context window composition from llm-relay session history"
    )
    parser.add_argument("session_id", nargs="?", help="Specific session ID (partial match OK)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--turns", action="store_true", help="Show per-turn breakdown")
    parser.add_argument("--top", type=int, default=0, help="Show only top N sessions by size")
    parser.add_argument("--db", type=str, default=str(DB_PATH), help="Database path")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        sessions = _get_sessions(conn)

        if args.session_id:
            # Partial match
            matches = [
                s for s in sessions if args.session_id in s["session_id"]
            ]
            if not matches:
                print(f"No session matching '{args.session_id}'", file=sys.stderr)
                sys.exit(1)
            sessions = matches

        if args.top > 0:
            # Sort by peak request bytes and take top N
            sessions = sorted(
                sessions, key=lambda s: s["peak_request_bytes"] or 0, reverse=True
            )[: args.top]

        analyses = []
        for s in sessions:
            analysis = analyze_session(conn, s["session_id"])
            analyses.append(analysis)

        if args.json:
            output_json(analyses)
        elif args.turns and len(analyses) == 1:
            print_turn_detail(analyses[0])
        elif len(analyses) == 1:
            print_turn_detail(analyses[0])
        else:
            print_cross_session_summary(analyses)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
