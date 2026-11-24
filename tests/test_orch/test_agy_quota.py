"""Tests for orch/agy_quota.py and the agy quota preflight in execute_cli.

No real agy is started: subprocess.run is patched everywhere a query or a call
would run. The fixture is a real ``agy -p "/usage" --output-format json``
envelope (agy 1.5.16, 2026-11-24; no secrets).
"""

from __future__ import annotations

import dataclasses
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from llm_relay.orch.agy_quota import (
    after_min_seconds,
    block_message,
    check_enabled,
    evaluate,
    fetch_usage,
    parse_usage,
    quota_group,
    relevant_buckets,
    summarize,
    thresholds_from_env,
    usage_command,
)
from llm_relay.orch.executor import _build_antigravity_cmd, execute_cli
from llm_relay.orch.models import AuthMethod, CLIStatus

_FIXTURE = Path(__file__).parent / "fixtures" / "agy_usage.json"
# Fixture time: gemini-5h resets in 2h 55m (175 min), gemini-weekly in 9619 min.
_NOW = datetime(2026, 11, 24, 7, 56, 5, tzinfo=timezone.utc)
_RESET_5H = "2026-11-24T10:51:05Z"
_RESET_WEEKLY = "2026-12-01T00:15:23Z"
_EPOCH = "1970-01-01T00:00:00Z"

_QUOTA_ENV = (
    "LLM_RELAY_AGY_QUOTA_CHECK",
    "LLM_RELAY_AGY_QUOTA_BLOCK_PCT",
    "LLM_RELAY_AGY_QUOTA_WARN_PCT",
    "LLM_RELAY_AGY_QUOTA_AFTER_MIN_S",
    "LLM_RELAY_AGY_DEFAULT_MODEL",
    "LLM_RELAY_AGY_PERMISSIONS",
)

_AGY_SUCCESS = json.dumps({"conversation_id": "c1", "status": "SUCCESS", "response": "pong\n", "num_turns": 1})


@pytest.fixture(autouse=True)
def _clean_quota_env(monkeypatch):
    """Defaults only: the operator's shell must not change what these tests see."""
    for name in _QUOTA_ENV:
        monkeypatch.delenv(name, raising=False)


def _fixture_text() -> str:
    return _FIXTURE.read_text(encoding="utf-8")


def _envelope(fractions: Dict[str, Any], resets: Optional[Dict[str, str]] = None) -> str:
    """A /usage envelope with one group holding the given {bucket id: remaining_fraction}."""
    resets = resets or {}
    buckets = []
    for bucket_id, fraction in fractions.items():
        default_reset = _RESET_5H if bucket_id.endswith("-5h") else _RESET_WEEKLY
        bucket = {"id": bucket_id, "name": bucket_id, "window": bucket_id.rsplit("-", 1)[-1],
                  "reset_time": resets.get(bucket_id, default_reset)}
        if fraction is not None:
            bucket["remaining_fraction"] = fraction
        buckets.append(bucket)
    return json.dumps({
        "conversation_id": "", "status": "SUCCESS", "response": "", "num_turns": 0,
        "command": {"name": "usage", "data": {"groups": [{"name": "g", "buckets": buckets}]}},
    })


