"""verify install -- confirm the llm-relay package itself is usable.

Checks are ordered from most-fundamental (Python version) to most-optional
(MCP entry point). An agent reading the report should be able to act on the
remediation strings directly.
"""

from __future__ import annotations

import importlib
import shutil
import sys
from importlib.metadata import PackageNotFoundError, version

from llm_relay.verify import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIPPED,
    STATUS_WARN,
    VerifyCheck,
    VerifyReport,
    run_check,
)

_MIN_PYTHON = (3, 9)


def _check_python_version() -> VerifyCheck:
    cur = sys.version_info[:3]
    if cur[:2] >= _MIN_PYTHON:
        return VerifyCheck(
            id="", label="",  # filled by run_check
            status=STATUS_PASS,
            detail="Python {}.{}.{}".format(*cur),
            data={"required": "{}.{}+".format(*_MIN_PYTHON), "current": ".".join(map(str, cur))},
        )
    return VerifyCheck(
        id="", label="",
        status=STATUS_FAIL,
        detail="Python {}.{}.{} is below the required {}.{}+".format(*cur, *_MIN_PYTHON),
        remediation="Install Python {}.{} or newer.".format(*_MIN_PYTHON),
        data={"required": "{}.{}+".format(*_MIN_PYTHON), "current": ".".join(map(str, cur))},
    )


def _check_package_importable() -> VerifyCheck:
    try:
        mod = importlib.import_module("llm_relay")
    except ImportError as exc:
        return VerifyCheck(
            id="", label="",
            status=STATUS_FAIL,
            detail="import llm_relay failed: {}".format(exc),
            remediation="pip install llm-relay",
        )
    return VerifyCheck(
        id="", label="",
        status=STATUS_PASS,
        detail="llm_relay imported from {}".format(getattr(mod, "__file__", "(builtin)")),
        data={"module_file": getattr(mod, "__file__", None)},
    )


def _check_entry_point_relay() -> VerifyCheck:
    path = shutil.which("llm-relay")
    if path:
        return VerifyCheck(
            id="", label="",
            status=STATUS_PASS,
            detail="llm-relay entry point at {}".format(path),
            data={"path": path},
        )
    return VerifyCheck(
        id="", label="",
        status=STATUS_FAIL,
        detail="llm-relay binary not found on PATH",
        remediation=(
            "Reinstall the package (`pip install --force-reinstall llm-relay`) "
            "or ensure your Python scripts directory is on PATH."
        ),
    )


def _check_entry_point_mcp() -> VerifyCheck:
    """MCP entry point is an optional extra. Missing == warn, not fail."""
    path = shutil.which("llm-relay-mcp")
    if path:
        return VerifyCheck(
            id="", label="",
            status=STATUS_PASS,
            detail="llm-relay-mcp entry point at {}".format(path),
            data={"path": path},
        )
    return VerifyCheck(
        id="", label="",
        status=STATUS_WARN,
        detail="llm-relay-mcp binary not found (optional MCP extra)",
        remediation="pip install llm-relay[mcp] (only needed if exposing the MCP server)",
    )


def _check_proxy_extras() -> VerifyCheck:
    """Proxy extras are optional; missing means the [proxy] feature won't work."""
    missing = []
    for module_name in ("httpx", "uvicorn", "starlette"):
        try:
            importlib.import_module(module_name)
        except ImportError:
            missing.append(module_name)
    if not missing:
        return VerifyCheck(
            id="", label="",
            status=STATUS_PASS,
            detail="proxy extras importable (httpx, uvicorn, starlette)",
        )
    return VerifyCheck(
        id="", label="",
        status=STATUS_WARN,
        detail="proxy extras missing: {}".format(", ".join(missing)),
        remediation="pip install llm-relay[proxy] (needed for `llm-relay serve`)",
        data={"missing": missing},
    )


def _check_version_consistency() -> VerifyCheck:
    """The installed wheel's metadata version should match what the running
    process is using. A mismatch usually means a stale editable install.
    """
    try:
        meta_ver = version("llm-relay")
    except PackageNotFoundError:
        return VerifyCheck(
            id="", label="",
            status=STATUS_FAIL,
            detail="package metadata not found (importlib.metadata.PackageNotFoundError)",
            remediation="pip install llm-relay (the package is imported but not installed?)",
        )
    try:
        mod = importlib.import_module("llm_relay")
    except ImportError:
        # Package metadata exists but import failed -- the importable check
        # above already covered that; skip here.
        return VerifyCheck(
            id="", label="",
            status=STATUS_SKIPPED,
            detail="cannot compare versions: import failed (see package_importable)",
        )
    runtime_ver = getattr(mod, "__version__", None)
    if runtime_ver is None:
        return VerifyCheck(
            id="", label="",
            status=STATUS_WARN,
            detail="package metadata version is {} but llm_relay.__version__ is unset".format(meta_ver),
            data={"metadata_version": meta_ver, "runtime_version": None},
        )
    if runtime_ver != meta_ver:
        return VerifyCheck(
            id="", label="",
            status=STATUS_WARN,
            detail="metadata version {!r} != runtime __version__ {!r} (stale install?)".format(
                meta_ver, runtime_ver,
            ),
            remediation="pip install --force-reinstall llm-relay",
            data={"metadata_version": meta_ver, "runtime_version": runtime_ver},
        )
    return VerifyCheck(
        id="", label="",
        status=STATUS_PASS,
        detail="version {} (consistent across metadata and runtime)".format(meta_ver),
        data={"metadata_version": meta_ver, "runtime_version": runtime_ver},
    )


_CHECKS = [
    ("python_version", "Python interpreter version", _check_python_version, None),
    ("package_importable", "llm-relay package can be imported", _check_package_importable, None),
    ("entry_point_relay", "llm-relay CLI entry point exists", _check_entry_point_relay, None),
    ("entry_point_mcp", "llm-relay-mcp entry point exists (optional)", _check_entry_point_mcp, None),
    ("proxy_extras", "proxy extras (httpx/uvicorn/starlette) importable", _check_proxy_extras, None),
    ("version_consistency", "package metadata and runtime version agree", _check_version_consistency, None),
]


def verify_install() -> VerifyReport:
    """Run all install-time checks and return a structured report."""
    report = VerifyReport(target="install")
    for check_id, label, fn, fallback_rem in _CHECKS:
        report.checks.append(
            run_check(check_id, label, fn, fallback_remediation=fallback_rem)
        )
    return report
