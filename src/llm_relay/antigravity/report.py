"""Read-only reports over the provider_* tables -- ``python -m llm_relay.antigravity.report``.

    quota                         latest remaining fraction per quota bucket
    recent [-n N]                 the last N recorded conversations (delegations and interactive sessions)
    show SESSION_ID [--thinking]  the stored turns of one conversation (optionally with the thinking blocks)
    aux [-n N] [--type T]         generate calls stored without a turn row (checkpoint, web_search, ...)
    blobs [--kind K]              stored system prompts, tool lists and model / experiment snapshots
    blob SHA_PREFIX               the body of one stored blob as indented JSON

Nothing here writes. The data comes from the Antigravity relay (see ``app.py``).
``aux``, ``blobs`` and ``blob`` read columns and a table added in 2026-11; on a
store migrated before them they print one line pointing at
``docs/migrate-provider-columns.py`` and exit 1.

SQL here never holds a literal ``?`` or ``%``: the PostgreSQL wrapper of
``llm_relay.proxy.db`` turns every ``?`` into ``%s``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

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


def show(conn: Any, session_id: str, width: int = 600, thinking: bool = False) -> List[str]:
    """Readable lines for one conversation: each turn's new messages, then the model's answer.

    ``thinking`` adds the turn's thinking blocks as ``(thinking) ...`` lines just
    before its answer lines.
    """
    cur = conn.execute(
        "SELECT turn_number, ts, storage_mode, model, request_messages, response_message, thinking_blocks"
        " FROM provider_turns WHERE provider = ? AND session_id = ? ORDER BY turn_number, id",
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
        if thinking:
            for block in _json(row["thinking_blocks"]) or []:
                lines.append("{:>9}: {}".format("answer", _block_text(block, width)))
        for block in _json(row["response_message"]) or []:
            lines.append("{:>9}: {}".format("answer", _block_text(block, width)))
    return lines


class SchemaMissing(RuntimeError):
    """The store predates the columns or table a report needs; the message names them."""


_MIGRATE_HINT = "run docs/migrate-provider-columns.py"
_AUX_COLUMNS = ("response_message", "thinking_blocks")


def _require_request_columns(conn: Any, columns: Iterable[str]) -> None:
    present = store.request_columns_present(conn)
    missing = [column for column in columns if column not in present]
    if missing:
        raise SchemaMissing("provider_requests has no {} column yet: {}".format(", ".join(missing), _MIGRATE_HINT))


def _require_blobs(conn: Any) -> None:
    if not store.tables_present(conn, store.OPTIONAL_TABLES).get("provider_blobs"):
        raise SchemaMissing("there is no provider_blobs table yet: {}".format(_MIGRATE_HINT))


def _blocks_text(blocks: Iterable[Any], width: int) -> str:
    """Blocks on one line, cut at ``width`` characters (0 = all)."""
    text = " ".join(_block_text(block, 0) for block in blocks if isinstance(block, dict))
    text = " ".join(text.split())
    return text if width <= 0 or len(text) <= width else text[:width] + " …"


def aux(conn: Any, limit: int = 10, request_type: Optional[str] = None, width: int = 200) -> List[Dict[str, Any]]:
    """The newest ``limit`` request rows that carry a body of their own, newest first.

    These are generate calls without a turn row (``checkpoint``, ``web_search``,
    an interrupted agent call, ...). ``web_search`` and ``agent_monitoring``
    rows have no session id: match them to a conversation by time. ``body`` is
    the answer blocks, then the thinking blocks, on one line.
    """
    _require_request_columns(conn, _AUX_COLUMNS)
    sql = ("SELECT ts, request_type, session_id, model, output_tokens, thinking_tokens, stream_end,"
           " response_message, thinking_blocks FROM provider_requests WHERE provider = ?"
           " AND (response_message IS NOT NULL OR thinking_blocks IS NOT NULL)")
    params: List[Any] = [PROVIDER]
    if request_type:
        sql += " AND request_type = ?"
        params.append(request_type)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(limit, 1))
    out = []
    for raw in conn.execute(sql, tuple(params)).fetchall():
        row = dict(raw)
        blocks = list(_json(row["response_message"]) or []) + list(_json(row["thinking_blocks"]) or [])
        out.append({
            "when": _when(row["ts"]),
            "request_type": row["request_type"],
            "session_id": row["session_id"],
            "model": row["model"],
            "output_tokens": row["output_tokens"],
            "thinking_tokens": row["thinking_tokens"],
            "stream_end": row["stream_end"],
            "body": _blocks_text(blocks, width),
        })
    return out


def blobs(conn: Any, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    """Stored blobs newest first (the body itself is not read)."""
    _require_blobs(conn)
    sql = "SELECT ts, kind, sha256, size_bytes FROM provider_blobs WHERE provider = ?"
    params: List[Any] = [PROVIDER]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY ts DESC, id DESC"
    return [
        {"when": _when(row["ts"]), "kind": row["kind"], "sha256": row["sha256"], "size_bytes": row["size_bytes"]}
        for row in (dict(raw) for raw in conn.execute(sql, tuple(params)).fetchall())
    ]


def blob(conn: Any, sha_prefix: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Blobs whose sha256 starts with ``sha_prefix`` (at most ``limit``, oldest first), body parsed."""
    _require_blobs(conn)
    prefix = sha_prefix.strip().lower()
    if not prefix:
        return []
    # substr with parameters instead of LIKE 'x%': no literal % in the SQL (see the module docstring).
    rows = conn.execute(
        "SELECT ts, kind, sha256, size_bytes, body FROM provider_blobs"
        " WHERE provider = ? AND substr(sha256, 1, ?) = ? ORDER BY id LIMIT ?",
        (PROVIDER, len(prefix), prefix, max(limit, 1)),
    ).fetchall()
    out = []
    for raw in rows:
        row = dict(raw)
        out.append({"when": _when(row["ts"]), "kind": row["kind"], "sha256": row["sha256"],
                    "size_bytes": row["size_bytes"], "body": _json(row["body"])})
    return out


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
    p_show.add_argument("--thinking", action="store_true", help="also print each turn's thinking blocks")
    p_aux = sub.add_parser("aux", help="generate calls stored without a turn row")
    p_aux.add_argument("-n", type=int, default=10)
    p_aux.add_argument("--type", dest="request_type", help="only this request_type (e.g. checkpoint)")
    p_aux.add_argument("--width", type=int, default=200, help="characters of body kept (0 = all)")
    p_blobs = sub.add_parser("blobs", help="stored system prompts, tool lists and snapshots")
    p_blobs.add_argument("--kind", help="only this kind (e.g. system_instruction)")
    p_blob = sub.add_parser("blob", help="the body of one stored blob")
    p_blob.add_argument("sha_prefix")
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help(sys.stderr)
        return 2
    conn = _conn()
    try:
        return _run(conn, args)
    except SchemaMissing as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        conn.close()


