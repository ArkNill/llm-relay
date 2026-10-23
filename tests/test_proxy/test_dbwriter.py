"""Tests for the background DB writer (proxy/dbwriter.py)."""

from __future__ import annotations

import json
import threading

from llm_relay.proxy import dbwriter
from llm_relay.proxy.dbwriter import DBWriter


class _FakeConn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _make_writer(tmp_path, fail_times: int = 0):
    calls = []
    state = {"fail": fail_times, "conns": 0}

    def factory():
        state["conns"] += 1
        return _FakeConn()

    def fn(conn, **kw):
        if state["fail"] > 0:
            state["fail"] -= 1
            raise RuntimeError("db down")
        calls.append(kw)

    writer = DBWriter(conn_factory=factory, spill_dir=tmp_path)
    return writer, fn, calls, state


def test_writes_in_submission_order(tmp_path):
    writer, fn, calls, _ = _make_writer(tmp_path)
    writer.start()
    for i in range(50):
        writer.submit(fn, i=i)
    writer.stop(timeout=5)
    assert [c["i"] for c in calls] == list(range(50))
    assert writer.stats["written"] == 50
    assert writer.stats["spilled"] == 0


def test_retries_on_fresh_connection_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr("llm_relay.proxy.dbwriter.time.sleep", lambda s: None)
    writer, fn, calls, state = _make_writer(tmp_path, fail_times=2)
    writer.start()
    writer.submit(fn, i=1)
    writer._q.join()  # retries run before stop(): a stopping writer spills instead of backing off
    writer.stop(timeout=5)
    assert calls == [{"i": 1}]
    assert state["conns"] == 3  # reconnected after each failed attempt
    assert writer.stats["spilled"] == 0


def test_persistent_failure_spills_instead_of_dropping(tmp_path, monkeypatch):
    monkeypatch.setattr("llm_relay.proxy.dbwriter.time.sleep", lambda s: None)
    monkeypatch.setattr("llm_relay.proxy.dbwriter._RETRIES", 2)
    writer, fn, calls, _ = _make_writer(tmp_path, fail_times=99)
    writer.start()
    writer.submit(fn, session_id="s1", payload={"a": 1})
    writer._q.join()  # all retries happen before stop() (see above)
    writer.stop(timeout=5)
    files = list(tmp_path.glob("spill-*.jsonl"))
    assert len(files) == 1
    record = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    assert record["fn"] == "fn"
    assert record["reason"] == "retries exhausted"
    assert record["kwargs"] == {"session_id": "s1", "payload": {"a": 1}}
    assert writer.stats["spilled"] == 1
    assert calls == []


def test_submit_after_stop_writes_directly(tmp_path):
    writer, fn, calls, _ = _make_writer(tmp_path)
    writer.start()
    writer.stop(timeout=5)
    writer.submit(fn, i=7)
    assert calls == [{"i": 7}]
    assert writer.stats["direct"] == 1


def test_shutdown_timeout_spills_leftover_jobs(tmp_path):
    gate = threading.Event()
    calls = []

    def slow_fn(conn, **kw):
        gate.wait(5)
        calls.append(kw)

    writer = DBWriter(conn_factory=lambda: _FakeConn(), spill_dir=tmp_path)
    writer.start()
    writer.submit(slow_fn, i=1)
    writer.submit(slow_fn, i=2)
    writer.stop(timeout=0.2)  # first job still blocked -> the queued second job must be spilled
    gate.set()
    writer._q.put(None)  # let the worker thread exit after finishing job 1
    files = list(tmp_path.glob("spill-*.jsonl"))
    assert files, "leftover job was not spilled"
    spilled = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    # Since R9-10b the job still in progress (1) is spilled too, after the queued one.
    assert [(r["kwargs"], r["reason"]) for r in spilled] == [
        ({"i": 2}, "shutdown timeout"),
        ({"i": 1}, "shutdown timeout (in progress; may also have been written)"),
    ]
    assert writer.stats["spilled"] == 2


# ── queue byte cap (LLM_RELAY_DB_QUEUE_MAX_BYTES) ──


def _blocked_writer(tmp_path):
    """A writer whose consumer blocks on ``gate`` so jobs pile up in the queue."""
    gate = threading.Event()
    calls = []

    def slow_fn(conn, **kw):
        gate.wait(5)
        calls.append(kw)

    writer = DBWriter(conn_factory=lambda: _FakeConn(), spill_dir=tmp_path)
    writer.start()
    return writer, slow_fn, calls, gate


def test_queued_bytes_follow_size_hints_until_the_job_is_written(tmp_path):
    writer, slow_fn, calls, gate = _blocked_writer(tmp_path)
    writer.submit(slow_fn, size_hint=1000, i=1)  # taken by the worker at once, still counted
    writer.submit(slow_fn, size_hint=500, i=2)
    writer.submit(slow_fn, i=3)

    snap = writer.snapshot()
    assert snap["queued_bytes"] == 3 * dbwriter._JOB_OVERHEAD_BYTES + 1500
    assert snap["queue_max_bytes"] == dbwriter._QUEUE_MAX_BYTES
    gate.set()
    writer.stop(timeout=5)
    assert [c["i"] for c in calls] == [1, 2, 3]  # size_hint is not passed on to the job
    assert writer.snapshot()["queued_bytes"] == 0


