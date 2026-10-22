"""Byte budget for the request bodies the proxy holds in memory.

Why (2026-10-22): a request body is read into memory, parsed once, possibly
rewritten and uploaded; a 25 MB image request costs about 50 MB of heap while
that happens (five concurrent 24 MB requests peaked at 248 MB in the 2026-09-29
isolated measurement). Nothing bounded how many requests could be in that phase
at once, so a burst of image sub-agents (about 40 requests of 25 MB on 9/29)
pushed the relay past its cgroup limit and into an OOM restart loop.

The gate admits a body only while the bytes already admitted plus the new body
fit the budget; anything else waits, in arrival order, *before* its body is
read, so a waiting request holds no more than the server's read buffer. Waiting
is the default answer, not rejection: only a request that is still waiting
after ``timeout_s`` gets an error (the proxy answers 503, which clients retry).

  * A body counts from admission until upstream response headers arrive (httpx
    has uploaded all of it by then) or the request fails -- not until a
    streamed response ends.
  * Bodies below ``min_bytes`` (ordinary traffic: p50 785 KB) are never counted
    or delayed.
  * A body larger than the whole budget is admitted when nothing else is in
    flight, so no request can wait on its own size forever.

Event-loop thread only: plain counters and asyncio futures, no locks.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from typing import Deque, Optional, Tuple

logger = logging.getLogger("llm-relay.bodygate")

FALLBACK_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_MIN_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_S = 120.0


def cgroup_memory_max(proc_cgroup: str = "/proc/self/cgroup", cgroup_root: str = "/sys/fs/cgroup") -> Optional[int]:
    """Byte limit of this process's cgroup v2 (``memory.max``); None when it is "max" or unreadable."""
    try:
        with open(proc_cgroup, encoding="ascii") as fh:
            for line in fh:
                if line.startswith("0::"):
                    with open(cgroup_root + line[3:].strip() + "/memory.max", encoding="ascii") as limit:
                        value = limit.read().strip()
                    return int(value) if value.isdigit() else None
    except (OSError, ValueError):
        pass
    return None


def default_max_bytes(env_value: Optional[str], memory_max: Optional[int]) -> int:
    """The budget: ``env_value`` when set, else a quarter of ``memory_max``, else 256 MiB.

    A quarter leaves room for the second copy each admitted request makes while
    it is parsed and rewritten, and for the rest of the process (history
    captures, composition caches, the DB writer queue).
    """
    if env_value is not None and env_value.strip():
        return int(env_value)
    if memory_max:
        return memory_max // 4
    return FALLBACK_MAX_BYTES


class BodyGateTimeout(Exception):
    """A request waited ``timeout_s`` at the gate without being admitted."""

    def __init__(self, n: int, waited_s: float, max_bytes: int) -> None:
        super().__init__("waited {:.0f}s for {} B, max {} B".format(waited_s, n, max_bytes))
        self.n = n
        self.waited_s = waited_s
        self.max_bytes = max_bytes


class BodyGateToken:
    """One admission. ``release()`` gives the bytes back; calling it again does nothing."""

    __slots__ = ("_gate", "n", "waited_ms")

    def __init__(self, gate: Optional[BodyGate], n: int, waited_ms: int = 0) -> None:
        self._gate = gate  # None: never counted (gate off or a small body), or already released
        self.n = n
        self.waited_ms = waited_ms  # 0 = admitted without waiting

    def release(self) -> None:
        gate, self._gate = self._gate, None
        if gate is not None:
            gate._release(self.n)


class BodyGate:
    """FIFO byte budget; see the module docstring."""

    def __init__(
        self,
        max_bytes: int,
        min_bytes: int = DEFAULT_MIN_BYTES,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.max_bytes = max_bytes  # 0 = gate off
        self.min_bytes = min_bytes
        self.timeout_s = timeout_s
        self._inflight_bytes = 0
        self._inflight_requests = 0
        # (bytes, future) per waiting request, oldest first. The future is
        # resolved by _wake() once the bytes have been counted as in flight.
        self._waiters: Deque[Tuple[int, asyncio.Future]] = collections.deque()
        self._waited_total = 0
        self._timeouts = 0

    async def acquire(self, n: int, *, label: str = "") -> BodyGateToken:
        """Wait until ``n`` bytes fit the budget; raise BodyGateTimeout after ``timeout_s``.

        ``label`` only goes into the log lines.
        """
        # n <= 0 (no content-length) also passes when min_bytes is 0: there is
        # nothing to count, and such a request must not queue behind a large one.
        if self.max_bytes <= 0 or n <= 0 or n < self.min_bytes:
            return BodyGateToken(None, n)
        # A newcomer queues behind earlier waiters even when it would fit, so a
        # steady flow of mid-sized bodies cannot starve a large one at the head.
        if not self._waiters and self._fits(n):
            self._admit(n)
            return BodyGateToken(self, n)

        entry = (n, asyncio.get_running_loop().create_future())
        self._waiters.append(entry)
        self._waited_total += 1
        logger.info(
            "body gate: waiting for %d B (inflight %d B / max %d B, %d waiting) -- %s",
            n, self._inflight_bytes, self.max_bytes, len(self._waiters), label,
        )
        start = time.monotonic()
        try:
            # asyncio.wait, not wait_for: it never cancels the future, so after
            # it returns the future alone says whether we were admitted.
            await asyncio.wait({entry[1]}, timeout=self.timeout_s)
        except BaseException:
            self._abandon(entry)  # cancelled (client gone, shutdown) while queued
            raise
        waited_s = time.monotonic() - start
        if not entry[1].done():
            self._abandon(entry)
            self._timeouts += 1
            raise BodyGateTimeout(n, waited_s, self.max_bytes)
        logger.info(
            "body gate: admitted after %.0f ms (%d B, inflight %d B / max %d B) -- %s",
            waited_s * 1000, n, self._inflight_bytes, self.max_bytes, label,
        )
        # At least 1, so "waited" never reads as 0 (= did not wait).
        return BodyGateToken(self, n, max(1, int(round(waited_s * 1000))))

    def snapshot(self) -> dict:
        return {
            "enabled": self.max_bytes > 0,
            "max_bytes": self.max_bytes,
            "min_bytes": self.min_bytes,
            "timeout_s": self.timeout_s,
            "inflight_bytes": self._inflight_bytes,
            "inflight_requests": self._inflight_requests,
            "waiting": len(self._waiters),
            "waited_total": self._waited_total,
            "timeouts": self._timeouts,
        }

    def _fits(self, n: int) -> bool:
        # Alone, any size fits: waiting for room that can never exist would
        # only end in a timeout.
        return self._inflight_bytes == 0 or self._inflight_bytes + n <= self.max_bytes

    def _admit(self, n: int) -> None:
        self._inflight_bytes += n
        self._inflight_requests += 1

    def _release(self, n: int) -> None:
        self._inflight_bytes -= n
        self._inflight_requests -= 1
        self._wake()

    def _wake(self) -> None:
        """Admit waiters from the head while they fit; stop at the first that does not."""
        while self._waiters:
            n, fut = self._waiters[0]
            if not self._fits(n):
                return
            self._waiters.popleft()
            self._admit(n)
            fut.set_result(None)

    def _abandon(self, entry: Tuple[int, asyncio.Future]) -> None:
        """Take a request that stopped waiting out of the gate."""
        n, fut = entry
        if fut.done():
            # Cancelled after _wake() admitted it but before the task resumed:
            # its bytes were already counted.
            self._release(n)
            return
        self._waiters.remove(entry)
        # It may have been the head that kept smaller bodies behind it waiting.
        self._wake()
