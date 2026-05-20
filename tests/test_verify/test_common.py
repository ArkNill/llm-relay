"""Tests for the shared verify dataclasses and helpers."""

from __future__ import annotations

import pytest

from llm_relay.verify import (
    SCHEMA_VERSION,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIPPED,
    STATUS_WARN,
    VerifyCheck,
    VerifyReport,
    aggregate,
    run_check,
)


class TestVerifyCheck:
    def test_rejects_invalid_status(self):
        with pytest.raises(ValueError):
            VerifyCheck(id="x", label="x", status="bogus", detail="...")

    def test_to_dict_round_trip(self):
        c = VerifyCheck(
            id="x", label="X check", status=STATUS_PASS, detail="ok",
            remediation="do X", data={"k": 1},
        )
        d = c.to_dict()
        assert d == {
            "id": "x", "label": "X check", "status": "pass",
            "detail": "ok", "remediation": "do X", "data": {"k": 1},
        }


class TestVerifyReport:
    def test_empty_report_is_pass(self):
        r = VerifyReport(target="install")
        assert r.overall == STATUS_PASS
        assert r.summary == {"pass": 0, "fail": 0, "warn": 0, "skipped": 0}

    def test_overall_priority_fail_over_warn(self):
        r = VerifyReport(target="install")
        r.checks.append(VerifyCheck(id="a", label="a", status=STATUS_WARN, detail=""))
        r.checks.append(VerifyCheck(id="b", label="b", status=STATUS_FAIL, detail=""))
        r.checks.append(VerifyCheck(id="c", label="c", status=STATUS_PASS, detail=""))
        assert r.overall == STATUS_FAIL

    def test_overall_warn_when_no_fail(self):
        r = VerifyReport(target="install")
        r.checks.append(VerifyCheck(id="a", label="a", status=STATUS_WARN, detail=""))
        r.checks.append(VerifyCheck(id="b", label="b", status=STATUS_PASS, detail=""))
        r.checks.append(VerifyCheck(id="c", label="c", status=STATUS_SKIPPED, detail=""))
        assert r.overall == STATUS_WARN

    def test_skipped_does_not_affect_overall(self):
        r = VerifyReport(target="install")
        r.checks.append(VerifyCheck(id="a", label="a", status=STATUS_SKIPPED, detail=""))
        r.checks.append(VerifyCheck(id="b", label="b", status=STATUS_PASS, detail=""))
        assert r.overall == STATUS_PASS

    def test_summary_counts_by_status(self):
        r = VerifyReport(target="install")
        for status in (STATUS_PASS, STATUS_PASS, STATUS_WARN, STATUS_FAIL, STATUS_SKIPPED):
            r.checks.append(VerifyCheck(id="x", label="x", status=status, detail=""))
        assert r.summary == {"pass": 2, "fail": 1, "warn": 1, "skipped": 1}

    def test_to_dict_includes_schema_version(self):
        r = VerifyReport(target="install")
        d = r.to_dict()
        assert d["schema_version"] == SCHEMA_VERSION
        assert d["target"] == "install"
        assert d["overall"] == "pass"
        assert "captured_at" in d
        assert "summary" in d
        assert d["checks"] == []

    def test_captured_at_is_iso_with_tz(self):
        from datetime import datetime
        r = VerifyReport(target="install")
        parsed = datetime.fromisoformat(r.captured_at)
        assert parsed.tzinfo is not None


class TestRunCheck:
    def test_captures_exception_as_fail(self):
        def boom():
            raise RuntimeError("kaboom")

        c = run_check("x", "X", boom, fallback_remediation="retry")
        assert c.status == STATUS_FAIL
        assert c.id == "x"
        assert c.label == "X"
        assert "RuntimeError" in c.detail
        assert "kaboom" in c.detail
        assert c.remediation == "retry"
        assert c.data == {"_error": True}

    def test_passes_through_normal_result(self):
        def ok():
            return VerifyCheck(
                id="", label="", status=STATUS_PASS, detail="all good",
            )

        c = run_check("real_id", "Real label", ok)
        assert c.status == STATUS_PASS
        assert c.id == "real_id"  # id/label always wins
        assert c.label == "Real label"
        assert c.detail == "all good"


class TestAggregate:
    def test_namespaces_check_ids(self):
        sub_a = VerifyReport(target="install")
        sub_a.checks.append(VerifyCheck(id="x", label="X", status=STATUS_PASS, detail=""))
        sub_b = VerifyReport(target="config")
        sub_b.checks.append(VerifyCheck(id="x", label="X", status=STATUS_FAIL, detail=""))

        combined = aggregate("all", [sub_a, sub_b])
        ids = {c.id for c in combined.checks}
        # Same original id "x" but namespaced by sub-target -- no collision
        assert ids == {"install.x", "config.x"}
        assert combined.overall == STATUS_FAIL

    def test_target_label_preserved(self):
        sub = VerifyReport(target="install")
        sub.checks.append(VerifyCheck(id="x", label="X", status=STATUS_PASS, detail=""))
        combined = aggregate("custom-label", [sub])
        assert combined.target == "custom-label"
