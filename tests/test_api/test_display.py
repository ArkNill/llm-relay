"""Tests for api/display.py — multi-CLI session prompt extraction and process detection."""

from __future__ import annotations

import json

from llm_relay.api.display import (
    _extract_prompt_from_cc,
    _extract_prompt_from_codex,
    _extract_prompt_from_gemini,
    _is_cli_process,
    _is_real_user_prompt,
    find_claude_pid_by_tty,
    find_cli_pid_by_tty,
    get_last_user_prompt,
    is_cc_process_alive,
    is_cli_process_alive,
)

# ── _is_cli_process ──

class TestIsCliProcess:
    def test_claude(self):
        assert _is_cli_process("claude", "") is True

    def test_codex(self):
        assert _is_cli_process("codex", "") is True

    def test_gemini(self):
        assert _is_cli_process("gemini", "") is True

    def test_unknown(self):
        assert _is_cli_process("bash", "") is False

    def test_cmdline_match(self):
        assert _is_cli_process("node", "/usr/bin/claude --arg") is True
        assert _is_cli_process("node", "/usr/bin/codex app-server") is True

    def test_case_insensitive(self):
        assert _is_cli_process("Claude", "") is True
        assert _is_cli_process("CODEX", "") is True


# ── backward compatibility aliases ──

class TestBackwardCompat:
    def test_find_claude_alias(self):
        assert find_claude_pid_by_tty is find_cli_pid_by_tty

    def test_is_cc_alias(self):
        assert is_cc_process_alive is is_cli_process_alive


# ── _is_real_user_prompt ──

class TestIsRealUserPrompt:
    def test_normal(self):
        assert _is_real_user_prompt("hello world") is True

    def test_empty(self):
        assert _is_real_user_prompt("") is False

    def test_wrapper(self):
        assert _is_real_user_prompt("<task-notification>...") is False
        assert _is_real_user_prompt("<tool_use_error>...") is False

    def test_system_reminder(self):
        assert _is_real_user_prompt("<system-reminder>data</system-reminder>") is False


# ── Claude Code prompt extraction ──

class TestExtractPromptCC:
    def test_basic(self):
        lines = [
            json.dumps({
                "type": "user",
                "timestamp": "2026-04-15T10:00:00Z",
                "message": {"role": "user", "content": "fix the bug"},
            }),
        ]
        result = _extract_prompt_from_cc(lines)
        assert result["text"] == "fix the bug"
        assert result["timestamp"] == "2026-04-15T10:00:00Z"

    def test_skips_assistant(self):
        lines = [
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "hello"},
            }),
            json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": "hi"},
            }),
        ]
        result = _extract_prompt_from_cc(lines)
        assert result["text"] == "hello"

    def test_empty(self):
        result = _extract_prompt_from_cc([])
        assert result["text"] == ""
        assert result["timestamp"] is None

    def test_skips_wrapper(self):
        lines = [
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "<task-notification>..."},
            }),
        ]
        result = _extract_prompt_from_cc(lines)
        assert result["text"] == ""


# ── Codex prompt extraction ──

class TestExtractPromptCodex:
    def test_basic_type_user(self):
        lines = [
            json.dumps({
                "type": "user",
                "timestamp": "2026-04-14T11:02:32Z",
                "message": {"content": "refactor the module"},
            }),
        ]
        result = _extract_prompt_from_codex(lines)
        assert result["text"] == "refactor the module"

    def test_role_user(self):
        lines = [
            json.dumps({
                "role": "user",
                "created_at": "2026-04-14T12:00:00Z",
                "content": "run tests",
            }),
        ]
        result = _extract_prompt_from_codex(lines)
        assert result["text"] == "run tests"
        assert result["timestamp"] == "2026-04-14T12:00:00Z"

    def test_text_field(self):
        lines = [
            json.dumps({
                "type": "user",
                "text": "deploy to staging",
            }),
        ]
        result = _extract_prompt_from_codex(lines)
        assert result["text"] == "deploy to staging"

    def test_empty(self):
        result = _extract_prompt_from_codex([])
        assert result["text"] == ""


# ── Gemini prompt extraction ──

class TestExtractPromptGemini:
    def test_json_array(self):
        content = json.dumps([
            {"type": "user", "message": "hello gemini", "timestamp": "2026-04-13T06:32:26Z"},
            {"type": "gemini", "message": "hi there"},
        ])
        result = _extract_prompt_from_gemini(content)
        assert result["text"] == "hello gemini"

    def test_jsonl(self):
        lines = [
            json.dumps({"type": "user", "text": "search for patterns"}),
            json.dumps({"type": "assistant", "text": "found 3 matches"}),
        ]
        content = "\n".join(lines)
        result = _extract_prompt_from_gemini(content)
        assert result["text"] == "search for patterns"

    def test_empty(self):
        result = _extract_prompt_from_gemini("")
        assert result["text"] == ""

    def test_role_user(self):
        content = json.dumps([
            {"role": "user", "content": "explain this code", "createdAt": "2026-04-13T07:00:00Z"},
        ])
        result = _extract_prompt_from_gemini(content)
        assert result["text"] == "explain this code"
        assert result["timestamp"] == "2026-04-13T07:00:00Z"


# ── get_last_user_prompt with file system ──

class TestGetLastUserPrompt:
    def test_empty_session_id(self):
        result = get_last_user_prompt("")
        assert result["text"] == ""

    def test_cc_session_file(self, tmp_path):
        # Create a CC-style session directory
        project_dir = tmp_path / "project1"
        project_dir.mkdir()
        session_file = project_dir / "test-session-123.jsonl"
        session_file.write_text(
            json.dumps({
                "type": "user",
                "timestamp": "2026-04-15T10:00:00Z",
                "message": {"role": "user", "content": "CC prompt"},
            }) + "\n",
        )
        result = get_last_user_prompt("test-session-123", projects_dir=tmp_path)
        assert result["text"] == "CC prompt"

    def test_codex_session_file(self, tmp_path):
        # Simulate Codex-style path with .codex in the name
        codex_dir = tmp_path / ".codex" / "sessions" / "2026" / "04"
        codex_dir.mkdir(parents=True)
        session_file = codex_dir / "rollout-codex-session-456.jsonl"
        session_file.write_text(
            json.dumps({
                "type": "user",
                "timestamp": "2026-04-14T11:00:00Z",
                "message": {"content": "Codex prompt"},
            }) + "\n",
        )
        # Point to the .codex/sessions dir
        result = get_last_user_prompt(
            "rollout-codex-session-456",
            projects_dir=codex_dir,
        )
        # Direct file match — since codex_dir path contains .codex
        assert result["text"] == "Codex prompt"

    def test_nonexistent_session(self, tmp_path):
        result = get_last_user_prompt("nonexistent-id", projects_dir=tmp_path)
        assert result["text"] == ""


# ── is_cli_process_alive ──

class TestIsCliProcessAlive:
    def test_none_pid(self):
        assert is_cli_process_alive(None) is False

    def test_zero_pid(self):
        assert is_cli_process_alive(0) is False

    def test_negative_pid(self):
        assert is_cli_process_alive(-1) is False

    def test_nonexistent_pid(self):
        assert is_cli_process_alive(999999999) is False


# ── find_cli_pid_by_tty ──

class TestFindCliPidByTty:
    def test_none_tty(self):
        assert find_cli_pid_by_tty(None) is None

    def test_empty_tty(self):
        assert find_cli_pid_by_tty("") is None

    def test_dev_only(self):
        assert find_cli_pid_by_tty("/dev/") is None
