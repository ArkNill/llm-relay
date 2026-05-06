"""Windows background daemon for llm-relay serve.

Runs llm-relay as a windowless background process using pythonw.exe +
CREATE_NO_WINDOW. No Windows Service registry, no pywin32 dependency.

PID file at ~/.llm-relay/llm-relay.pid for lifecycle management.

Usage via CLI:
    llm-relay service install [--port 8083]   # start + register auto-start
    llm-relay service start                   # start daemon
    llm-relay service stop                    # stop daemon
    llm-relay service uninstall               # stop + remove auto-start
    llm-relay service status                  # show running status
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_SERVICE_DIR = Path.home() / ".llm-relay"
_PID_FILE = _SERVICE_DIR / "llm-relay.pid"
_LOG_FILE = _SERVICE_DIR / "service.log"
_STARTUP_VBS = _SERVICE_DIR / "llm-relay-startup.vbs"
_STARTUP_LINK_NAME = "llm-relay.lnk"
_DEFAULT_PORT = 8083


def _get_startup_folder() -> Path:
    """Return the Windows Startup folder for current user."""
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _get_pythonw() -> str:
    """Return pythonw.exe path (no-console Python)."""
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    if Path(pythonw).exists():
        return pythonw
    return sys.executable


def _read_pid() -> int:
    """Read PID from pidfile. Returns 0 if not found or stale."""
    if not _PID_FILE.exists():
        return 0
    try:
        pid = int(_PID_FILE.read_text().strip())
        # Check if process is alive (Windows-compatible)
        try:
            result = subprocess.run(
                ["tasklist", "/FI", "PID eq {}".format(pid)],
                capture_output=True, text=True,
                stdin=subprocess.DEVNULL,
            )
            if str(pid) in result.stdout:
                return pid
            _PID_FILE.unlink(missing_ok=True)
            return 0
        except Exception:
            _PID_FILE.unlink(missing_ok=True)
            return 0
    except (ValueError, OSError):
        return 0


def _is_running() -> bool:
    """Check if daemon is running."""
    return _read_pid() > 0


def start_daemon(port: int = _DEFAULT_PORT) -> bool:
    """Start llm-relay as a background process with no console window."""
    if _is_running():
        pid = _read_pid()
        print(f"llm-relay is already running (PID {pid}).")
        return True

    _SERVICE_DIR.mkdir(parents=True, exist_ok=True)

    log_fh = open(str(_LOG_FILE), "a", encoding="utf-8")
    err_fh = open(str(_SERVICE_DIR / "service-error.log"), "a", encoding="utf-8")

    # Hide the console window via STARTUPINFO
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE

    # CREATE_BREAKAWAY_FROM_JOB (0x01000000) — detach child from parent's
    # job object so it survives after the parent (llm-relay.exe CLI) exits.
    # CREATE_NEW_PROCESS_GROUP (0x00000200) — own process group.
    creation_flags = 0x01000000 | 0x00000200

    proc = subprocess.Popen(
        [sys.executable, "-m", "llm_relay.detect", "serve",
         "--port", str(port), "--host", "0.0.0.0"],
        stdout=log_fh,
        stderr=err_fh,
        stdin=subprocess.DEVNULL,
        startupinfo=si,
        creationflags=creation_flags,
    )

    # Wait briefly to check if process survives startup
    import time
    time.sleep(2)
    if proc.poll() is not None:
        err_fh.close()
        log_fh.close()
        err_content = (_SERVICE_DIR / "service-error.log").read_text(encoding="utf-8", errors="replace").strip()
        if err_content:
            print(f"Error: server exited immediately.\n{err_content}")
        else:
            print("Error: server exited immediately (no error output).")
        _PID_FILE.unlink(missing_ok=True)
        return False

    _PID_FILE.write_text(str(proc.pid))

    print(f"llm-relay started (PID {proc.pid}, port {port}).")
    print(f"  Dashboard: http://localhost:{port}/dashboard/")
    print(f"  Display:   http://localhost:{port}/display/")
    print(f"  Log:       {_LOG_FILE}")
    return True


def stop_daemon() -> bool:
    """Stop the llm-relay background process."""
    pid = _read_pid()
    if not pid:
        print("llm-relay is not running.")
        return True

    try:
        # On Windows, use taskkill for clean shutdown
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True, text=True,
            stdin=subprocess.DEVNULL,
        )
        _PID_FILE.unlink(missing_ok=True)
        print(f"llm-relay stopped (PID {pid}).")
        return True
    except Exception as e:
        print(f"Error stopping llm-relay: {e}")
        return False


def install_service(port: int = _DEFAULT_PORT) -> bool:
    """Start daemon + register for auto-start on login."""
    _SERVICE_DIR.mkdir(parents=True, exist_ok=True)

    # Create VBS launcher for startup (hidden window)
    pythonw = _get_pythonw()
    vbs_content = (
        'Set ws = CreateObject("Wscript.Shell")\n'
        'ws.Run """{}""" & " -m llm_relay.detect serve --port {} --host 0.0.0.0", 0, False\n'
    ).format(pythonw, port)
    _STARTUP_VBS.write_text(vbs_content, encoding="utf-8")

    # Create shortcut in Startup folder
    startup_dir = _get_startup_folder()
    startup_dir.mkdir(parents=True, exist_ok=True)
    shortcut_path = startup_dir / _STARTUP_LINK_NAME

    try:
        # Use PowerShell to create .lnk shortcut
        ps_cmd = (
            '$ws = New-Object -ComObject WScript.Shell; '
            '$sc = $ws.CreateShortcut("{}"); '
            '$sc.TargetPath = "wscript.exe"; '
            '$sc.Arguments = "{}"; '
            '$sc.WindowStyle = 7; '
            '$sc.Save()'
        ).format(str(shortcut_path), str(_STARTUP_VBS))

        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True,
            stdin=subprocess.DEVNULL,
        )
    except Exception:
        # Fallback: just copy VBS to startup folder directly
        import shutil
        shutil.copy2(str(_STARTUP_VBS), str(startup_dir / "llm-relay-startup.vbs"))

    # Start now
    started = start_daemon(port=port)
    if started:
        print("  Auto-start: registered (runs on login)")
    return started


def uninstall_service() -> bool:
    """Stop daemon + remove auto-start registration."""
    stop_daemon()

    # Remove startup shortcut
    startup_dir = _get_startup_folder()
    for name in [_STARTUP_LINK_NAME, "llm-relay-startup.vbs"]:
        target = startup_dir / name
        if target.exists():
            target.unlink()

    # Remove VBS launcher
    if _STARTUP_VBS.exists():
        _STARTUP_VBS.unlink()

    # Remove PID file
    _PID_FILE.unlink(missing_ok=True)

    print("llm-relay auto-start removed.")
    return True


def service_status() -> bool:
    """Show daemon status."""
    pid = _read_pid()
    if pid:
        print(f"llm-relay: RUNNING (PID {pid})")
        print(f"  Log: {_LOG_FILE}")

        # Check auto-start
        startup_dir = _get_startup_folder()
        auto = (startup_dir / _STARTUP_LINK_NAME).exists() or (startup_dir / "llm-relay-startup.vbs").exists()
        print(f"  Auto-start: {'yes' if auto else 'no'}")
        return True
    else:
        print("llm-relay: STOPPED")
        return False
