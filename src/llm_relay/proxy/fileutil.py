"""File safety utilities — atomic writes, advisory locks, change detection.

Absorbs the FileSnapshot pattern from cozempic for safe concurrent writes
when Claude may be appending to the same JSONL file.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class FileSnapshot:
    """Capture inode, size, and content hash at load time.

    Used to detect what happened to a file between load and write:
    - unchanged: nothing happened
    - appended: Claude added lines (safe — merge the delta)
    - conflict: file was replaced, truncated, or inode changed (unsafe)
    """

    path: Path
    inode: int
    size: int
    content_hash: str  # MD5 of full content at snapshot time

    @classmethod
    def take(cls, path: Path) -> "FileSnapshot":
        """Snapshot a file's current state."""
        stat = path.stat()
        data = path.read_bytes()
        h = hashlib.md5(data, usedforsecurity=False).hexdigest()
        return cls(path=path, inode=stat.st_ino, size=stat.st_size, content_hash=h)

    def classify(self) -> Literal["unchanged", "appended", "conflict"]:
        """Check what happened since the snapshot was taken."""
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return "conflict"

        if stat.st_ino != self.inode:
            return "conflict"

        if stat.st_size == self.size:
            # Quick check — same size, verify hash
            data = self.path.read_bytes()
            h = hashlib.md5(data, usedforsecurity=False).hexdigest()
            return "unchanged" if h == self.content_hash else "conflict"

        if stat.st_size > self.size:
            # File grew — check if prefix is intact
            data = self.path.read_bytes()
            prefix_hash = hashlib.md5(
                data[: self.size], usedforsecurity=False
            ).hexdigest()
            return "appended" if prefix_hash == self.content_hash else "conflict"

        # File shrank
        return "conflict"

    def read_delta(self) -> bytes:
        """Return bytes appended since the snapshot.  Only valid after
        classify() returns 'appended'."""
        return self.path.read_bytes()[self.size :]


@contextmanager
def advisory_lock(path: Path):
    """Acquire an advisory (non-blocking) exclusive lock on *path*.

    Creates a `.lock` sibling file.  Raises ``BlockingIOError`` if
    another process already holds the lock.
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        try:
            lock_path.unlink()
        except OSError:
            pass


def atomic_write(path: Path, data: bytes | str) -> None:
    """Write *data* to *path* atomically via write→fsync→rename.

    The file is written to a temporary sibling first, fsynced, then
    ``os.replace``-d over the target (atomic on POSIX).
    """
    if isinstance(data, str):
        data = data.encode("utf-8")

    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        os.replace(tmp, str(path))
    except BaseException:
        os.close(fd) if not os.get_inheritable(fd) else None
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
