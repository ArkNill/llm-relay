"""Session recovery -- extract context from JSONL for resumption in a new session.

Absorbs the recover algorithm from kolkov/ccdiag: walk the session JSONL
and extract files modified, git commands, GitHub actions, URLs, and issue
references.  Outputs a handoff summary or structured action list.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FileAction:
    tool: str  # Write, Edit, Read
    path: str
    action: str  # created, modified, read


@dataclass
class GitAction:
    command: str


@dataclass
class GitHubAction:
    command: str
    action_type: str  # issue-create, issue-comment, pr-create, pr-merge, etc.


@dataclass
class SessionContext:
    """Extracted context from a session JSONL."""

    session_id: str = ""
    project_path: str = ""
    files_modified: list[FileAction] = field(default_factory=list)
    git_commands: list[GitAction] = field(default_factory=list)
    github_actions: list[GitHubAction] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    issues: dict[str, int] = field(default_factory=dict)  # issue# → mention count
    bash_commands: list[str] = field(default_factory=list)
    key_messages: list[tuple[str, str]] = field(default_factory=list)  # (role, text_preview)

    @property
    def unique_files(self) -> set[str]:
        return {f.path for f in self.files_modified}


_URL_RE = re.compile(r"https?://[^\s\"'`<>\)\]}{]+")
_ISSUE_RE = re.compile(r"#(\d{3,6})")
_TRIVIAL_BASH = frozenset({"ls", "cat", "cd", "pwd", "wc", "mkdir", "test", "echo", "head", "tail", "true", "false"})

_GH_ACTIONS = {
    "gh issue create": "issue-create",
    "gh issue comment": "issue-comment",
    "gh pr create": "pr-create",
    "gh pr merge": "pr-merge",
    "gh pr review": "pr-review",
    "gh api": "api-call",
}


def extract_context(path: Path) -> SessionContext:
    """Parse a session JSONL and extract all actionable context."""
    ctx = SessionContext()

    messages: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not messages:
        return ctx

    # Extract session ID from first message
    first = messages[0]
    ctx.session_id = first.get("sessionId", "") or first.get("session_id", "")

    for msg in messages:
        inner = msg.get("message", {})
        if not isinstance(inner, dict):
            continue

        role = inner.get("role", "")
        content = inner.get("content", [])
        if not isinstance(content, list):
            if isinstance(content, str):
                _extract_urls(content, ctx)
                _extract_issues(content, ctx)
                if role in ("user", "assistant"):
                    ctx.key_messages.append((role, content[:200]))
            continue

        for block in content:
            if not isinstance(block, dict):
                continue

            btype = block.get("type", "")

            if btype == "tool_use":
                name = block.get("name", "")
                inp = block.get("input", {})

                if name in ("Write", "Edit", "Read"):
                    fpath = inp.get("file_path", "")
                    if fpath:
                        action = "read" if name == "Read" else ("created" if name == "Write" else "modified")
                        ctx.files_modified.append(FileAction(tool=name, path=fpath, action=action))

                elif name == "Bash":
                    cmd = inp.get("command", "")
                    if cmd:
                        _classify_bash(cmd, ctx)

                elif name in ("WebFetch", "WebSearch"):
                    url = inp.get("url", "") or inp.get("query", "")
                    if url:
                        ctx.urls.append(url)

            elif btype == "text":
                text = block.get("text", "")
                _extract_urls(text, ctx)
                _extract_issues(text, ctx)

            elif btype == "tool_result":
                content_val = block.get("content", "")
                if isinstance(content_val, str):
                    _extract_urls(content_val, ctx)

        # Capture key user/assistant messages
        if role in ("user", "assistant"):
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            if text_parts:
                combined = " ".join(text_parts)[:200]
                ctx.key_messages.append((role, combined))

    return ctx


def _extract_urls(text: str, ctx: SessionContext) -> None:
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:!?)")
        if len(url) > 15 and url not in ctx.urls:
            ctx.urls.append(url)


def _extract_issues(text: str, ctx: SessionContext) -> None:
    for match in _ISSUE_RE.finditer(text):
        num = match.group(0)
        ctx.issues[num] = ctx.issues.get(num, 0) + 1


def _classify_bash(cmd: str, ctx: SessionContext) -> None:
    first_word = cmd.strip().split()[0] if cmd.strip() else ""

    if first_word == "git":
        ctx.git_commands.append(GitAction(command=cmd.strip()))
        return

    if first_word == "gh":
        for prefix, atype in _GH_ACTIONS.items():
            if cmd.strip().startswith(prefix):
                ctx.github_actions.append(GitHubAction(command=cmd.strip(), action_type=atype))
                return

    if first_word in _TRIVIAL_BASH:
        return

    if len(cmd.strip()) > 20:
        ctx.bash_commands.append(cmd.strip()[:200])


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def format_handoff(ctx: SessionContext) -> str:
    """One-paragraph context summary for continuing in a new session."""
    parts: list[str] = []

    if ctx.unique_files:
        write_edit = {f.path for f in ctx.files_modified if f.action != "read"}
        if write_edit:
            parts.append(f"Files modified: {', '.join(sorted(write_edit)[:10])}")
            if len(write_edit) > 10:
                parts.append(f"  (+{len(write_edit) - 10} more)")

    if ctx.git_commands:
        parts.append(f"Git commands: {len(ctx.git_commands)}")

    if ctx.github_actions:
        by_type: dict[str, int] = {}
        for ga in ctx.github_actions:
            by_type[ga.action_type] = by_type.get(ga.action_type, 0) + 1
        parts.append("GitHub: " + ", ".join(f"{n}x {t}" for t, n in by_type.items()))

    if ctx.issues:
        top = sorted(ctx.issues.items(), key=lambda x: -x[1])[:5]
        parts.append("Issues referenced: " + ", ".join(f"{num}({cnt}x)" for num, cnt in top))

    if ctx.urls:
        parts.append(f"URLs accessed: {len(ctx.urls)}")

    # Last user message as context
    user_msgs = [(r, t) for r, t in ctx.key_messages if r == "user"]
    if user_msgs:
        parts.append(f"Last user request: {user_msgs[-1][1][:150]}")

    return "\n".join(parts) if parts else "(empty session)"


def format_actions(ctx: SessionContext) -> str:
    """Structured list of all side-effects."""
    lines: list[str] = []

    if ctx.files_modified:
        lines.append("## Files")
        seen: set[str] = set()
        for fa in ctx.files_modified:
            key = f"{fa.action}:{fa.path}"
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"  {fa.action:8s} {fa.path}")

    if ctx.git_commands:
        lines.append("\n## Git")
        for ga in ctx.git_commands:
            lines.append(f"  {ga.command}")

    if ctx.github_actions:
        lines.append("\n## GitHub")
        for ga in ctx.github_actions:
            lines.append(f"  [{ga.action_type}] {ga.command}")

    if ctx.bash_commands:
        lines.append("\n## Bash")
        for cmd in ctx.bash_commands[:20]:
            lines.append(f"  {cmd}")

    if ctx.urls:
        lines.append("\n## URLs")
        for url in sorted(set(ctx.urls))[:20]:
            lines.append(f"  {url}")

    if ctx.issues:
        lines.append("\n## Issues")
        for num, cnt in sorted(ctx.issues.items(), key=lambda x: -x[1]):
            lines.append(f"  {num} ({cnt}x)")

    return "\n".join(lines) if lines else "(no actions found)"


def format_full(ctx: SessionContext) -> str:
    """Handoff + actions combined."""
    return format_handoff(ctx) + "\n\n---\n\n" + format_actions(ctx)
