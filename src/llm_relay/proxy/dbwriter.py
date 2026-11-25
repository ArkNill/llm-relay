"""Background DB writer -- keeps synchronous DB writes off the proxy event loop.

Why (2026-09-28): every request used to run 2-3 synchronous psycopg INSERTs
(requests / cache_diagnostics / conversation_turns) inside the uvicorn event
loop, stalling every other in-flight stream for the duration. An earlier
attempt with ``asyncio.to_thread`` dropped records because the streaming
generator is torn down at the ``yield`` site and any ``await`` in its
``finally`` can be cut short.

Guarantees -- every CC conversation turn MUST land in the DB:
  * ``submit()`` is synchronous and never awaits, blocks or raises, so it
    survives generator teardown;
  * one consumer thread preserves per-request ordering (``log_request`` before
    ``capture_turn_streamed``) and owns its own DB connection;
  * transient failures are retried on a fresh connection; persistent failures
    are spilled to ``~/.llm-relay/spill-YYYYMMDD.jsonl`` and never silently
    dropped;
  * the queue is bounded by count and by bytes (``LLM_RELAY_DB_QUEUE_MAX``,
    ``LLM_RELAY_DB_QUEUE_MAX_BYTES``); a job that does not fit is spilled at
    once rather than kept in RAM while the database is slow;
  * ``stop()`` (lifespan shutdown) drains the queue; anything left after the
    timeout is spilled, including the job the writer thread is still working on
    (2026-10-23), and a job failing during shutdown is spilled at once instead
    of backing off into the timeout.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("llm-relay.dbwriter")

# (fn, kwargs, approximate bytes the job keeps alive while it waits in the queue)
Job = Tuple[Callable[..., Any], Dict[str, Any], int]

_QUEUE_MAX = int(os.getenv("LLM_RELAY_DB_QUEUE_MAX", "20000"))
# Byte cap on the queue (2026-09-29): the count cap alone lets a stalled database
# hold thousands of conversation captures (p50 0.7 MB, p95 2.1 MB each) in RAM,
# well past the unit's MemoryMax. A job that would push the queued total over
# the cap is spilled straight away instead. 0 disables the cap.
_QUEUE_MAX_BYTES = int(os.getenv("LLM_RELAY_DB_QUEUE_MAX_BYTES", str(256 * 1024 * 1024)))
# Counted for every job on top of its size hint (kwargs, tuple, a raw_usage dict).
_JOB_OVERHEAD_BYTES = 2048
_RETRIES = int(os.getenv("LLM_RELAY_DB_WRITE_RETRIES", "3"))
_SPILL_DIR = Path(os.getenv("LLM_RELAY_SPILL_DIR", str(Path.home() / ".llm-relay")))


class DBWriter:
    """Single-consumer write queue with its own DB connection."""

    def __init__(
        self,
        conn_factory: Optional[Callable[[], Any]] = None,
        spill_dir: Optional[Path] = None,
    ) -> None:
        self._q: queue.Queue[Optional[Job]] = queue.Queue(maxsize=_QUEUE_MAX)
        self._conn_factory = conn_factory
        self._spill_dir = Path(spill_dir) if spill_dir else _SPILL_DIR
        self._conn: Any = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._stopped = False
        # The job the writer thread is working on, and the one stop() spilled on
        # its behalf (R9-10b). Before 2026-10-23 a job taken off the queue but not
        # finished by the drain timeout was in neither the database nor the
        # spill file: it died with the daemon thread. Both guarded by _lock.
        self._current: Optional[Job] = None
        self._handed_off: Optional[Job] = None
        # Bytes held by queued and in-progress jobs; producer (event loop) and
        # consumer (writer thread) both touch it, so it has its own lock.
        self._queued_bytes = 0
        self._bytes_lock = threading.Lock()
        self.stats: Dict[str, int] = {"enqueued": 0, "written": 0, "retried": 0, "spilled": 0, "direct": 0}

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._stopped or (self._thread is not None and self._thread.is_alive()):
                return
            self._thread = threading.Thread(target=self._run, name="llm-relay-dbwriter", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 8.0) -> None:
        """Drain the queue and stop the thread. Leftover jobs are spilled, never dropped."""
        with self._lock:
            self._stopped = True
            thread = self._thread
        if thread is None or not thread.is_alive():
            self._drain_to_spill("writer not running at shutdown")
            return
        try:
            self._q.put(None, timeout=1.0)
        except queue.Full:
            pass
        thread.join(timeout)
        if thread.is_alive():
            self._drain_to_spill("shutdown timeout")
            # The thread is still inside a job (a hung connect or INSERT). It may yet
            # finish, so the record says so; the thread will not spill it a second time.
            with self._lock:
                current, self._current = self._current, None
                self._handed_off = current
            if current is not None:
                self._spill(current, reason="shutdown timeout (in progress; may also have been written)")

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def snapshot(self) -> Dict[str, Any]:
        with self._bytes_lock:
            queued_bytes = self._queued_bytes
        return {
            "alive": self.alive(),
            "queued": self._q.qsize(),
            "queued_bytes": queued_bytes,
            "queue_max_bytes": _QUEUE_MAX_BYTES,
            **self.stats,
        }

    # -- producer side -----------------------------------------------------

    def submit(self, fn: Callable[..., Any], *, size_hint: int = 0, **kwargs: Any) -> None:
        """Enqueue ``fn(conn, **kwargs)``. Synchronous; never blocks, never raises.

        ``size_hint`` is the caller's estimate of the bytes the job keeps alive
        while it waits (a conversation capture holds its stripped request and
        the response); it counts against ``LLM_RELAY_DB_QUEUE_MAX_BYTES`` and
        is not passed on to ``fn``.
        """
        self.stats["enqueued"] += 1
        job: Job = (fn, kwargs, _JOB_OVERHEAD_BYTES + max(0, int(size_hint or 0)))
        if not self._stopped and not self.alive():
            self.start()
        if self._stopped or not self.alive():
            # No consumer available (shutdown, or the thread failed to start):
            # one attempt on a private connection, spill on failure. The caller
            # pays at most one local DB round-trip.
            self.stats["direct"] += 1
            self._write_or_spill(job, attempts=1)
            return
        with self._bytes_lock:
            over = _QUEUE_MAX_BYTES > 0 and self._queued_bytes + job[2] > _QUEUE_MAX_BYTES
            if not over:
                self._queued_bytes += job[2]
        if over:
            self._spill(job, reason="queue bytes")
            return
        try:
            self._q.put_nowait(job)
        except queue.Full:
            self._release(job)
            self._spill(job, reason="queue full")

    def _release(self, job: Job) -> None:
        """The job left the queue (written, spilled or drained): stop counting its bytes."""
        with self._bytes_lock:
            self._queued_bytes = max(0, self._queued_bytes - job[2])

    # -- consumer side -----------------------------------------------------

    def _run(self) -> None:
        while True:
            job = self._q.get()
            try:
                if job is None:
                    break
                with self._lock:
                    self._current = job
                self._write_or_spill(job, attempts=_RETRIES)
            except Exception:  # pragma: no cover - _write_or_spill handles its own errors
                logger.exception("dbwriter: unexpected error")
            finally:
                if job is not None:
                    with self._lock:
                        if self._current is job:
                            self._current = None
                    self._release(job)
                self._q.task_done()
        self._reset_conn()

    def _get_conn(self) -> Any:
        if self._conn is None:
            factory = self._conn_factory
            if factory is None:
                from .db import get_conn

                factory = get_conn
            self._conn = factory()
        return self._conn

    def _reset_conn(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _write_or_spill(self, job: Job, attempts: int) -> None:
        fn, kwargs, _size = job
        name = getattr(fn, "__name__", repr(fn))
        delay = 0.5
        attempts = max(1, attempts)
        for attempt in range(1, attempts + 1):
            try:
                fn(self._get_conn(), **kwargs)
                self.stats["written"] += 1
                return
            except Exception as exc:
                self.stats["retried"] += 1
                logger.warning("dbwriter: %s failed (attempt %d/%d): %r", name, attempt, attempts, exc)
                self._reset_conn()
                if attempt < attempts:
                    if self._stopped:
                        # Shutting down: the backoff would only run into stop()'s
                        # drain timeout, so spill now rather than risk the job.
                        self._spill_unless_handed_off(job, reason="shutdown")
                        return
                    time.sleep(delay)
                    delay = min(delay * 2, 4.0)
        self._spill_unless_handed_off(job, reason="retries exhausted")

    def _spill_unless_handed_off(self, job: Job, reason: str) -> None:
        """Spill ``job`` unless stop() already spilled it as the in-progress job."""
        with self._lock:
            handed_off = job is self._handed_off
        if not handed_off:
            self._spill(job, reason=reason)

    def _drain_to_spill(self, reason: str) -> None:
        while True:
            try:
                job = self._q.get_nowait()
            except queue.Empty:
                return
            if job is not None:
                self._spill(job, reason=reason)
                self._release(job)
            self._q.task_done()

    def _spill(self, job: Job, reason: str) -> None:
        fn, kwargs, _size = job
        name = getattr(fn, "__name__", repr(fn))
        self.stats["spilled"] += 1
        try:
            self._spill_dir.mkdir(parents=True, exist_ok=True)
            path = self._spill_dir / "spill-{}.jsonl".format(datetime.now(timezone.utc).strftime("%Y%m%d"))
            record = {
                "ts": time.time(),
                "fn": name,
                "module": getattr(fn, "__module__", None),
                "reason": reason,
                "kwargs": kwargs,
            }
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            logger.error("dbwriter: %s spilled to %s (%s)", name, path, reason)
        except Exception:
            logger.exception("dbwriter: %s LOST -- spill failed (%s)", name, reason)


_default_writer: Optional[DBWriter] = None


def get_writer() -> DBWriter:
    global _default_writer
    if _default_writer is None:
        _default_writer = DBWriter()
    return _default_writer
