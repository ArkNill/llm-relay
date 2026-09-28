"""Tests for the background DB writer (proxy/dbwriter.py)."""

from __future__ import annotations

import json
import threading

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
