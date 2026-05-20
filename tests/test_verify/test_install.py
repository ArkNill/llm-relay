"""Tests for verify install checks."""

from __future__ import annotations

from llm_relay.verify import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_WARN,
)
from llm_relay.verify.install import verify_install


class TestVerifyInstall:
    def test_returns_report_with_install_target(self):
        report = verify_install()
        assert report.target == "install"

    def test_runs_all_six_checks(self):
        report = verify_install()
        check_ids = {c.id for c in report.checks}
        assert check_ids == {
            "python_version",
            "package_importable",
            "entry_point_relay",
            "entry_point_mcp",
            "proxy_extras",
            "version_consistency",
        }

    def test_python_version_passes_on_supported_runtime(self):
        # We run our tests on Python >= 3.9; this check should always pass.
        report = verify_install()
        py = next(c for c in report.checks if c.id == "python_version")
        assert py.status == STATUS_PASS
        assert "current" in py.data
        assert "required" in py.data

    def test_package_importable_passes_for_self(self):
        report = verify_install()
        check = next(c for c in report.checks if c.id == "package_importable")
        assert check.status == STATUS_PASS
        assert check.data["module_file"] is not None

    def test_entry_point_mcp_warn_when_missing(self, monkeypatch):
        # shutil.which is the only thing that determines this
        def fake_which(name):
            if name == "llm-relay-mcp":
                return None
            return "/fake/{}".format(name)

        monkeypatch.setattr("llm_relay.verify.install.shutil.which", fake_which)
        report = verify_install()
        check = next(c for c in report.checks if c.id == "entry_point_mcp")
        assert check.status == STATUS_WARN
        assert "pip install" in check.remediation.lower()

    def test_entry_point_relay_fail_when_missing(self, monkeypatch):
        def fake_which(name):
            if name == "llm-relay":
                return None
            return "/fake/{}".format(name)

        monkeypatch.setattr("llm_relay.verify.install.shutil.which", fake_which)
        report = verify_install()
        check = next(c for c in report.checks if c.id == "entry_point_relay")
        assert check.status == STATUS_FAIL
        # The whole report should fail because of this
        assert report.overall == STATUS_FAIL

    def test_proxy_extras_pass_when_all_importable(self):
        # In the test env httpx/uvicorn/starlette are all installed.
        report = verify_install()
        check = next(c for c in report.checks if c.id == "proxy_extras")
        assert check.status == STATUS_PASS

    def test_proxy_extras_warn_when_one_missing(self, monkeypatch):
        import importlib as real_importlib
        original_import = real_importlib.import_module

        def selective_import(name, *args, **kwargs):
            if name == "starlette":
                raise ImportError("simulated missing starlette")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(
            "llm_relay.verify.install.importlib.import_module",
            selective_import,
        )
        report = verify_install()
        check = next(c for c in report.checks if c.id == "proxy_extras")
        assert check.status == STATUS_WARN
        assert "starlette" in check.data["missing"]
