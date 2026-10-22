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
    assert [r["kwargs"] for r in spilled] == [{"i": 2}]
    assert writer.stats["spilled"] == 1


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

    assert writer.stats["spilled"] == 1
    assert writer.snapshot()["queued_bytes"] == dbwriter._JOB_OVERHEAD_BYTES + 500  # job 1 still running
    gate.set()
    writer._q.put(None)
    writer._thread.join(5)
    assert writer.snapshot()["queued_bytes"] == 0