def _run(conn: Any, args: argparse.Namespace) -> int:
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
    elif args.command == "aux":
        for row in aux(conn, args.n, args.request_type, args.width):
            print("{}  {:<16} {}  {:<22} out={:<5} think={:<6} {:<10} {}".format(
                row["when"], row["request_type"] or "", row["session_id"] or "-", row["model"] or "",
                row["output_tokens"] or 0, row["thinking_tokens"] or 0, row["stream_end"] or "", row["body"]))
    elif args.command == "blobs":
        for row in blobs(conn, args.kind):
            print("{}  {:<18} {}  {:>9} bytes".format(row["when"], row["kind"], row["sha256"][:12], row["size_bytes"]))
    elif args.command == "blob":
        found = blob(conn, args.sha_prefix)
        if len(found) != 1:
            if not found:
                print("no stored blob with sha256 starting {!r}".format(args.sha_prefix), file=sys.stderr)
            else:
                print("sha256 prefix {!r} matches more than one blob; give more characters:".format(args.sha_prefix),
                      file=sys.stderr)
                for row in found:
                    print("  {}  {}".format(row["kind"], row["sha256"]), file=sys.stderr)
            return 1
        print(json.dumps(found[0]["body"], ensure_ascii=False, indent=2))
    else:
        lines = show(conn, args.session_id, args.width, thinking=args.thinking)
        if not lines:
            print("no stored turns for session {}".format(args.session_id), file=sys.stderr)
            return 1
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
