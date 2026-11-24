"""Command-line entry for CLI orchestration -- ``python -m llm_relay.orch``.

Runs a delegation outside the MCP server with the same discovery, command
building and success rules as the ``cli_delegate`` tool. The MCP tool blocks its
caller until the CLI answers; a shell job started from this entry point can run
in the background (and several can run side by side), writing its result to a
file the caller reads when the job ends.

    python -m llm_relay.orch delegate --cli agy --prompt-file task.md \\
        --working-dir /path/to/repo --timeout 900 --out result.json
    python -m llm_relay.orch status

``delegate`` prints (or writes with ``--out``) one JSON object:
``{"success", "cli_id", "model", "output", "error", "duration_ms", "exit_code"}``
plus ``"quota"`` when the agy quota preflight ran (see ``llm_relay.orch.agy_quota``),
and exits 0 on success, 1 on a failed delegation, 2 on a usage error and 3 when the
quota preflight blocked the call (agy was not started; ``error`` starts with
``quota preflight:``). ``--quota-floor PCT`` sets the blocking floor for one call
and ``--no-quota-check`` skips the preflight.
stdlib only.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Same short names as the MCP tools (llm_relay.mcp.server._CLI_ALIASES); kept here
# because importing that module requires the optional MCP SDK.
_CLI_ALIASES = {
    "claude": "claude-code",
    "codex": "openai-codex",
    "gemini": "gemini-cli",
    "agy": "antigravity-cli",
    "antigravity": "antigravity-cli",
}


def _read_prompt(args: argparse.Namespace) -> Optional[str]:
    """The prompt from --prompt, --prompt-file (``-`` = stdin) or None when neither is usable."""
    if args.prompt is not None:
        return args.prompt
    if args.prompt_file is None:
        return None
    if args.prompt_file == "-":
        return sys.stdin.read()
    with open(args.prompt_file, encoding="utf-8") as f:
        return f.read()


def _emit(payload: Dict[str, Any], out_path: Optional[str]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if not out_path:
        print(text)
        return
    # Write then rename: a reader woken by the job's exit never sees a half-written file.
    directory = os.path.dirname(os.path.abspath(out_path))
    fd, tmp = tempfile.mkstemp(prefix=".delegate-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        os.replace(tmp, out_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _log(result: Any, prompt: str, model: Optional[str], working_dir: Optional[str]) -> None:
    """Record the delegation like the MCP tool does (strategy ``cli``). Best effort."""
    try:
        from llm_relay.orch.db import get_orch_conn, log_delegation
        from llm_relay.orch.executor import prompt_hash, prompt_preview

        conn = get_orch_conn()
        log_delegation(
            conn,
            cli_id=result.cli_id,
            auth_method=result.auth_method.value,
            prompt_hash=prompt_hash(prompt),
            prompt_preview=prompt_preview(prompt),
            model=model,
            working_dir=working_dir,
            success=result.success,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            output_chars=len(result.output),
            error=result.error,
            strategy="cli",
        )
        conn.close()
    except Exception:
        logger.debug("Failed to log delegation", exc_info=True)


def cmd_delegate(args: argparse.Namespace) -> int:
    from llm_relay.orch.discovery import discover_all
    from llm_relay.orch.executor import execute_cli

    quota_floor = args.quota_floor
    if quota_floor is not None and not 0.0 <= quota_floor <= 100.0:  # also rejects NaN
        print("error: --quota-floor must be between 0 and 100", file=sys.stderr)
        return 2

    try:
        prompt = _read_prompt(args)
    except OSError as exc:
        print("error: cannot read the prompt: {}".format(exc), file=sys.stderr)
        return 2
    if prompt is None or not prompt.strip():
        print("error: give a non-empty prompt with --prompt or --prompt-file", file=sys.stderr)
        return 2

    cli_id = _CLI_ALIASES.get(args.cli, args.cli)
    target = None
    for status in discover_all():
        if status.cli_id == cli_id or status.binary_name == args.cli:
            target = status
            break

    model = args.model or None
    working_dir = args.working_dir or None
    if target is None or not target.is_usable():
        payload: Dict[str, Any] = {
            "success": False,
            "cli_id": cli_id,
            "model": model,
            "output": "",
            "error": "CLI '{}' is not available or not authenticated".format(args.cli),
            "duration_ms": 0.0,
            "exit_code": -1,
        }
        _emit(payload, args.out)
        return 1

    # Quota options are passed only when set, so the call stays the same as before for the defaults.
    quota_kwargs: Dict[str, Any] = {}
    if args.no_quota_check:
        quota_kwargs["quota_check"] = False
    if quota_floor is not None:
        quota_kwargs["quota_block_pct"] = quota_floor
    result = execute_cli(
        target, prompt, model=model, working_dir=working_dir, timeout=args.timeout, **quota_kwargs,
    )
    if not args.no_log:
        _log(result, prompt, model, working_dir)
    payload = {
        "success": result.success,
        "cli_id": result.cli_id,
        "model": model,
        "output": result.output,
        "error": result.error,
        "duration_ms": round(result.duration_ms, 1),
        "exit_code": result.exit_code,
    }
    quota = getattr(result, "quota", None)
    if quota is not None:
        payload["quota"] = quota
    _emit(payload, args.out)
    if result.success:
        return 0
    if isinstance(quota, dict) and quota.get("verdict") == "blocked":
        return 3
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    from llm_relay.orch.discovery import discover_all

    rows = [
        {
            "cli_id": s.cli_id,
            "binary_name": s.binary_name,
            "binary_path": s.binary_path,
            "installed": s.installed,
            "authenticated": s.cli_authenticated,
            "preferred_auth": s.preferred_auth.value,
            "version": s.version,
            "usable": s.is_usable(),
        }
        for s in discover_all()
    ]
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m llm_relay.orch", description="CLI orchestration from the shell.")
    sub = parser.add_subparsers(dest="command")

    delegate = sub.add_parser("delegate", help="send one task to a CLI in headless mode")
    delegate.add_argument("--cli", default="agy", help="claude | codex | gemini | agy (default: agy)")
    delegate.add_argument("--prompt", help="the task text")
    delegate.add_argument("--prompt-file", help="read the task text from a file ('-' = stdin)")
    delegate.add_argument("--model", default="", help="model override (default: the CLI adapter's default)")
    delegate.add_argument("--working-dir", default="", help="directory the CLI runs in")
    delegate.add_argument("--timeout", type=int, default=600, help="seconds before the call is cut (default 600)")
    delegate.add_argument("--out", default="", help="write the JSON result to this file instead of stdout")
    delegate.add_argument("--no-log", action="store_true", help="do not record the delegation in the orch table")
    delegate.add_argument(
        "--quota-floor", type=float, default=None, metavar="PCT",
        help="agy only: do not start the call when a quota bucket it draws from is below PCT percent "
             "(0-100, 0 = never block; default: LLM_RELAY_AGY_QUOTA_BLOCK_PCT or 10)",
    )
    delegate.add_argument(
        "--no-quota-check", action="store_true", help="agy only: skip the quota preflight before the call",
    )
    delegate.set_defaults(func=cmd_delegate)

    status = sub.add_parser("status", help="list registered CLIs and whether they are usable")
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help(sys.stderr)
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
