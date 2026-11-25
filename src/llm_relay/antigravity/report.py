"""Read-only reports over the provider_* tables -- ``python -m llm_relay.antigravity.report``.

    quota                 latest remaining fraction per quota bucket
    recent [-n N]         the last N recorded conversations (delegations and interactive sessions)
    show SESSION_ID       the stored turns of one conversation

Nothing here writes. The data comes from the Antigravity relay (see ``app.py``).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

from llm_relay.antigravity import PROVIDER, store


def _conn() -> Any:
    from llm_relay.proxy.db import get_conn  # lazy: importing it loads the repo env files

    return get_conn()


def _when(ts: Any) -> str:
    """Local wall-clock text for a stored ts (datetime on PostgreSQL, epoch float on SQLite)."""
    if ts is None:
        return ""
    if hasattr(ts, "astimezone"):
        return ts.astimezone().strftime("%m-%d %H:%M:%S")
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return str(ts)


def _json(value: Any) -> Any:
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def quota(conn: Any) -> List[Dict[str, Any]]:
    rows = store.get_latest_quota(conn, PROVIDER)
    return [
        {
            "bucket_id": r["bucket_id"],
            "window": r.get("quota_window"),
            "remaining_pct": None if r.get("remaining_fraction") is None else round(r["remaining_fraction"] * 100, 1),
            "reset_time": r.get("reset_time"),
            "recorded": _when(r.get("ts")),
        }
        for r in rows
    ]


def recent(conn: Any, limit: int = 10) -> List[Dict[str, Any]]:
    """The last ``limit`` conversations, newest first, with their size and cost."""
    cur = conn.execute(
        "SELECT id, ts, session_id, turn_number, model, last_user_prompt, request_row_id FROM provider_turns "
        "WHERE provider = ? ORDER BY id DESC LIMIT ?",
        (PROVIDER, max(limit, 1) * 40),
    )
    sessions: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for raw in cur.fetchall():
        row = dict(raw)
        sid = row["session_id"]
        if sid not in sessions:
            if len(order) >= limit:
                continue
            order.append(sid)
            sessions[sid] = {"session_id": sid, "last": _when(row["ts"]), "model": row["model"], "turns": 0,
                             "prompt": row["last_user_prompt"], "request_ids": [], "first_turn": row["turn_number"]}
        entry = sessions[sid]
        entry["turns"] += 1
        if row["turn_number"] <= entry["first_turn"]:
            entry["first_turn"] = row["turn_number"]
            entry["prompt"] = row["last_user_prompt"]
        if row["request_row_id"] is not None:
            entry["request_ids"].append(row["request_row_id"])
    out = []
    for sid in order:
        entry = sessions[sid]
        ids = entry.pop("request_ids")
        entry.pop("first_turn")
        peak = output = thinking = 0
        latency = 0.0
        if ids:
            marks = ", ".join(["?"] * len(ids))
            for raw in conn.execute(
                "SELECT prompt_tokens, output_tokens, thinking_tokens, latency_ms FROM provider_requests "
                "WHERE id IN ({})".format(marks), tuple(ids),
            ).fetchall():
                r = dict(raw)
                peak = max(peak, r["prompt_tokens"] or 0)
                output += r["output_tokens"] or 0
                thinking += r["thinking_tokens"] or 0
                latency += r["latency_ms"] or 0.0
        entry.update({"peak_context": peak, "output_tokens": output, "thinking_tokens": thinking,
                      "model_seconds": round(latency / 1000.0, 1)})
        out.append(entry)
    return out


def _block_text(block: Dict[str, Any], width: int) -> str:
    kind = block.get("type")
    if kind == "text":
        text = block.get("text") or ""
    elif kind == "thinking":
        text = "(thinking) " + (block.get("thinking") or "")
    elif kind == "tool_use":
        text = "[tool_use {}] {}".format(block.get("name"), json.dumps(block.get("input"), ensure_ascii=False))
    elif kind == "tool_result":
        text = "[tool_result {}] {}".format(block.get("name"), json.dumps(block.get("content"), ensure_ascii=False))
    else:
        text = "[{}]".format(kind)
    text = text.strip()
    return text if width <= 0 or len(text) <= width else text[:width] + " …"


def show(conn: Any, session_id: str, width: int = 600) -> List[str]:
    """Readable lines for one conversation: each turn's new messages, then the model's answer."""
    cur = conn.execute(
        "SELECT turn_number, ts, storage_mode, model, request_messages, response_message FROM provider_turns "
        "WHERE provider = ? AND session_id = ? ORDER BY turn_number, id",
        (PROVIDER, session_id),
    )
    lines: List[str] = []
    for raw in cur.fetchall():
        row = dict(raw)
        lines.append("=== turn {} · {} · {} · {}".format(row["turn_number"], _when(row["ts"]), row["model"],
                                                      row["storage_mode"]))
        for message in _json(row["request_messages"]) or []:
            for block in message.get("content") or []:
                lines.append("{:>9}: {}".format(message.get("role"), _block_text(block, width)))
        for block in _json(row["response_message"]) or []:
            lines.append("{:>9}: {}".format("answer", _block_text(block, width)))
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m llm_relay.antigravity.report",
                                     description="Read-only reports on recorded Antigravity traffic.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("quota", help="latest remaining quota per bucket")
    p_recent = sub.add_parser("recent", help="last recorded conversations")
    p_recent.add_argument("-n", type=int, default=10)
    p_show = sub.add_parser("show", help="stored turns of one conversation")
    p_show.add_argument("session_id")
    p_show.add_argument("--width", type=int, default=600, help="characters kept per block (0 = all)")
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help(sys.stderr)
        return 2
    conn = _conn()
    try:
        if args.command == "quota":
            for row in quota(conn):
                print("{:<26} {:<7} remaining {:>5}%  resets {}  (recorded {})".format(
                    row["bucket_id"], row["window"] or "", row["remaining_pct"], row["reset_time"], row["recorded"]))
        elif args.command == "recent":
            for row in recent(conn, args.n):
                print("{}  {}  {:<22} turns={:<2} peak_ctx={:<7} out={:<5} think={:<6} {:>6}s  {}".format(
                    row["last"], row["session_id"], row["model"] or "", row["turns"], row["peak_context"],
                    row["output_tokens"], row["thinking_tokens"], row["model_seconds"],
                    (row["prompt"] or "").replace("\n", " ")[:70]))
        else:
            lines = show(conn, args.session_id, args.width)
            if not lines:
                print("no stored turns for session {}".format(args.session_id), file=sys.stderr)
                return 1
            print("\n".join(lines))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
