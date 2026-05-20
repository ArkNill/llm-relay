"""Tests for env_fingerprint module."""

from __future__ import annotations

from llm_relay.env_fingerprint import (
    _DEFAULT_PROXY_PORTS,
    _RELEVANT_ENV_VARS,
    SCHEMA_VERSION,
    _safe_call,
    collect_fingerprint,
)


class TestSafeCall:
    def test_returns_value_on_success(self):
        assert _safe_call(lambda: {"k": 1}, default={}) == {"k": 1}

    def test_swallows_exception_and_marks_error(self):
        def boom():
            raise RuntimeError("kaboom")

        result = _safe_call(boom, default={})
        assert "_error" in result
        assert "RuntimeError" in result["_error"]
        assert "kaboom" in result["_error"]
        assert result["_value"] == {}


class TestCollectFingerprint:
    def test_basic_shape(self):
        snap = collect_fingerprint(include_doctor=False)
        assert snap["schema_version"] == SCHEMA_VERSION
        assert "captured_at" in snap
        assert "llm_relay" in snap
        assert "clis" in snap
        assert "ports" in snap
        assert "filesystem" in snap
        assert "env" in snap
        assert "doctor" not in snap  # disabled

    def test_include_doctor_adds_section(self):
        snap = collect_fingerprint(include_doctor=True)
        assert "doctor" in snap
        assert "totals" in snap["doctor"]
        assert "checks" in snap["doctor"]
        assert isinstance(snap["doctor"]["checks"], list)

    def test_default_ports_probed(self):
        snap = collect_fingerprint(include_doctor=False)
        ports = snap["ports"]
        for p in _DEFAULT_PROXY_PORTS:
            assert str(p) in ports
            assert ports[str(p)] in ("free", "in_use")

    def test_custom_ports(self):
        snap = collect_fingerprint(include_doctor=False, ports=[59999])
        assert "59999" in snap["ports"]
        assert "8083" not in snap["ports"]

    def test_clis_section_contains_known_ids_when_present(self):
        snap = collect_fingerprint(include_doctor=False)
        cli_ids = {c["id"] for c in snap["clis"]}
        # discover_all always returns the full registry; ids should match the
        # known set even when the binary is missing.
        assert cli_ids == {"claude-code", "openai-codex", "gemini-cli"}

    def test_cli_entries_have_required_fields(self):
        snap = collect_fingerprint(include_doctor=False)
        for entry in snap["clis"]:
            assert set(entry.keys()) >= {
                "id", "binary_name", "binary_path", "installed",
                "version", "auth", "config_dir", "config_dir_exists",
            }
            assert set(entry["auth"].keys()) >= {
                "cli_authenticated", "api_key_env", "api_key_set", "preferred",
            }

    def test_env_section_redacts_api_keys(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-shouldnotleak-1234567890abcdef")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://localhost:8083")
        snap = collect_fingerprint(include_doctor=False)
        # API key is redacted to a presence marker, never the value
        assert snap["env"]["ANTHROPIC_API_KEY"] == "set"
        assert "shouldnotleak" not in str(snap["env"])
        # Non-secret env vars pass through verbatim
        assert snap["env"]["ANTHROPIC_BASE_URL"] == "http://localhost:8083"

    def test_env_section_handles_unset_api_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        snap = collect_fingerprint(include_doctor=False)
        assert snap["env"]["ANTHROPIC_API_KEY"] is None

    def test_env_section_marks_empty_api_key_as_empty(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        snap = collect_fingerprint(include_doctor=False)
        assert snap["env"]["ANTHROPIC_API_KEY"] == "empty"

    def test_env_section_covers_relevant_vars(self):
        snap = collect_fingerprint(include_doctor=False)
        assert set(snap["env"].keys()) == set(_RELEVANT_ENV_VARS)

    def test_filesystem_section_shape(self):
        snap = collect_fingerprint(include_doctor=False)
        fs = snap["filesystem"]
        assert "home" in fs
        assert "knowledge_dir" in fs
        assert "session_count" in fs
        assert isinstance(fs["session_count"], int)

    def test_subprobe_failure_does_not_crash_collection(self, monkeypatch):
        # Force the doctor probe to raise; collection should still produce a
        # snapshot with an error marker on the doctor section only.
        def boom(fix=False):
            raise RuntimeError("doctor exploded")

        monkeypatch.setattr("llm_relay.recover.doctor.run_doctor", boom)
        snap = collect_fingerprint(include_doctor=True)
        assert "_error" in snap["doctor"]
        assert "doctor exploded" in snap["doctor"]["_error"]
        # Other sections remain intact
        assert "clis" in snap
        assert isinstance(snap["clis"], list)

    def test_captured_at_is_utc_iso(self):
        snap = collect_fingerprint(include_doctor=False)
        ts = snap["captured_at"]
        # ISO 8601 with timezone offset; should parse with datetime
        from datetime import datetime
        parsed = datetime.fromisoformat(ts)
        assert parsed.tzinfo is not None


class TestSchemaContract:
    """Ensures the schema version is bumped intentionally when shape changes."""

    def test_schema_version_is_string(self):
        assert isinstance(SCHEMA_VERSION, str)
        assert SCHEMA_VERSION  # non-empty

    def test_relevant_env_vars_are_documented(self):
        # If a new env var is added to _RELEVANT_ENV_VARS, this test does not
        # gate it directly -- but it forces the test reader to be aware that
        # adding env vars is part of the schema contract.
        assert "ANTHROPIC_BASE_URL" in _RELEVANT_ENV_VARS
        assert "LLM_RELAY_DB" in _RELEVANT_ENV_VARS
