"""Tests for the read-only report commands over the provider_* tables."""

from __future__ import annotations

import json
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


# ── 2026-11 extension: show --thinking, aux, blobs, blob ──


class _KeepOpen(sqlite3.Connection):
    """A real sqlite3 connection (store's dialect check needs one) that main() cannot close."""

    def close(self):
        pass


@pytest.fixture
def kept(tmp_path, monkeypatch):
    connection = sqlite3.connect(str(tmp_path / "kept.sqlite"), factory=_KeepOpen)
    connection.row_factory = sqlite3.Row
    store.ensure_schema(connection)
    monkeypatch.setattr(report, "_conn", lambda: connection)
    yield connection
    sqlite3.Connection.close(connection)


def _thinking_turn(conn, session="s-think"):
    store.log_turn(conn, provider=PROVIDER, session_id=session, turn_number=1, storage_mode="full",
                   request_messages=[{"role": "user", "content": [{"type": "text", "text": "q"}]}],
                   response_message=[{"type": "text", "text": "the answer"}],
                   thinking_blocks=[{"type": "thinking", "thinking": "let me think " + "y" * 40}],
                   model="gemini-4-neon-xhigh")


def _aux(conn, request_type, session=None, answer=None, thinking=None, provider=PROVIDER):
    return store.log_request(conn, provider=provider, endpoint="streamGenerateContent", request_type=request_type,
                             session_id=session, model="gemini-4-flash", output_tokens=7, thinking_tokens=3,
                             stream_end="completed", response_message=answer, thinking_blocks=thinking)


def _text(text):
    return [{"type": "text", "text": text}]


def test_show_thinking_puts_thinking_lines_before_the_answer(conn):
    _thinking_turn(conn)
    plain = report.show(conn, "s-think")
    assert not any("(thinking)" in line for line in plain)
    assert plain == report.show(conn, "s-think", thinking=False)
    lines = report.show(conn, "s-think", width=20, thinking=True)
    thinking_at = next(i for i, line in enumerate(lines) if "(thinking) let me" in line)
    answer_at = next(i for i, line in enumerate(lines) if line.endswith("the answer"))
    assert thinking_at < answer_at and lines[thinking_at].endswith(" …")  # --width applies
    assert [line for line in lines if "(thinking)" not in line] == plain


def test_main_show_thinking_flag(kept, capsys):
    _thinking_turn(kept)
    assert report.main(["show", "s-think"]) == 0
    assert "(thinking)" not in capsys.readouterr().out
    assert report.main(["show", "s-think", "--thinking"]) == 0
    out = capsys.readouterr().out
    assert out.index("(thinking) let me") < out.index("the answer")


def test_aux_lists_only_rows_with_a_body_newest_first(conn):
    _aux(conn, "checkpoint", session="s1", answer=_text("summary one"))
    _aux(conn, "agent", session="s1")  # no body: a normal agent call whose turn row holds the answer
    _aux(conn, "web_search", thinking=[{"type": "thinking", "thinking": "searching"}])
    _aux(conn, "checkpoint", session="s2", answer=_text("summary two " + "z" * 300))
    _aux(conn, "checkpoint", session="s3", answer=_text("other provider"), provider="other-cli")
    rows = report.aux(conn)
    assert [r["request_type"] for r in rows] == ["checkpoint", "web_search", "checkpoint"]
    assert rows[0]["body"].startswith("summary two") and rows[0]["body"].endswith(" …")
    assert len(rows[0]["body"]) == 200 + 2
    assert rows[1]["session_id"] is None and rows[1]["body"] == "(thinking) searching"
    assert rows[2] == {"when": rows[2]["when"], "request_type": "checkpoint", "session_id": "s1",
                       "model": "gemini-4-flash", "output_tokens": 7, "thinking_tokens": 3,
                       "stream_end": "completed", "body": "summary one"}
    assert [r["request_type"] for r in report.aux(conn, request_type="web_search")] == ["web_search"]
    assert [r["session_id"] for r in report.aux(conn, limit=2, request_type="checkpoint")] == ["s2", "s1"]
    assert len(report.aux(conn, width=0)[0]["body"]) > 300