def _buckets(fractions: Dict[str, Any], resets: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    buckets, reason = parse_usage(_envelope(fractions, resets))
    assert reason is None
    return buckets


def _proc(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


def _agy_cli(path: str = "/usr/bin/agy") -> CLIStatus:
    return CLIStatus(
        cli_id="antigravity-cli", binary_name="agy", binary_path=path, installed=True, cli_authenticated=True,
        api_key_name=None, api_key_available=False, preferred_auth=AuthMethod.CLI_OAUTH,
    )


def _claude_cli() -> CLIStatus:
    return CLIStatus(
        cli_id="claude-code", binary_name="claude", binary_path="/usr/bin/claude", installed=True,
        cli_authenticated=True, api_key_name=None, api_key_available=False, preferred_auth=AuthMethod.CLI_OAUTH,
    )


class _Runner:
    """subprocess.run stand-in: /usage queries pop ``usage`` in order, anything else gets ``main``.

    An entry that is an exception instance is raised instead of returned.
    """

    def __init__(self, usage: List[Any], main: Any = None):
        self.usage = list(usage)
        self.main = main if main is not None else _proc(_AGY_SUCCESS)
        self.calls: List[Any] = []

    @staticmethod
    def is_usage(cmd: List[str]) -> bool:
        return cmd[1:3] == ["-p", "/usage"]

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        outcome = self.usage.pop(0) if self.is_usage(cmd) else self.main
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    @property
    def commands(self) -> List[List[str]]:
        return [cmd for cmd, _ in self.calls]


# ── parse_usage ──


class TestParseUsage:
    def test_fixture(self):
        buckets, reason = parse_usage(_fixture_text())
        assert reason is None
        by_id = {b["id"]: b for b in buckets}
        assert list(by_id) == ["gemini-weekly", "gemini-5h", "3p-weekly", "3p-5h"]
        assert by_id["gemini-5h"] == {
            "id": "gemini-5h", "window": "5h", "remaining_pct": 28.0, "reset_time": _RESET_5H,
        }
        assert by_id["gemini-weekly"]["remaining_pct"] == 75.3
        assert by_id["3p-5h"]["remaining_pct"] == 100.0
        assert by_id["3p-5h"]["reset_time"] == _EPOCH

    @pytest.mark.parametrize("stdout", ["", "  \n", None])
    def test_empty(self, stdout):
        buckets, reason = parse_usage(stdout)
        assert buckets is None and "empty" in reason

    def test_not_json(self):
        assert parse_usage("Gemini Models\tWeekly\t75%") == (None, "usage output is not JSON")

    def test_not_an_object(self):
        assert parse_usage("[1, 2]") == (None, "usage output is not a JSON object")

    def test_status_error_carries_error_text(self):
        buckets, reason = parse_usage(json.dumps({"status": "ERROR", "error": "not signed in"}))
        assert buckets is None
        assert reason == "usage status ERROR: not signed in"

    def test_status_missing(self):
        assert parse_usage(json.dumps({"command": {}})) == (None, "usage status None")

    @pytest.mark.parametrize("command", [None, {"name": "usage"}, {"data": {}}, {"data": {"groups": {}}}])
    def test_groups_missing_or_not_a_list(self, command):
        buckets, reason = parse_usage(json.dumps({"status": "SUCCESS", "command": command}))
        assert buckets is None and "groups" in reason

    def test_no_buckets(self):
        stdout = json.dumps({"status": "SUCCESS", "command": {"data": {"groups": [{"buckets": []}, "junk"]}}})
        assert parse_usage(stdout) == (None, "usage output has no quota buckets")

    def test_bucket_without_id_is_skipped(self):
        stdout = json.dumps({"status": "SUCCESS", "command": {"data": {"groups": [{"buckets": [
            {"name": "no id", "remaining_fraction": 0.5}, {"id": "gemini-5h", "remaining_fraction": 0.5},
        ]}]}}})
        buckets, reason = parse_usage(stdout)
        assert reason is None and [b["id"] for b in buckets] == ["gemini-5h"]

    @pytest.mark.parametrize("fraction", [None, "0.5", True, float("nan")])
    def test_non_numeric_fraction_gives_none(self, fraction):
        stdout = json.dumps({"status": "SUCCESS", "command": {"data": {"groups": [{"buckets": [
            {"id": "gemini-5h", "remaining_fraction": fraction, "reset_time": 5},
        ]}]}}})
        buckets, reason = parse_usage(stdout)
        assert reason is None
        assert buckets == [{"id": "gemini-5h", "window": None, "remaining_pct": None, "reset_time": None}]

    def test_missing_fraction_gives_none(self):
        assert _buckets({"gemini-5h": None})[0]["remaining_pct"] is None


# ── fetch_usage ──


class TestFetchUsage:
    @patch("llm_relay.orch.agy_quota.subprocess.run")
    def test_success_and_call_shape(self, mock_run):
        mock_run.return_value = _proc(_fixture_text())
        buckets, reason = fetch_usage("/usr/bin/agy", cwd="/repo")
        assert reason is None and len(buckets) == 4
        assert mock_run.call_args.args[0] == usage_command("/usr/bin/agy")
        assert usage_command("/x/agy") == [
            "/x/agy", "-p", "/usage", "--output-format", "json", "--print-timeout", "30s",
        ]
        kwargs = mock_run.call_args.kwargs
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["timeout"] == 45 and kwargs["cwd"] == "/repo"
        assert kwargs["capture_output"] is True and kwargs["text"] is True
        assert kwargs["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"

    @patch("llm_relay.orch.agy_quota.subprocess.run")
    def test_nonzero_rc_and_unparsable(self, mock_run):
        mock_run.return_value = _proc("crash", returncode=2)
        assert fetch_usage("/usr/bin/agy") == (None, "usage query exited 2: usage output is not JSON")

    @patch("llm_relay.orch.agy_quota.subprocess.run")
    def test_nonzero_rc_with_good_envelope_still_parses(self, mock_run):
        mock_run.return_value = _proc(_fixture_text(), returncode=1)
        buckets, reason = fetch_usage("/usr/bin/agy")
        assert reason is None and buckets

    @patch("llm_relay.orch.agy_quota.subprocess.run")
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["agy"], timeout=7)
        assert fetch_usage("/usr/bin/agy", timeout=7) == (None, "usage query timed out after 7s")

    @patch("llm_relay.orch.agy_quota.subprocess.run")
    def test_os_error(self, mock_run):
        mock_run.side_effect = OSError("No such file or directory")
        assert fetch_usage("/usr/bin/agy") == (None, "No such file or directory")


# ── grouping ──


class TestQuotaGroup:
    @pytest.mark.parametrize("model", [None, "", "gemini-4-neon-xhigh", "Gemini-4-Argon-High", "gemini-3.8-flash"])
    def test_gemini(self, model):
        assert quota_group(model) == "gemini"

    @pytest.mark.parametrize("model", ["claude-opus-5.5", "Claude-Sonnet-5.5", "gpt-oss-120b", "GPT-OSS-120B"])
    def test_third_party(self, model):
        assert quota_group(model) == "3p"

    @pytest.mark.parametrize("model", ["llama-3", "orchestrator-neon", "neon"])
    def test_unknown(self, model):
        assert quota_group(model) is None


class TestRelevantBuckets:
    _ALL = {"gemini-5h": 0.5, "gemini-weekly": 0.5, "gemini-argon-wtu-weekly": 0.5, "3p-5h": 1, "3p-weekly": 1}

    def _ids(self, group, model):
        return [b["id"] for b in relevant_buckets(_buckets(self._ALL), group, model)]

    def test_neon_skips_argon_bucket(self):
        assert self._ids("gemini", "gemini-4-neon-xhigh") == ["gemini-5h", "gemini-weekly"]

    def test_argon_model_includes_argon_bucket(self):
        assert self._ids("gemini", "gemini-4-argon-high") == ["gemini-5h", "gemini-weekly", "gemini-argon-wtu-weekly"]

    def test_argon_match_is_case_insensitive(self):
        assert "gemini-argon-wtu-weekly" in self._ids("gemini", "Gemini-4-ARGON-high")

    def test_third_party_group(self):
        assert self._ids("3p", "claude-sonnet-5.5") == ["3p-5h", "3p-weekly"]


# ── summarize / evaluate / block_message ──


class TestSummarize:
    def test_fixture_resets(self):
        buckets, _ = parse_usage(_fixture_text())
        summary = summarize(buckets, now=_NOW)
        assert summary["gemini-5h"] == {"remaining_pct": 28.0, "reset_time": _RESET_5H, "resets_in_min": 175}
        assert summary["gemini-weekly"]["resets_in_min"] == 9619
        assert summary["3p-5h"]["resets_in_min"] is None  # 1970 epoch for an unused bucket

    def test_minutes_round_down(self):
        reset = (_NOW + timedelta(minutes=59, seconds=59)).strftime("%Y-%m-%dT%H:%M:%SZ")
        summary = summarize(_buckets({"gemini-5h": 0.5}, {"gemini-5h": reset}), now=_NOW)
        assert summary["gemini-5h"]["resets_in_min"] == 59

    @pytest.mark.parametrize("reset", ["soon", "", "2026-11-24T07:00:00Z"])
    def test_unparsable_or_past_reset(self, reset):
        summary = summarize(_buckets({"gemini-5h": 0.5}, {"gemini-5h": reset}), now=_NOW)
        assert summary["gemini-5h"]["resets_in_min"] is None

    def test_offset_and_fractional_seconds(self):
        resets = {"gemini-5h": "2026-11-24T17:56:05.123456789+09:00", "gemini-weekly": "2026-11-24T08:56:05"}
        summary = summarize(_buckets({"gemini-5h": 0.5, "gemini-weekly": 0.5}, resets), now=_NOW)
        assert summary["gemini-5h"]["resets_in_min"] == 60
        assert summary["gemini-weekly"]["resets_in_min"] == 60  # naive = UTC

    def test_default_now(self):
        far = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        summary = summarize(_buckets({"gemini-5h": 0.5}, {"gemini-5h": far}))
        assert 178 <= summary["gemini-5h"]["resets_in_min"] <= 180


class TestEvaluate:
    def _eval(self, fractions, model="gemini-4-neon-xhigh", block_pct=10.0, warn_pct=30.0):
        return evaluate(_buckets(fractions), model, block_pct=block_pct, warn_pct=warn_pct, now=_NOW)

    def test_fixture_is_warn(self):
        buckets, _ = parse_usage(_fixture_text())
        quota = evaluate(buckets, "gemini-4-neon-xhigh", block_pct=10, warn_pct=30, now=_NOW)
        assert quota == {
            "verdict": "warn", "group": "gemini", "block_pct": 10.0, "warn_pct": 30.0,
            "before": {
                "gemini-weekly": {"remaining_pct": 75.3, "reset_time": _RESET_WEEKLY, "resets_in_min": 9619},
                "gemini-5h": {"remaining_pct": 28.0, "reset_time": _RESET_5H, "resets_in_min": 175},
            },
            "note": "gemini-5h 28.0% < warn 30%",
        }

    def test_ok_has_no_note(self):
        quota = self._eval({"gemini-5h": 0.5, "gemini-weekly": 0.9})
        assert quota["verdict"] == "ok" and "note" not in quota

    def test_floor_is_strict(self):
        assert self._eval({"gemini-5h": 0.1, "gemini-weekly": 0.9})["verdict"] == "warn"  # 10.0 passes
        assert self._eval({"gemini-5h": 0.099, "gemini-weekly": 0.9})["verdict"] == "blocked"  # 9.9 stops

    def test_warn_is_strict(self):
        assert self._eval({"gemini-5h": 0.3, "gemini-weekly": 0.9})["verdict"] == "ok"
        assert self._eval({"gemini-5h": 0.299, "gemini-weekly": 0.9})["verdict"] == "warn"

    def test_blocked_note_lists_every_bucket_under_the_floor(self):
        quota = self._eval({"gemini-weekly": 0.05, "gemini-5h": 0.04})
        assert quota["verdict"] == "blocked"
        assert quota["note"] == "gemini-5h 4.0% < floor 10%; gemini-weekly 5.0% < floor 10%"

    def test_block_pct_zero_never_blocks(self):
        quota = self._eval({"gemini-5h": 0.0, "gemini-weekly": 0.9}, block_pct=0)
        assert quota["verdict"] == "warn" and quota["note"] == "gemini-5h 0.0% < warn 30%"

    def test_non_integer_threshold_kept(self):
        quota = self._eval({"gemini-5h": 0.12, "gemini-weekly": 0.9}, block_pct=12.5)
        assert quota["note"] == "gemini-5h 12.0% < floor 12.5%"

    def test_unknown_model(self):
        quota = self._eval({"gemini-5h": 0.01}, model="llama-3")
        assert quota == {
            "verdict": "unknown", "group": None, "block_pct": 10.0, "warn_pct": 30.0,
            "note": "no quota group known for model 'llama-3'",
        }

    def test_no_bucket_for_group(self):
        quota = self._eval({"3p-5h": 0.01, "3p-weekly": 0.01})
        assert quota["verdict"] == "unknown" and quota["note"] == "no quota bucket for group 'gemini'"

    def test_bucket_without_fraction_is_not_a_reading(self):
        quota = self._eval({"gemini-5h": None, "gemini-weekly": None})
        assert quota["verdict"] == "unknown" and quota["note"] == "no quota bucket for group 'gemini'"

    def test_third_party_model_reads_3p_buckets(self):
        quota = self._eval({"gemini-5h": 0.01, "3p-5h": 0.5, "3p-weekly": 0.9}, model="claude-opus-5.5")
        assert quota["verdict"] == "ok" and set(quota["before"]) == {"3p-5h", "3p-weekly"}

    def test_argon_bucket_only_counts_for_argon(self):
        fractions = {"gemini-5h": 0.9, "gemini-weekly": 0.9, "gemini-argon-wtu-weekly": 0.02}
        assert self._eval(fractions, model="gemini-4-neon-xhigh")["verdict"] == "ok"
        assert self._eval(fractions, model="gemini-4-argon-high")["verdict"] == "blocked"


class TestBlockMessage:
    _LOCAL = re.compile(r"; resets \d{4}-\d{2}-\d{2} \d{2}:\d{2}.*?, in ")

    def _quota(self, fractions, resets=None):
        return evaluate(_buckets(fractions, resets), "gemini-4-neon-xhigh", block_pct=10, warn_pct=30, now=_NOW)

    def test_single_bucket(self):
        msg = block_message(self._quota({"gemini-5h": 0.04, "gemini-weekly": 0.75}))
        assert msg.startswith("quota preflight: gemini-5h has 4.0% left (floor 10%); resets ")
        assert msg.endswith(", in 2h 55m. agy was not started.")
        assert self._LOCAL.search(msg)  # local time; its text depends on the machine's time zone
        assert "gemini-weekly" not in msg

    def test_under_an_hour(self):
        reset = (_NOW + timedelta(minutes=55, seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        msg = block_message(self._quota({"gemini-5h": 0.04}, {"gemini-5h": reset}))
        assert msg.endswith(", in 55m. agy was not started.")

    def test_unknown_reset_drops_the_clause(self):
        msg = block_message(self._quota({"gemini-5h": 0.04}, {"gemini-5h": _EPOCH}))
        assert msg == "quota preflight: gemini-5h has 4.0% left (floor 10%). agy was not started."

    def test_several_buckets(self):
        msg = block_message(self._quota({"gemini-weekly": 0.05, "gemini-5h": 0.04}))
        assert msg.startswith("quota preflight: gemini-5h has 4.0% left (floor 10%); resets ")
        assert "; gemini-weekly has 5.0% left (floor 10%); resets " in msg
        assert msg.endswith(". agy was not started.")

    def test_without_readings_still_has_prefix(self):
        msg = block_message({"verdict": "blocked", "block_pct": 10.0})
        assert msg.startswith("quota preflight:") and msg.endswith("agy was not started.")


# ── environment ──


class TestEnv:
    def test_defaults(self):
        assert thresholds_from_env() == (10.0, 30.0)
        assert check_enabled() is True
        assert after_min_seconds() == 60.0

    def test_valid_values(self, monkeypatch):
        monkeypatch.setenv("LLM_RELAY_AGY_QUOTA_BLOCK_PCT", "5")
        monkeypatch.setenv("LLM_RELAY_AGY_QUOTA_WARN_PCT", "50.5")
        monkeypatch.setenv("LLM_RELAY_AGY_QUOTA_AFTER_MIN_S", "0")
        assert thresholds_from_env() == (5.0, 50.5)
        assert after_min_seconds() == 0.0

    @pytest.mark.parametrize("raw", ["abc", "", "-1", "100.1", "nan", "inf"])
    def test_invalid_thresholds_fall_back(self, monkeypatch, raw):
        monkeypatch.setenv("LLM_RELAY_AGY_QUOTA_BLOCK_PCT", raw)
        monkeypatch.setenv("LLM_RELAY_AGY_QUOTA_WARN_PCT", raw)
        assert thresholds_from_env() == (10.0, 30.0)

    @pytest.mark.parametrize("raw", ["abc", "-5", "nan"])
    def test_invalid_after_min_falls_back(self, monkeypatch, raw):
        monkeypatch.setenv("LLM_RELAY_AGY_QUOTA_AFTER_MIN_S", raw)
        assert after_min_seconds() == 60.0

    @pytest.mark.parametrize("raw,enabled", [("0", False), ("1", True), ("false", True), ("", True)])
    def test_check_enabled(self, monkeypatch, raw, enabled):
        monkeypatch.setenv("LLM_RELAY_AGY_QUOTA_CHECK", raw)
        assert check_enabled() is enabled

    def test_read_on_every_call(self, monkeypatch):
        assert thresholds_from_env()[0] == 10.0
        monkeypatch.setenv("LLM_RELAY_AGY_QUOTA_BLOCK_PCT", "20")
        assert thresholds_from_env()[0] == 20.0
        monkeypatch.setenv("LLM_RELAY_AGY_QUOTA_CHECK", "0")
        assert check_enabled() is False


# ── execute_cli preflight ──


_LOW = _envelope({"gemini-weekly": 0.75, "gemini-5h": 0.04})
_HEALTHY = _envelope({"gemini-weekly": 0.8, "gemini-5h": 0.5})
_SPENT = _envelope({"gemini-weekly": 0.79, "gemini-5h": 0.45})


class TestExecutePreflight:
    def _run(self, runner, cli=None, **kwargs):
        with patch("llm_relay.orch.executor.subprocess.run", side_effect=runner):
            return execute_cli(cli or _agy_cli(), "ping", **kwargs)

    def test_blocked_does_not_start_agy(self):
        runner = _Runner([_proc(_LOW)])
        result = self._run(runner, timeout=120)
        assert len(runner.calls) == 1 and runner.is_usage(runner.commands[0])
        assert result.success is False and result.output == "" and result.exit_code == -1
        assert result.error.startswith("quota preflight: gemini-5h has 4.0% left (floor 10%)")
        assert result.quota["verdict"] == "blocked"
        assert result.quota["note"] == "gemini-5h 4.0% < floor 10%"
        assert result.duration_ms >= 0

    def test_pass_runs_the_unchanged_command(self):
        runner = _Runner([_proc(_HEALTHY)])
        result = self._run(runner, timeout=120)
        assert runner.commands[0] == usage_command("/usr/bin/agy")
        main_cmd, main_kwargs = runner.calls[1]
        assert main_cmd == _build_antigravity_cmd(_agy_cli(), "ping", timeout=120)
        assert main_kwargs["timeout"] == 120 and main_kwargs["cwd"] is None and main_kwargs["env"] is None
        assert main_kwargs["stdin"] == subprocess.DEVNULL
        assert len(runner.calls) == 2  # default AFTER_MIN_S=60: a quick call is not re-queried
        assert result.success is True and result.output == "pong\n" and result.error is None
        assert result.quota["verdict"] == "ok"
        assert set(result.quota["before"]) == {"gemini-weekly", "gemini-5h"}
        assert "after" not in result.quota and "used_pct" not in result.quota

    def test_warn_still_runs(self):
        runner = _Runner([_proc(_envelope({"gemini-5h": 0.2, "gemini-weekly": 0.9}))])
        result = self._run(runner)
        assert len(runner.calls) == 2 and result.success is True
        assert result.quota["verdict"] == "warn" and result.quota["note"] == "gemini-5h 20.0% < warn 30%"

    def test_after_query_records_spent_quota(self, monkeypatch):
        monkeypatch.setenv("LLM_RELAY_AGY_QUOTA_AFTER_MIN_S", "0")
        runner = _Runner([_proc(_HEALTHY), _proc(_SPENT)])
        result = self._run(runner)
        assert len(runner.calls) == 3
        assert runner.is_usage(runner.commands[0]) and not runner.is_usage(runner.commands[1])
        assert runner.is_usage(runner.commands[2])
        assert result.quota["after"]["gemini-5h"]["remaining_pct"] == 45.0
        assert result.quota["used_pct"] == {"gemini-weekly": 1.0, "gemini-5h": 5.0}

    def test_after_query_failure_is_noted(self, monkeypatch):
        monkeypatch.setenv("LLM_RELAY_AGY_QUOTA_AFTER_MIN_S", "0")
        runner = _Runner([_proc(_HEALTHY), subprocess.TimeoutExpired(cmd=["agy"], timeout=45)])
        result = self._run(runner)
        assert len(runner.calls) == 3 and result.success is True
        assert result.quota["after_note"] == "usage query timed out after 45s"
        assert "after" not in result.quota

    @pytest.mark.parametrize("failure,note", [
        (_proc("not json"), "usage output is not JSON"),
        (subprocess.TimeoutExpired(cmd=["agy"], timeout=45), "usage query timed out after 45s"),
        (OSError("Permission denied"), "Permission denied"),
    ])
    def test_failed_query_fails_open(self, monkeypatch, failure, note):
        monkeypatch.setenv("LLM_RELAY_AGY_QUOTA_AFTER_MIN_S", "0")
        runner = _Runner([failure])
        result = self._run(runner)
        assert len(runner.calls) == 2  # the call still runs; no follow-up query without a reading
        assert result.success is True
        assert result.quota == {"verdict": "unknown", "note": note}

    def test_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("LLM_RELAY_AGY_QUOTA_CHECK", "0")
        runner = _Runner([_proc(_LOW)])
        result = self._run(runner)
        assert len(runner.calls) == 1 and not runner.is_usage(runner.commands[0])
        assert result.success is True and result.quota is None

    def test_disabled_by_argument(self):
        runner = _Runner([_proc(_LOW)])
        result = self._run(runner, quota_check=False)
        assert len(runner.calls) == 1 and not runner.is_usage(runner.commands[0])
        assert result.success is True and result.quota is None

    @pytest.mark.parametrize("cli_id,name", [("claude-code", "claude"), ("gemini-cli", "gemini")])
    def test_other_clis_untouched(self, cli_id, name):
        cli = dataclasses.replace(_claude_cli(), cli_id=cli_id, binary_name=name, binary_path="/usr/bin/" + name)
        runner = _Runner([], main=_proc(json.dumps({"result": "done"})))
        result = self._run(runner, cli=cli)
        assert len(runner.calls) == 1
        assert result.success is True and result.quota is None

    def test_argument_floor_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("LLM_RELAY_AGY_QUOTA_BLOCK_PCT", "50")
        twenty = _envelope({"gemini-5h": 0.2, "gemini-weekly": 0.9})
        result_env = self._run(_Runner([_proc(twenty)]))
        assert result_env.quota["verdict"] == "blocked" and result_env.quota["block_pct"] == 50.0
        runner = _Runner([_proc(twenty)])
        result_arg = self._run(runner, quota_block_pct=3)
        assert len(runner.calls) == 2 and result_arg.success is True
        assert result_arg.quota["verdict"] == "warn" and result_arg.quota["block_pct"] == 3.0

    def test_argument_floor_can_block(self, monkeypatch):
        monkeypatch.setenv("LLM_RELAY_AGY_QUOTA_BLOCK_PCT", "5")
        runner = _Runner([_proc(_envelope({"gemini-5h": 0.2, "gemini-weekly": 0.9}))])
        result = self._run(runner, quota_block_pct=25)
        assert len(runner.calls) == 1 and result.quota["verdict"] == "blocked"
        assert result.error.startswith("quota preflight: gemini-5h has 20.0% left (floor 25%)")

    def test_main_timeout_keeps_quota(self, monkeypatch):
        monkeypatch.setenv("LLM_RELAY_AGY_QUOTA_AFTER_MIN_S", "0")
        runner = _Runner([_proc(_HEALTHY), _proc(_SPENT)], main=subprocess.TimeoutExpired(cmd=["agy"], timeout=5))
        result = self._run(runner, timeout=5)
        assert result.success is False and result.error == "Execution timed out after 5s"
        assert result.quota["verdict"] == "ok"
        assert len(runner.calls) == 3 and result.quota["used_pct"]["gemini-5h"] == 5.0

    def test_main_os_error_keeps_quota(self):
        runner = _Runner([_proc(_HEALTHY)], main=OSError("exec format error"))
        result = self._run(runner)
        assert result.success is False and result.error == "exec format error"
        assert result.quota["verdict"] == "ok"

    def test_main_failure_keeps_quota(self):
        failed = _proc(json.dumps({"status": "ERROR", "error": "quota exhausted", "response": ""}), returncode=1)
        runner = _Runner([_proc(_HEALTHY)], main=failed)
        result = self._run(runner)
        assert result.success is False and result.error == "quota exhausted"
        assert result.quota["verdict"] == "ok"

    def test_model_selects_the_buckets(self, monkeypatch):
        low_argon = _envelope({"gemini-5h": 0.9, "gemini-weekly": 0.9, "gemini-argon-wtu-weekly": 0.01})
        assert self._run(_Runner([_proc(low_argon)]), model="gemini-4-neon-high").quota["verdict"] == "ok"
        assert self._run(_Runner([_proc(low_argon)]), model="gemini-4-argon-high").quota["verdict"] == "blocked"
        monkeypatch.setenv("LLM_RELAY_AGY_DEFAULT_MODEL", "gemini-4-argon-low")
        assert self._run(_Runner([_proc(low_argon)])).quota["verdict"] == "blocked"

    def test_working_dir_used_only_when_it_exists(self, tmp_path):
        runner = _Runner([_proc(_HEALTHY)])
        self._run(runner, working_dir=str(tmp_path))
        assert runner.calls[0][1]["cwd"] == str(tmp_path) and runner.calls[1][1]["cwd"] == str(tmp_path)
        runner = _Runner([_proc(_HEALTHY)], main=OSError("missing cwd"))
        self._run(runner, working_dir=str(tmp_path / "missing"))
        assert runner.calls[0][1]["cwd"] is None

    def test_older_result_class_without_quota_field(self):
        @dataclasses.dataclass
        class OldDelegationResult:  # DelegationResult as it was before the quota field
            cli_id: str
            auth_method: AuthMethod
            success: bool
            output: str
            error: Optional[str] = None
            duration_ms: float = 0.0
            exit_code: int = 0
            model_used: Optional[str] = None

        with patch("llm_relay.orch.executor.DelegationResult", OldDelegationResult):
            blocked = self._run(_Runner([_proc(_LOW)]))
            passed = self._run(_Runner([_proc(_HEALTHY)]))
        assert isinstance(blocked, OldDelegationResult) and blocked.quota["verdict"] == "blocked"
        assert passed.success is True and passed.quota["verdict"] == "ok"

    def test_preflight_logs_one_line(self, caplog):
        with caplog.at_level("INFO", logger="llm_relay.orch.executor"):
            self._run(_Runner([_proc(_LOW)]))
        lines = [r.getMessage() for r in caplog.records if "quota preflight" in r.getMessage()]
        assert len(lines) == 1
        assert "verdict=blocked" in lines[0] and "lowest=gemini-5h 4.0%" in lines[0]
