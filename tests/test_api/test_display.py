"""Tests for api/display.py — multi-CLI session prompt extraction and process detection."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from llm_relay.api.display import (
    _extract_prompt_from_cc,
    _extract_prompt_from_codex,
    _extract_prompt_from_gemini,
    _is_cli_process,
    _is_real_user_prompt,
    _parse_codex_session_raw,
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


# ── Codex raw session parsing ──

class TestParseCodexSessionRaw:
    def test_token_count_metrics(self, tmp_path):
        session_file = tmp_path / "rollout-live.jsonl"
        session_file.write_text(
            "\n".join([
                json.dumps({
                    "timestamp": "2026-04-24T00:00:00Z",
                    "type": "response_item",
                    "payload": {
                        "role": "user",
                        "content": [{"text": "first prompt"}],
                    },
                }),
                json.dumps({
                    "timestamp": "2026-04-24T00:00:01Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 1000,
                                "cached_input_tokens": 200,
                                "output_tokens": 50,
                                "total_tokens": 1050,
                            },
                            "last_token_usage": {
                                "input_tokens": 1000,
                                "cached_input_tokens": 200,
                                "output_tokens": 50,
                                "total_tokens": 1050,
                            },
                            "model_context_window": 258400,
                        },
                    },
                }),
                json.dumps({
                    "timestamp": "2026-04-24T00:00:02Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 4500,
                                "cached_input_tokens": 900,
                                "output_tokens": 150,
                                "total_tokens": 4650,
                            },
                            "last_token_usage": {
                                "input_tokens": 2500,
                                "cached_input_tokens": 700,
                                "output_tokens": 100,
                                "total_tokens": 2600,
                            },
                            "model_context_window": 258400,
                        },
                    },
                }),
            ]) + "\n"
        )

        result = _parse_codex_session_raw(session_file)
        assert result["user_turns"] == 1
        assert result["last_user_text"] == "first prompt"
        assert result["current_ctx"] == 2500
        assert result["peak_ctx"] == 2500
        assert result["recent_peak"] == 2500
        assert result["cumul_unique"] == 4650
        assert result["model_window"] == 258400


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


# ── collect_owned_cc_pids ──

class TestCollectOwnedCcPids:
    def test_empty_terminals(self):
        from llm_relay.api.display import collect_owned_cc_pids
        assert collect_owned_cc_pids({}) == set()

    def test_skips_terminals_without_cc_pid(self):
        from llm_relay.api.display import collect_owned_cc_pids
        terms = {"sid": {"tty": "/dev/pts/1"}}
        assert collect_owned_cc_pids(terms) == set()

    def test_includes_only_alive_pids(self, monkeypatch):
        import llm_relay.api.display as disp
        monkeypatch.setattr(disp, "is_cc_process_alive", lambda pid: pid == 1000)
        terms = {
            "alive-sid": {"cc_pid": 1000, "tty": "/dev/pts/1"},
            "dead-sid":  {"cc_pid": 2000, "tty": "/dev/pts/2"},
        }
        assert disp.collect_owned_cc_pids(terms) == {1000}


# ── check_cc_session_alive ──

class TestCheckCcSessionAlive:
    def test_empty_term_returns_false(self):
        from llm_relay.api.display import check_cc_session_alive
        assert check_cc_session_alive({}, 100.0, set(), 200.0) is False

    def test_alive_pid_wins(self, monkeypatch):
        import llm_relay.api.display as disp
        monkeypatch.setattr(disp, "is_cc_process_alive", lambda pid: pid == 1000)
        term = {"cc_pid": 1000, "tty": "/dev/pts/1"}
        assert disp.check_cc_session_alive(term, 100.0, set(), 200.0) is True

    def test_tty_fallback_when_pid_dead(self, monkeypatch):
        import llm_relay.api.display as disp
        monkeypatch.setattr(disp, "is_cc_process_alive", lambda pid: False)
        monkeypatch.setattr(disp, "find_claude_pid_by_tty", lambda tty: 9999)
        term = {"cc_pid": 1000, "tty": "/dev/pts/1"}
        assert disp.check_cc_session_alive(term, 195.0, set(), 200.0) is True

    def test_tty_fallback_rejects_owned_pid(self, monkeypatch):
        """A dead session must not resurrect via TTY when that PID belongs
        to another live registered session."""
        import llm_relay.api.display as disp
        monkeypatch.setattr(disp, "is_cc_process_alive", lambda pid: False)
        monkeypatch.setattr(disp, "find_claude_pid_by_tty", lambda tty: 8000)
        term = {"cc_pid": 7000, "tty": "/dev/pts/1"}
        assert disp.check_cc_session_alive(term, 195.0, {8000}, 200.0) is False

    def test_tty_fallback_skipped_for_stale_last_ts(self, monkeypatch):
        import llm_relay.api.display as disp
        monkeypatch.setattr(disp, "is_cc_process_alive", lambda pid: False)
        monkeypatch.setattr(disp, "find_claude_pid_by_tty", lambda tty: 9999)
        term = {"cc_pid": 1000, "tty": "/dev/pts/1"}
        # last_ts is 1200s old — well past default 600s window
        assert disp.check_cc_session_alive(term, 100.0, set(), 1300.0) is False


# ── _collect_open_session_path_pids ──

class TestCollectOpenSessionPaths:
    def _make_proc_with_fd(self, tmp_path, pid, target_path):
        """Build a fake /proc/<pid>/fd/<n> -> target_path symlink layout."""
        import os
        proc_dir = tmp_path / "proc"
        fd_dir = proc_dir / str(pid) / "fd"
        fd_dir.mkdir(parents=True)
        os.symlink(str(target_path), str(fd_dir / "5"))
        return proc_dir

    def test_finds_jsonl_fd(self, tmp_path):
        from llm_relay.api.display import _collect_open_session_path_pids
        target = tmp_path / "session.jsonl"
        target.write_text("")
        proc_dir = self._make_proc_with_fd(tmp_path, 12345, target)
        paths = _collect_open_session_path_pids(proc_dir)
        assert str(target.resolve()) in paths

    def test_ignores_non_session_files(self, tmp_path):
        from llm_relay.api.display import _collect_open_session_path_pids
        target = tmp_path / "log.txt"
        target.write_text("")
        proc_dir = self._make_proc_with_fd(tmp_path, 12345, target)
        paths = _collect_open_session_path_pids(proc_dir)
        assert str(target.resolve()) not in paths

    def test_skips_non_pid_entries(self, tmp_path):
        from llm_relay.api.display import _collect_open_session_path_pids
        proc_dir = tmp_path / "proc"
        (proc_dir / "self").mkdir(parents=True)
        (proc_dir / "stat").write_text("")
        # Should not raise
        assert _collect_open_session_path_pids(proc_dir) == {}

    def test_finds_json_fd(self, tmp_path):
        from llm_relay.api.display import _collect_open_session_path_pids
        target = tmp_path / "gemini-chat.json"
        target.write_text("")
        proc_dir = self._make_proc_with_fd(tmp_path, 12345, target)
        paths = _collect_open_session_path_pids(proc_dir)
        assert str(target.resolve()) in paths


# ── discover_external_cli_sessions alive filter ──

class TestDiscoverExternalAliveFilter:
    def _make_provider(self, provider_id, sessions):
        """Build a minimal mock provider that returns the given SessionFile list."""
        provider = MagicMock()
        provider.provider_id = provider_id
        provider.display_name = provider_id
        provider.discover_sessions = MagicMock(return_value=sessions)
        return provider

    def _make_session_file(self, tmp_path, name, content):
        path = tmp_path / name
        path.write_text(content)
        sf = MagicMock()
        sf.path = path
        sf.session_id = name.replace(".jsonl", "")
        sf.mtime = path.stat().st_mtime
        return sf

    def test_dead_session_filtered_by_default(self, tmp_path, monkeypatch):
        """A Codex session whose file is NOT held open by any process should be
        filtered out unless include_dead=True."""
        import llm_relay.api.display as disp
        # One Codex session with at least one user turn
        codex_jsonl = (
            '{"type":"response_item","timestamp":"2026-04-16T13:00:00Z",'
            '"payload":{"role":"user","content":[{"text":"hi"}]}}\n'
        )
        sf = self._make_session_file(tmp_path, "rollout-dead.jsonl", codex_jsonl)
        provider = self._make_provider("openai-codex", [sf])
        monkeypatch.setattr(disp, "_collect_open_session_path_pids", lambda *a, **kw: {})
        monkeypatch.setattr(
            "llm_relay.providers.get_all_providers", lambda: [provider], raising=False,
        )

        results = disp.discover_external_cli_sessions(window_hours=24)
        assert results == []

    def test_dead_session_included_when_flag_set(self, tmp_path, monkeypatch):
        import llm_relay.api.display as disp
        codex_jsonl = (
            '{"type":"response_item","timestamp":"2026-04-16T13:00:00Z",'
            '"payload":{"role":"user","content":[{"text":"hi"}]}}\n'
        )
        sf = self._make_session_file(tmp_path, "rollout-dead.jsonl", codex_jsonl)
        provider = self._make_provider("openai-codex", [sf])
        monkeypatch.setattr(disp, "_collect_open_session_path_pids", lambda *a, **kw: {})
        monkeypatch.setattr(
            "llm_relay.providers.get_all_providers", lambda: [provider], raising=False,
        )

        results = disp.discover_external_cli_sessions(window_hours=24, include_dead=True)
        assert len(results) == 1
        assert results[0]["alive"] is False

    def test_alive_session_when_fd_open(self, tmp_path, monkeypatch):
        import llm_relay.api.display as disp
        codex_jsonl = (
            '{"type":"response_item","timestamp":"2026-04-16T13:00:00Z",'
            '"payload":{"role":"user","content":[{"text":"hi"}]}}\n'
        )
        sf = self._make_session_file(tmp_path, "rollout-live.jsonl", codex_jsonl)
        provider = self._make_provider("openai-codex", [sf])
        monkeypatch.setattr(
            disp, "_collect_open_session_path_pids",
            lambda *a, **kw: {str(sf.path.resolve()): 12345},
        )
        monkeypatch.setattr(
            "llm_relay.providers.get_all_providers", lambda: [provider], raising=False,
        )

        results = disp.discover_external_cli_sessions(window_hours=24)
        assert len(results) == 1
        assert results[0]["alive"] is True
        assert results[0]["provider"] == "openai-codex"

    def test_codex_zone_uses_current_ctx_not_cumul_unique(self, tmp_path, monkeypatch):
        """Zone classification must use current_ctx, not cumul_unique."""
        import llm_relay.api.display as disp

        codex_jsonl = (
            '{"type":"response_item","timestamp":"2026-04-16T13:00:00Z",'
            '"payload":{"role":"user","content":[{"text":"hi"}]}}\n'
            '{"type":"event_msg","timestamp":"2026-04-16T13:00:01Z","payload":{"type":"token_count","info":'
            '{"total_token_usage":{"input_tokens":700000,"cached_input_tokens":640000,"output_tokens":5000,"total_tokens":705000},'
            '"last_token_usage":{"input_tokens":80000,"cached_input_tokens":75000,"output_tokens":120,"total_tokens":80120},'
            '"model_context_window":760000}}}\n'
        )
        sf = self._make_session_file(tmp_path, "rollout-cumul.jsonl", codex_jsonl)
        provider = self._make_provider("openai-codex", [sf])
        monkeypatch.setenv("CODEX_TOKEN_DISPLAY_CEILING", "684000")
        monkeypatch.setenv("CODEX_TOKEN_ZONE_CEILING", "760000")
        monkeypatch.setattr(
            disp, "_collect_open_session_path_pids",
            lambda *a, **kw: {str(sf.path.resolve()): 12345},
        )
        monkeypatch.setattr(
            "llm_relay.providers.get_all_providers", lambda: [provider], raising=False,
        )

        results = disp.discover_external_cli_sessions(window_hours=24)
        assert len(results) == 1
        assert results[0]["ceiling"] == 684000
        assert results[0]["current_ctx"] == 80000
        assert results[0]["cumul_unique"] == 705000
        assert results[0]["zone"] == "green"
        assert results[0]["zone_a"] == "green"
        assert results[0]["zone_b"] == "green"

    def test_codex_default_ceiling_is_official_400k(self, tmp_path, monkeypatch):
        """Without env overrides, display & zone ceilings default to Official 400K."""
        import llm_relay.api.display as disp

        codex_jsonl = (
            '{"type":"response_item","timestamp":"2026-04-16T13:00:00Z",'
            '"payload":{"role":"user","content":[{"text":"hi"}]}}\n'
            '{"type":"event_msg","timestamp":"2026-04-16T13:00:01Z","payload":{"type":"token_count","info":'
            '{"total_token_usage":{"input_tokens":300000,"cached_input_tokens":250000,"output_tokens":5000,"total_tokens":305000},'
            '"last_token_usage":{"input_tokens":80000,"cached_input_tokens":75000,"output_tokens":120,"total_tokens":80120},'
            '"model_context_window":258400}}}\n'
        )
        sf = self._make_session_file(tmp_path, "rollout-default.jsonl", codex_jsonl)
        provider = self._make_provider("openai-codex", [sf])
        # Do NOT set CODEX_TOKEN_DISPLAY_CEILING or CODEX_TOKEN_ZONE_CEILING
        monkeypatch.delenv("CODEX_TOKEN_DISPLAY_CEILING", raising=False)
        monkeypatch.delenv("CODEX_TOKEN_ZONE_CEILING", raising=False)
        monkeypatch.setattr(
            disp, "_collect_open_session_path_pids",
            lambda *a, **kw: {str(sf.path.resolve()): 12345},
        )
        monkeypatch.setattr(
            "llm_relay.providers.get_all_providers", lambda: [provider], raising=False,
        )

        results = disp.discover_external_cli_sessions(window_hours=24)
        assert len(results) == 1
        # Display ceiling = Official 400K (not 684K)
        assert results[0]["ceiling"] == 400000
        # current_ctx=80K → 20% of 400K → green for both zones
        assert results[0]["zone"] == "green"
        assert results[0]["zone_a"] == "green"
        assert results[0]["zone_b"] == "green"
        # model_window is still passed through as metadata
        assert results[0]["model_window"] == 258400

    def test_codex_zone_b_independent_of_model_window(self, tmp_path, monkeypatch):
        """Zone B must use the official ceiling, not the model_context_window."""
        import llm_relay.api.display as disp

        # current_ctx=206K, model_window=258K
        # Old behavior: 206/258 = 80% → orange (wrong)
        # New behavior: 206/400 = 52% → yellow (correct)
        codex_jsonl = (
            '{"type":"response_item","timestamp":"2026-04-16T13:00:00Z",'
            '"payload":{"role":"user","content":[{"text":"check"}]}}\n'
            '{"type":"event_msg","timestamp":"2026-04-16T13:00:01Z","payload":{"type":"token_count","info":'
            '{"total_token_usage":{"input_tokens":600000,"cached_input_tokens":400000,"output_tokens":8000,"total_tokens":608000},'
            '"last_token_usage":{"input_tokens":206000,"cached_input_tokens":180000,"output_tokens":500,"total_tokens":206500},'
            '"model_context_window":258400}}}\n'
        )
        sf = self._make_session_file(tmp_path, "rollout-206k.jsonl", codex_jsonl)
        provider = self._make_provider("openai-codex", [sf])
        monkeypatch.delenv("CODEX_TOKEN_DISPLAY_CEILING", raising=False)
        monkeypatch.delenv("CODEX_TOKEN_ZONE_CEILING", raising=False)
        monkeypatch.setattr(
            disp, "_collect_open_session_path_pids",
            lambda *a, **kw: {str(sf.path.resolve()): 12345},
        )
        monkeypatch.setattr(
            "llm_relay.providers.get_all_providers", lambda: [provider], raising=False,
        )

        results = disp.discover_external_cli_sessions(window_hours=24)
        assert len(results) == 1
        assert results[0]["current_ctx"] == 206000
        # Zone A: 206K >= 200K (yellow) — recalibrated threshold
        assert results[0]["zone_a"] == "yellow"
        # Zone B: 206K / 400K = 51.5% → yellow (>= 50%)
        assert results[0]["zone_b"] == "yellow"
        assert results[0]["zone"] == "yellow"

    def test_codex_zone_a_boundary_transitions(self, monkeypatch):
        """Zone A thresholds: 200K/280K/360K/400K."""
        import llm_relay.api.display as disp

        monkeypatch.delenv("CODEX_TOKEN_A_YELLOW", raising=False)
        monkeypatch.delenv("CODEX_TOKEN_A_ORANGE", raising=False)
        monkeypatch.delenv("CODEX_TOKEN_A_RED", raising=False)
        monkeypatch.delenv("CODEX_TOKEN_A_HARD", raising=False)

        # Below yellow
        assert disp._codex_classify_absolute(199999)[0] == "green"
        # Exactly at yellow
        assert disp._codex_classify_absolute(200000)[0] == "yellow"
        # Below orange
        assert disp._codex_classify_absolute(279999)[0] == "yellow"
        # Exactly at orange
        assert disp._codex_classify_absolute(280000)[0] == "orange"
        # Below red
        assert disp._codex_classify_absolute(359999)[0] == "orange"
        # Exactly at red
        assert disp._codex_classify_absolute(360000)[0] == "red"
        # Below hard
        assert disp._codex_classify_absolute(399999)[0] == "red"
        # Exactly at hard
        assert disp._codex_classify_absolute(400000)[0] == "hard"

    def test_codex_zone_b_message_shows_actual_ratio(self, monkeypatch):
        """Zone B messages must show the actual current ratio, not a fixed label."""
        import llm_relay.api.display as disp

        monkeypatch.delenv("CODEX_TOKEN_ZONE_CEILING", raising=False)

        ceiling = 400000
        tokens = 206000  # 51.5%
        result = disp._codex_classify_ratio(tokens, ceiling)
        assert result[0] == "yellow"
        msg = result[3]
        # Message should contain "51%" (actual ratio) and "206K/400K"
        assert "51%" in msg
        assert "206K" in msg
        assert "400K" in msg

        # 75% test
        tokens_orange = 300000  # 75%
        result_orange = disp._codex_classify_ratio(tokens_orange, ceiling)
        assert result_orange[0] == "orange"
        msg_orange = result_orange[3]
        assert "75%" in msg_orange
        assert "300K" in msg_orange
