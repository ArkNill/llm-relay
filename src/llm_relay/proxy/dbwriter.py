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
  * ``stop()`` (lifespan shutdown) drains the queue; anything left after the
    timeout is spilled.
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

Job = Tuple[Callable[..., Any], Dict[str, Any]]

_QUEUE_MAX = int(os.getenv("LLM_RELAY_DB_QUEUE_MAX", "20000"))
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

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def snapshot(self) -> Dict[str, Any]:
        return {"alive": self.alive(), "queued": self._q.qsize(), **self.stats}

    # -- producer side -----------------------------------------------------

    def submit(self, fn: Callable[..., Any], **kwargs: Any) -> None:
        """Enqueue ``fn(conn, **kwargs)``. Synchronous; never blocks, never raises."""
        self.stats["enqueued"] += 1
        job: Job = (fn, kwargs)
        if not self._stopped and not self.alive():
            self.start()
        if self._stopped or not self.alive():
            # No consumer available (shutdown, or the thread failed to start):
            # one attempt on a private connection, spill on failure. The caller
            # pays at most one local DB round-trip.
            self.stats["direct"] += 1
            self._write_or_spill(job, attempts=1)
            return
        try:
            self._q.put_nowait(job)
        except queue.Full:
            self._spill(job, reason="queue full")

    # -- consumer side -----------------------------------------------------

    def _run(self) -> None:
        while True:
            job = self._q.get()
            try:
                if job is None:
                    break
                self._write_or_spill(job, attempts=_RETRIES)
            except Exception:  # pragma: no cover - _write_or_spill handles its own errors
                logger.exception("dbwriter: unexpected error")
            finally:
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
        fn, kwargs = job
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
                    time.sleep(delay)
                    delay = min(delay * 2, 4.0)
        self._spill(job, reason="retries exhausted")

    def _drain_to_spill(self, reason: str) -> None:
        while True:
            try:
                job = self._q.get_nowait()
            except queue.Empty:
                return
            if job is not None:
                self._spill(job, reason=reason)
            self._q.task_done()

    def _spill(self, job: Job, reason: str) -> None:
        fn, kwargs = job
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
