"""Cross-platform process inspection helpers.

Linux: reads /proc directly (zero dependencies).
Windows/macOS: uses psutil if available, else graceful degradation.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_IS_LINUX = sys.platform.startswith("linux")

# Lazy psutil import for non-Linux platforms
_psutil = None
_psutil_checked = False


def _get_psutil():
    """Soft-import psutil. Returns module or None."""
    global _psutil, _psutil_checked
    if not _psutil_checked:
        try:
            import psutil
            _psutil = psutil
        except ImportError:
            logger.debug("psutil not available -- process liveness features degraded on this platform")
            _psutil = None
        _psutil_checked = True
    return _psutil


# ── /proc helpers (Linux only) ──

_CLI_PROCESS_NAMES = {"claude", "codex", "gemini", "node", "bun", "deno"}


def _get_proc_dir() -> Path:
    """Return the /proc directory path (host /proc if mounted, else local)."""
    env_path = os.getenv("LLM_RELAY_HOST_PROC")
    if env_path:
        return Path(env_path)
    return Path("/proc")


def _is_cli_process_name(comm: str, cmdline: str) -> bool:
    """Check if a process name or cmdline matches any known CLI tool."""
    comm_lower = comm.lower()
    cmdline_lower = cmdline.lower()
    for name in _CLI_PROCESS_NAMES:
        if comm_lower == name or name in cmdline_lower:
            return True
    return False


# ── Public API ──

def is_cli_process_alive(pid: Optional[int]) -> bool:
    """Check if a process is alive and is a known CLI instance.

    Linux: reads /proc/PID/cmdline.
    Windows: uses psutil.Process(pid).cmdline().
    """
    if not pid or pid <= 0:
        return False

    if _IS_LINUX:
        proc_dir = _get_proc_dir() / str(pid)
        if not proc_dir.exists():
            return False
        try:
            cmdline_path = proc_dir / "cmdline"
            if not cmdline_path.exists():
                return False
            cmdline = cmdline_path.read_bytes().decode("utf-8", errors="replace")
            comm = ""
            comm_path = proc_dir / "comm"
            if comm_path.exists():
                comm = comm_path.read_text(errors="replace").strip()
            return _is_cli_process_name(comm, cmdline)
        except (OSError, PermissionError):
            return False

    # Non-Linux: psutil fallback
    psutil = _get_psutil()
    if psutil is None:
        return False
    try:
        proc = psutil.Process(pid)
        if not proc.is_running():
            return False
        cmdline = " ".join(proc.cmdline())
        comm = proc.name()
        return _is_cli_process_name(comm, cmdline)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        return False


def find_cli_pid_by_tty(tty: Optional[str]) -> Optional[int]:
    """Find a running CLI process on a given TTY.

    Linux: scans /proc/*/stat for TTY major/minor matching.
    Windows: TTY concept does not apply -- always returns None.
    """
    if not tty:
        return None

    if not _IS_LINUX:
        # TTY-based lookup is Linux-specific
        return None

    tty_short = tty.replace("/dev/", "")
    if not tty_short:
        return None

    proc_dir = _get_proc_dir()
    try:
        for entry in proc_dir.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline_path = entry / "cmdline"
                if not cmdline_path.exists():
                    continue
                cmdline = cmdline_path.read_bytes().decode("utf-8", errors="replace")
                comm = ""
                comm_path = entry / "comm"
                if comm_path.exists():
                    comm = comm_path.read_text(errors="replace").strip()
                if not _is_cli_process_name(comm, cmdline):
                    continue
                stat_path = entry / "stat"
                if not stat_path.exists():
                    continue
                stat_content = stat_path.read_text(errors="replace")
                rparen = stat_content.rfind(")")
                if rparen == -1:
                    continue
                fields = stat_content[rparen + 1:].split()
                if len(fields) < 5:
                    continue
                tty_nr = int(fields[4])
                major = (tty_nr >> 8) & 0xff
                minor = (tty_nr & 0xff) | ((tty_nr >> 12) & 0xfff00)
                if major == 136:
                    candidate = "pts/{}".format(minor)
                elif major == 4:
                    candidate = "tty{}".format(minor)
                else:
                    continue
                if candidate == tty_short:
                    return int(entry.name)
            except (OSError, ValueError, PermissionError):
                continue
    except OSError:
        pass
    return None


def read_proc_environ(pid: int) -> dict:
    """Read a process's environment variables.

    Linux: reads /proc/PID/environ.
    Windows: uses psutil.Process(pid).environ().
    """
    if _IS_LINUX:
        proc_dir = _get_proc_dir() / str(pid)
        environ_path = proc_dir / "environ"
        try:
            raw = environ_path.read_bytes()
            result = {}
            for entry_bytes in raw.split(b"\x00"):
                if b"=" in entry_bytes:
                    key, _, val = entry_bytes.partition(b"=")
                    result[key.decode("utf-8", errors="replace")] = val.decode("utf-8", errors="replace")
            return result
        except (OSError, PermissionError):
            return {}

    psutil = _get_psutil()
    if psutil is None:
        return {}
    try:
        return psutil.Process(pid).environ()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        return {}


def get_parent_comm_chain(pid: int, max_depth: int = 10) -> List[Tuple[int, str]]:
    """Walk parent process chain and return list of (pid, comm) tuples.

    Linux: reads /proc/PID/status recursively.
    Windows: uses psutil.Process(pid).ppid() + .name().
    """
    if _IS_LINUX:
        proc_dir = _get_proc_dir()
        chain = []  # type: List[Tuple[int, str]]
        current = pid
        for _ in range(max_depth):
            try:
                status_path = proc_dir / str(current) / "status"
                if not status_path.exists():
                    break
                ppid = None
                comm = ""
                for line in status_path.read_text(errors="replace").splitlines():
                    if line.startswith("Name:\t"):
                        comm = line[6:].strip()
                    elif line.startswith("PPid:\t"):
                        ppid = int(line[6:].strip())
                if ppid is None or ppid <= 1:
                    if comm:
                        chain.append((current, comm))
                    break
                chain.append((current, comm))
                current = ppid
            except (OSError, ValueError, PermissionError):
                break
        return chain

    psutil = _get_psutil()
    if psutil is None:
        return []
    chain = []  # type: List[Tuple[int, str]]
    current = pid
    try:
        for _ in range(max_depth):
            proc = psutil.Process(current)
            comm = proc.name()
            ppid = proc.ppid()
            if ppid is None or ppid <= 1:
                if comm:
                    chain.append((current, comm))
                break
            chain.append((current, comm))
            current = ppid
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        pass
    return chain


def collect_open_session_paths(proc_dir: Optional[Path] = None) -> Set[str]:
    """Return resolved paths of session JSONL/JSON files held open by any process.

    Linux: reads /proc/*/fd symlinks.
    Windows: uses psutil to enumerate open files across all processes.
    """
    if _IS_LINUX:
        if proc_dir is None:
            proc_dir = _get_proc_dir()
        open_paths = set()  # type: Set[str]
        try:
            entries = list(proc_dir.iterdir())
        except OSError:
            return open_paths
        for entry in entries:
            if not entry.name.isdigit():
                continue
            fd_dir = entry / "fd"
            try:
                fds = list(fd_dir.iterdir())
            except (OSError, PermissionError):
                continue
            for fd in fds:
                try:
                    target = os.readlink(str(fd))
                except OSError:
                    continue
                if not (target.endswith(".jsonl") or target.endswith(".json")):
                    continue
                try:
                    resolved = str(Path(target).resolve())
                except OSError:
                    resolved = target
                open_paths.add(resolved)
        return open_paths

    # Non-Linux: psutil fallback
    psutil = _get_psutil()
    if psutil is None:
        return set()
    open_paths = set()  # type: Set[str]
    try:
        for proc in psutil.process_iter(["pid"]):
            try:
                for f in proc.open_files():
                    if f.path.endswith(".jsonl") or f.path.endswith(".json"):
                        try:
                            resolved = str(Path(f.path).resolve())
                        except OSError:
                            resolved = f.path
                        open_paths.add(resolved)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                continue
    except (psutil.Error, OSError):
        pass
    return open_paths


def collect_open_session_path_pids(proc_dir: Optional[Path] = None) -> Dict[str, int]:
    """Return {resolved_path: pid} for session files held open by any process.

    Like collect_open_session_paths() but also returns the PID holding each file.
    If multiple PIDs hold the same file, the last one wins (typically the CLI process).
    """
    if _IS_LINUX:
        if proc_dir is None:
            proc_dir = _get_proc_dir()
        path_pids = {}  # type: Dict[str, int]
        try:
            entries = list(proc_dir.iterdir())
        except OSError:
            return path_pids
        for entry in entries:
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            fd_dir = entry / "fd"
            try:
                fds = list(fd_dir.iterdir())
            except (OSError, PermissionError):
                continue
            for fd in fds:
                try:
                    target = os.readlink(str(fd))
                except OSError:
                    continue
                if not (target.endswith(".jsonl") or target.endswith(".json")):
                    continue
                try:
                    resolved = str(Path(target).resolve())
                except OSError:
                    resolved = target
                path_pids[resolved] = pid
        return path_pids

    # Non-Linux: psutil fallback
    psutil = _get_psutil()
    if psutil is None:
        return {}
    path_pids = {}  # type: Dict[str, int]
    try:
        for proc in psutil.process_iter(["pid"]):
            try:
                pid = proc.info["pid"]
                for f in proc.open_files():
                    if f.path.endswith(".jsonl") or f.path.endswith(".json"):
                        try:
                            resolved = str(Path(f.path).resolve())
                        except OSError:
                            resolved = f.path
                        path_pids[resolved] = pid
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                continue
    except (psutil.Error, OSError):
        pass
    return path_pids


def get_process_tty(pid: int) -> Optional[str]:
    """Return the TTY device (e.g., 'pts/2') for a process, or None.

    Linux: reads /proc/<pid>/fd/0 symlink.
    Non-Linux: uses psutil if available.
    """
    if _IS_LINUX:
        proc_dir = _get_proc_dir()
        fd0 = proc_dir / str(pid) / "fd" / "0"
        try:
            target = os.readlink(str(fd0))
        except OSError:
            return None
        # /dev/pts/2 → pts/2
        if target.startswith("/dev/"):
            return target[5:]
        return None

    psutil = _get_psutil()
    if psutil is None:
        return None
    try:
        proc = psutil.Process(pid)
        terminal = proc.terminal()
        if terminal and terminal.startswith("/dev/"):
            return terminal[5:]
        return terminal
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        return None


def get_process_terminal_name(pid: int) -> Optional[str]:
    """Walk the parent chain to find the terminal emulator or session leader name.

    Returns names like 'tmux', 'sshd', 'tailscaled', 'bash', etc.
    Useful for identifying the session's terminal context.
    """
    if _IS_LINUX:
        proc_dir = _get_proc_dir()
        chain = _get_parent_comm_chain_impl(pid, proc_dir)
    else:
        chain = _get_parent_comm_chain_psutil(pid)

    # Walk the chain (child → parent), skip the CLI itself and shells
    skip_names = {"codex", "node", "gemini", "python", "python3", "bash", "sh", "zsh", "fish"}
    for ppid, comm in chain:
        if comm.lower() in skip_names:
            continue
        # Found a meaningful parent
        return comm

    return None


def _get_parent_comm_chain_impl(pid: int, proc_dir: Path) -> List[Tuple[int, str]]:
    """Linux /proc-based parent chain. Returns [(pid, comm), ...]."""
    chain = []  # type: List[Tuple[int, str]]
    current = pid
    seen = set()  # type: Set[int]
    while current > 1 and current not in seen:
        seen.add(current)
        try:
            stat_data = (proc_dir / str(current) / "stat").read_text()
        except OSError:
            break
        # Extract comm from stat: "pid (comm) state ppid ..."
        start = stat_data.find("(")
        end = stat_data.rfind(")")
        if start < 0 or end < 0:
            break
        comm = stat_data[start + 1:end]
        rest = stat_data[end + 2:].split()
        if len(rest) < 2:
            break
        ppid = int(rest[1])
        chain.append((current, comm))
        current = ppid
    return chain


def _get_parent_comm_chain_psutil(pid: int) -> List[Tuple[int, str]]:
    """psutil-based parent chain for non-Linux."""
    psutil = _get_psutil()
    if psutil is None:
        return []
    chain = []  # type: List[Tuple[int, str]]
    current = pid
    seen = set()  # type: Set[int]
    while current and current > 0 and current not in seen:
        seen.add(current)
        try:
            proc = psutil.Process(current)
            chain.append((current, proc.name()))
            parent = proc.parent()
            current = parent.pid if parent else 0
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            break
    return chain