def test_main_aux_prints_dash_for_missing_session(kept, capsys):
    _aux(kept, "web_search", answer=_text("found it"))
    _aux(kept, "checkpoint", session="s9", answer=_text("cp"))
    assert report.main(["aux", "-n", "1"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1 and "checkpoint" in out[0] and "s9" in out[0]
    assert report.main(["aux", "--type", "web_search"]) == 0
    line = capsys.readouterr().out.strip()
    assert " - " in line and line.endswith("found it")


SHA_A = "ab12" + "0" * 60
SHA_B = "ab34" + "1" * 60
SHA_C = "cd56" + "2" * 60


def _blobs(conn):
    store.log_blob(conn, provider=PROVIDER, kind="system_instruction", sha256=SHA_A, size_bytes=11,
                   body={"parts": [{"text": "be brief"}]})
    store.log_blob(conn, provider=PROVIDER, kind="tools", sha256=SHA_B, size_bytes=22, body=[{"name": "read_file"}])
    store.log_blob(conn, provider=PROVIDER, kind="tools", sha256=SHA_C, size_bytes=33, body=[{"name": "grep"}])


def test_blobs_lists_newest_first_and_filters_by_kind(conn):
    _blobs(conn)
    rows = report.blobs(conn)
    assert [r["sha256"] for r in rows] == [SHA_C, SHA_B, SHA_A]
    assert [r["size_bytes"] for r in rows] == [33, 22, 11]
    assert [r["sha256"] for r in report.blobs(conn, kind="tools")] == [SHA_C, SHA_B]
    assert report.blobs(conn, kind="experiments") == []


def test_blob_by_prefix(conn):
    _blobs(conn)
    found = report.blob(conn, "AB12")
    assert len(found) == 1 and found[0]["body"] == {"parts": [{"text": "be brief"}]}
    assert {r["sha256"] for r in report.blob(conn, "ab")} == {SHA_A, SHA_B}
    assert report.blob(conn, "ffff") == [] and report.blob(conn, "") == []


def test_main_blobs_and_blob(kept, capsys):
    _blobs(kept)
    assert report.main(["blobs", "--kind", "tools"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2 and SHA_C[:12] in out[0] and SHA_C[:13] not in out[0] and "33 bytes" in out[0]
    assert report.main(["blob", "cd56"]) == 0
    printed = capsys.readouterr().out
    assert json.loads(printed) == [{"name": "grep"}] and "\n  " in printed  # indented
    assert report.main(["blob", "ab"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and SHA_A in captured.err and SHA_B in captured.err
    assert report.main(["blob", "9999"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "no stored blob" in captured.err


def _old_schema(path):
    """A store migrated before 2026-11: the three tables without the new columns, no provider_blobs."""
    connection = sqlite3.connect(str(path), factory=_KeepOpen)
    connection.row_factory = sqlite3.Row
    for table in store.TABLES:
        columns = [(name, sql_type) for name, sql_type in store.column_defs(table, "sqlite")
                   if name not in store.NEW_REQUEST_COLUMNS]
        connection.execute("CREATE TABLE {} ({})".format(table, ", ".join(" ".join(col) for col in columns)))
    connection.commit()
    return connection


@pytest.mark.parametrize("argv, hint", [
    (["aux"], "response_message"),
    (["aux", "--type", "checkpoint"], "response_message"),
    (["blobs"], "provider_blobs"),
    (["blob", "ab"], "provider_blobs"),
])
def test_old_schema_prints_one_line_and_exits_1(tmp_path, monkeypatch, capsys, argv, hint):
    connection = _old_schema(tmp_path / "old.sqlite")
    monkeypatch.setattr(report, "_conn", lambda: connection)
    try:
        assert report.main(argv) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.count("\n") == 1 and hint in captured.err
        assert "run docs/migrate-provider-columns.py" in captured.err
        # The older commands keep working on that store.
        assert report.main(["quota"]) == 0
    finally:
        sqlite3.Connection.close(connection)
