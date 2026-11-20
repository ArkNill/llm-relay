"""Tests for the read-only report commands over the provider_* tables."""

from __future__ import annotations

import sqlite3

import pytest

from llm_relay.antigravity import PROVIDER, report, store


@pytest.fixture
def conn(tmp_path):
    connection = sqlite3.connect(str(tmp_path / "report.sqlite"))
    connection.row_factory = sqlite3.Row
    store.ensure_schema(connection)
    yield connection
    connection.close()


def _turn(conn, session, number, prompt, answer, prompt_tokens, new_messages=None):
    rid = store.log_request(conn, provider=PROVIDER, endpoint="streamGenerateContent", session_id=session,
                            model="gemini-4-neon-xhigh", prompt_tokens=prompt_tokens, output_tokens=10,
                            thinking_tokens=100, latency_ms=2500.0, status_code=200)
    store.log_turn(conn, provider=PROVIDER, session_id=session, turn_number=number,
                   storage_mode="full" if number == 1 else "delta",
                   request_messages=new_messages or [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
                   response_message=[{"type": "text", "text": answer}], model="gemini-4-neon-xhigh",
                   request_row_id=rid, last_user_prompt=prompt)


def test_quota_reports_latest_row_per_bucket_as_percent(conn):
    store.log_quota(conn, provider=PROVIDER, buckets=[
        {"bucket_id": "gemini-weekly", "quota_window": "weekly", "remaining_fraction": 0.9, "reset_time": "t1"}])
    store.log_quota(conn, provider=PROVIDER, buckets=[
        {"bucket_id": "gemini-weekly", "quota_window": "weekly", "remaining_fraction": 0.8482, "reset_time": "t2"},
        {"bucket_id": "gemini-5h", "quota_window": "5h", "remaining_fraction": None, "reset_time": "t3"}])
    rows = {r["bucket_id"]: r for r in report.quota(conn)}
    assert rows["gemini-weekly"]["remaining_pct"] == 84.8 and rows["gemini-weekly"]["reset_time"] == "t2"
    assert rows["gemini-5h"]["remaining_pct"] is None


def test_recent_groups_turns_by_conversation_newest_first(conn):
    _turn(conn, "s-old", 1, "old question", "old answer", 9000)
    _turn(conn, "s-new", 1, "first prompt", "a1", 10000)
    _turn(conn, "s-new", 2, "first prompt", "a2", 14000)
    rows = report.recent(conn, 5)
    assert [r["session_id"] for r in rows] == ["s-new", "s-old"]
    new = rows[0]
    assert new["turns"] == 2 and new["peak_context"] == 14000 and new["output_tokens"] == 20
    assert new["thinking_tokens"] == 200 and new["model_seconds"] == 5.0 and new["prompt"] == "first prompt"
    assert [r["session_id"] for r in report.recent(conn, 1)] == ["s-new"]


def test_show_prints_new_messages_and_answer_with_truncation(conn):
    _turn(conn, "s1", 1, "question " + "x" * 50, "the answer", 100)
    _turn(conn, "s1", 2, "question", "final", 200, new_messages=[
        {"role": "assistant", "content": [{"type": "tool_use", "name": "read_file", "input": {"Path": "/a"}}]},
        {"role": "user", "content": [{"type": "tool_result", "name": "read_file", "content": {"output": "data"}}]}])
    lines = report.show(conn, "s1", width=20)
    assert lines[0].startswith("=== turn 1")
    assert lines[1].endswith(" …") and "question" in lines[1]
    assert any("[tool_use read_file]" in line for line in lines)
    assert any("[tool_result" in line for line in lines)  # cut at 20 characters
    assert lines[-1].strip().endswith("final")
    assert report.show(conn, "missing") == []


def test_main_commands(conn, monkeypatch, capsys):
    _turn(conn, "s1", 1, "hello", "world", 100)
    store.log_quota(conn, provider=PROVIDER, buckets=[
        {"bucket_id": "gemini-5h", "quota_window": "5h", "remaining_fraction": 0.5, "reset_time": "soon"}])

    class _Keep:
        def __getattr__(self, name):
            return getattr(conn, name)

        def close(self):
            pass

    monkeypatch.setattr(report, "_conn", lambda: _Keep())
    assert report.main(["quota"]) == 0 and "gemini-5h" in capsys.readouterr().out
    assert report.main(["recent", "-n", "3"]) == 0 and "s1" in capsys.readouterr().out
    assert report.main(["show", "s1"]) == 0 and "world" in capsys.readouterr().out
    assert report.main(["show", "nope"]) == 1
    assert report.main([]) == 2