def test_a_job_over_the_byte_cap_is_spilled_at_once_and_order_is_kept(tmp_path, monkeypatch):
    overhead = dbwriter._JOB_OVERHEAD_BYTES
    monkeypatch.setattr("llm_relay.proxy.dbwriter._QUEUE_MAX_BYTES", 2 * overhead + 150)
    writer, slow_fn, calls, gate = _blocked_writer(tmp_path)
    writer.submit(slow_fn, size_hint=100, i=1)  # fits
    writer.submit(slow_fn, size_hint=100, i=2)  # would exceed the cap -> spilled now
    writer.submit(slow_fn, size_hint=40, i=3)  # fits behind job 1

    assert writer.stats["spilled"] == 1
    assert writer.snapshot()["queued_bytes"] == 2 * overhead + 140
    gate.set()
    writer.stop(timeout=5)
    assert [c["i"] for c in calls] == [1, 3]
    spilled = [json.loads(line) for line in next(tmp_path.glob("spill-*.jsonl")).read_text("utf-8").splitlines()]
    assert [(r["kwargs"], r["reason"]) for r in spilled] == [({"i": 2}, "queue bytes")]
    assert writer.snapshot()["queued_bytes"] == 0


def test_zero_disables_the_byte_cap(tmp_path, monkeypatch):
    monkeypatch.setattr("llm_relay.proxy.dbwriter._QUEUE_MAX_BYTES", 0)
    writer, fn, calls, _ = _make_writer(tmp_path)
    writer.start()
    writer.submit(fn, size_hint=10**12, i=1)
    writer.stop(timeout=5)
    assert calls == [{"i": 1}]
    assert writer.stats["spilled"] == 0


def test_shutdown_drain_releases_the_bytes_of_spilled_jobs(tmp_path):
    writer, slow_fn, _calls, gate = _blocked_writer(tmp_path)
    writer.submit(slow_fn, size_hint=500, i=1)
    writer.submit(slow_fn, size_hint=500, i=2)
    writer.stop(timeout=0.2)  # job 1 still blocked -> job 2 is drained to the spill file

    assert writer.stats["spilled"] == 2  # job 2 drained, job 1 spilled as in progress (R9-10b)
    assert writer.snapshot()["queued_bytes"] == dbwriter._JOB_OVERHEAD_BYTES + 500  # job 1 still running
    gate.set()
    writer._q.put(None)
    writer._thread.join(5)
    assert writer.snapshot()["queued_bytes"] == 0


# ── shutdown with a job in progress (2026-10-23, R9-10b) ──

IN_PROGRESS_REASON = "shutdown timeout (in progress; may also have been written)"


def _spilled(tmp_path):
    records = []
    for path in sorted(tmp_path.glob("spill-*.jsonl")):
        records.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    return records


def test_shutdown_timeout_spills_the_job_in_progress(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def stuck_factory():  # the database does not answer: the worker hangs connecting
        entered.set()
        release.wait(5)
        raise RuntimeError("db unreachable")

    def fn(conn, **kw):
        raise AssertionError("never reached without a connection")

    writer = DBWriter(conn_factory=stuck_factory, spill_dir=tmp_path)
    writer.start()
    writer.submit(fn, i=1)
    assert entered.wait(5)
    writer.stop(timeout=0.2)  # job 1 is neither queued nor written: it used to be lost here

    assert writer.stats["spilled"] == 1
    assert [(r["kwargs"], r["reason"]) for r in _spilled(tmp_path)] == [({"i": 1}, IN_PROGRESS_REASON)]

    release.set()  # the connect fails; the worker sees the shutdown and does not spill job 1 again
    writer._q.put(None)  # the drain consumed stop()'s sentinel
    writer._thread.join(5)
    assert not writer._thread.is_alive()
    assert writer.stats["spilled"] == 1 and len(_spilled(tmp_path)) == 1
    assert writer.snapshot()["queued_bytes"] == 0


def test_failing_job_is_spilled_without_backoff_once_stopping(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr("llm_relay.proxy.dbwriter.time.sleep", sleeps.append)
    writer, fn, calls, _ = _make_writer(tmp_path, fail_times=99)
    writer._stopped = True  # stop() has been called while this job was being written

    writer._write_or_spill((fn, {"i": 1}, 0), attempts=3)

    assert sleeps == []  # no retry backoff that would run into the drain timeout
    assert (writer.stats["retried"], writer.stats["spilled"]) == (1, 1)
    assert [(r["kwargs"], r["reason"]) for r in _spilled(tmp_path)] == [({"i": 1}, "shutdown")]
    assert calls == []


def test_always_failing_database_loses_nothing_at_stop(tmp_path, monkeypatch):
    monkeypatch.setattr("llm_relay.proxy.dbwriter.time.sleep", lambda s: None)

    def broken_factory():
        raise RuntimeError("db down")

    writer = DBWriter(conn_factory=broken_factory, spill_dir=tmp_path)
    writer.start()
    writer.submit(lambda conn, **kw: None, session_id="s-stop")
    writer.stop(timeout=5)

    assert (writer.stats["written"], writer.stats["spilled"]) == (0, 1)
    records = _spilled(tmp_path)
    assert len(records) == 1 and records[0]["kwargs"] == {"session_id": "s-stop"}
    assert not writer.alive() and writer._current is None


def test_normal_stop_writes_everything_and_spills_nothing(tmp_path):
    writer, fn, calls, _ = _make_writer(tmp_path)
    writer.start()
    for i in range(5):
        writer.submit(fn, i=i)
    writer.stop(timeout=5)

    assert [c["i"] for c in calls] == list(range(5))
    assert writer.stats["spilled"] == 0 and _spilled(tmp_path) == []
    assert not writer.alive() and writer._current is None
