"""Tests for antigravity/wire.py -- agy request / SSE response / usage / quota parsing.

Fixtures are sanitized agy 1.5.13 traffic (see fixtures/README.md). The .sse files
are read in binary mode: events are separated by CRLF CRLF as on the wire.
"""

import hashlib
import json
import random
from pathlib import Path

import pytest

from llm_relay.antigravity.wire import (
    AgyRequest,
    AgyResponse,
    SSEAccumulator,
    content_hash,
    extract_user_request,
    first_message_fingerprint,
    last_user_prompt,
    normalize_contents,
    parse_json_response,
    parse_json_value,
    parse_quota_summary,
    parse_request,
    usage_columns,
)

FIXTURES = Path(__file__).parent / "fixtures"
CONV_ID = "00000000-0000-4000-8000-000000000001"
TRAJECTORY_ID = "00000000-0000-4000-8000-000000000002"
SESSION_ID = "-1234567890123456789"
AGENT_REQUESTS = ["req_turn1.json", "req_turn1_after_tools.json", "req_turn2.json"]
RESPONSES = ["resp_turn1.sse", "resp_turn1_after_tools.sse", "resp_turn2.sse", "resp_monitoring.sse"]
ZERO_USAGE = {
    "prompt_tokens": 0,
    "cache_read": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "thinking_tokens": 0,
    "total_tokens": 0,
}


def _raw(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _body(name: str) -> dict:
    return json.loads(_raw(name))


def _contents(name: str) -> list:
    return _body(name)["request"]["contents"]


def _wire_events(data: bytes) -> list:
    """Independent reading of a fixture: split on the CRLF CRLF separator and decode each event."""
    events = []
    for chunk in data.split(b"\r\n\r\n"):
        if chunk.strip():
            assert chunk.startswith(b"data: ")
            events.append(json.loads(chunk[len(b"data: ") :]))
    return events


def _feed(chunks) -> AgyResponse:
    acc = SSEAccumulator()
    for chunk in chunks:
        acc.feed(chunk)
    return acc.finish()


def _random_chunks(data: bytes, rng: random.Random, max_len: int) -> list:
    chunks, i = [], 0
    while i < len(data):
        n = rng.randint(1, max_len)
        chunks.append(data[i : i + n])
        i += n
    return chunks


def _sse(*events) -> bytes:
    return b"".join(b"data: " + json.dumps(e, ensure_ascii=False).encode("utf-8") + b"\r\n\r\n" for e in events)


def _text_event(text: str, **extra) -> dict:
    return {"response": {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, **extra}]}}


# ── parse_request ──


@pytest.mark.parametrize(
    ("name", "step_index", "message_count"),
    [("req_turn1.json", 0, 1), ("req_turn1_after_tools.json", 3, 3), ("req_turn2.json", 6, 6)],
)
def test_parse_request_agent_turns(name, step_index, message_count):
    body = _body(name)
    req = parse_request(_raw(name), "streamGenerateContent")
    assert isinstance(req, AgyRequest)
    assert req.method == "streamGenerateContent"
    assert req.request_type == "agent"
    assert req.request_id == body["requestId"]
    assert req.conversation_id == CONV_ID
    assert req.root_conversation_id == CONV_ID
    assert req.trajectory_id == TRAJECTORY_ID
    assert req.step_index == step_index
    assert req.model == "gemini-4-hydrogen-low"
    assert req.thinking_level == "LOW"
    assert req.client_session_id == SESSION_ID
    assert req.project == "aicode-consumers"
    assert req.message_count == message_count
    assert req.system_chars == len("SYSTEM PROMPT PLACEHOLDER (fixture)")
    assert req.tools_count == 2
    assert req.contents == body["request"]["contents"]


def test_parse_request_monitoring_side_call():
    req = parse_request(_raw("req_monitoring.json"), "streamGenerateContent")
    assert req is not None
    assert req.request_type == "agent_monitoring"
    assert req.request_id == "agent_monitoring/00000000-0000-4000-8000-000000000004"
    # No labels, and "agent_monitoring/<uuid>" is not a conversation request id.
    assert req.conversation_id is None
    assert req.root_conversation_id is None
    assert req.trajectory_id is None
    assert req.step_index is None
    assert req.model == "gemini-3.5-flash-lite"
    assert req.thinking_level == "MINIMAL"
    assert req.client_session_id == SESSION_ID
    assert req.tools_count == 0
    assert req.message_count == 1


def test_parse_request_conversation_id_falls_back_to_request_id():
    body = _body("req_turn2.json")
    del body["request"]["labels"]
    req = parse_request(json.dumps(body).encode(), "streamGenerateContent")
    assert req is not None
    assert req.conversation_id == CONV_ID
    assert req.root_conversation_id is None
    assert req.step_index is None


def test_parse_request_step_index_and_session_id_coercion():
    body = _body("req_turn1.json")
    body["request"]["labels"]["last_step_index"] = "n/a"
    body["request"]["sessionId"] = 42
    req = parse_request(json.dumps(body).encode(), "streamGenerateContent")
    assert req is not None
    assert req.step_index is None
    assert req.client_session_id == "42"


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'"str"', b"", b"\xff\xfe\x00", None, 12])
def test_parse_request_rejects_non_objects(body):
    assert parse_request(body, "streamGenerateContent") is None


def test_parse_request_tolerates_wrong_field_types():
    body = {"request": {"contents": "x", "labels": [], "tools": [{"functionDeclarations": 3}], "systemInstruction": 7}}
    req = parse_request(json.dumps(body).encode(), "m")
    assert req is not None
    assert (req.message_count, req.tools_count, req.system_chars, req.contents) == (0, 0, 0, [])
    assert req.conversation_id is None


# ── normalize_contents ──


def test_normalize_contents_fixture_turn2():
    contents = _contents("req_turn2.json")
    msgs = normalize_contents(contents)
    assert len(msgs) == len(contents) == 6
    # The tool results arrive with role "model" but are stored as user input.
    assert contents[2]["role"] == "model"
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "user", "user"]
    assert msgs[1]["content"] == [
        {"type": "tool_use", "id": "call_0001", "name": "read_file", "input": {"Path": "/work/proj/note.txt"}},
        {
            "type": "tool_use",
            "id": "call_0002",
            "name": "run_command",
            "input": contents[1]["parts"][1]["functionCall"]["args"],
        },
    ]
    results = msgs[2]["content"]
    assert [b["type"] for b in results] == ["tool_result", "tool_result"]
    assert results[0]["tool_use_id"] == "call_0001"
    assert results[0]["content"] is contents[2]["parts"][0]["functionResponse"]["response"]
    assert "error" in results[1]["content"]
    assert msgs[3]["content"] == [{"type": "text", "text": contents[3]["parts"][0]["text"]}]
    dumped = json.dumps(msgs)
    assert "thoughtSignature" not in dumped
    assert "c2lnbmF0dXJl" not in dumped


def test_normalize_contents_part_kinds():
    payload = "QUJD" * 5000
    contents = [
        {
            "role": "user",
            "parts": [
                {"text": "look"},
                {"inlineData": {"mimeType": "image/png", "data": payload}},
                {"fileData": {"mimeType": "application/pdf", "fileUri": "gs://bucket/doc.pdf"}},
                {"text": ""},
                "not a part",
                {"executableCode": {"code": "x"}, "thoughtSignature": "sig"},
                {"thoughtSignature": "sig"},
            ],
        },
        {"role": "model", "parts": [{"text": "hmm", "thought": True}, {"text": "answer", "thought": False}]},
        {"parts": [{"text": "no role"}]},
        {"role": "user", "parts": [{"functionResponse": {"id": "a", "name": "t", "response": {"output": 1}}}]},
    ]
    msgs = normalize_contents(contents)
    assert len(msgs) == 4
    assert msgs[0] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "look"},
            {"type": "inline_data", "mime_type": "image/png", "omitted_bytes": len(payload)},
            {"type": "file", "mime_type": "application/pdf", "uri": "gs://bucket/doc.pdf"},
            {"type": "unknown", "keys": ["executableCode"]},
        ],
    }
    assert payload not in json.dumps(msgs)
    assert msgs[1] == {
        "role": "assistant",
        "content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "answer"}],
    }
    assert msgs[2]["role"] == "user"
    assert msgs[3]["role"] == "user"
    assert msgs[3]["content"][0]["type"] == "tool_result"


def test_normalize_contents_garbage():
    for garbage in (None, "contents", {"role": "user"}, 5, b"x"):
        assert normalize_contents(garbage) == []
    # Malformed items keep their slot so callers can slice by raw index.
    msgs = normalize_contents([None, "x", {"role": "model", "parts": "nope"}, {"role": "model", "parts": []}])
    assert msgs == [
        {"role": "user", "content": []},
        {"role": "user", "content": []},
        {"role": "assistant", "content": []},
        {"role": "assistant", "content": []},
    ]


# ── SSEAccumulator ──


@pytest.mark.parametrize("name", RESPONSES)
def test_sse_chunking_invariance(name):
    data = _raw(name)
    assert b"\r\n\r\n" in data  # binary read keeps the wire separators
    whole = _feed([data])
    bytewise = _feed([data[i : i + 1] for i in range(len(data))])
    rng = random.Random(20261120)
    randomly = _feed(_random_chunks(data, rng, 64))
    assert whole == bytewise == randomly
    assert whole.n_events == len(_wire_events(data))
    assert whole.n_bad_events == 0
    assert whole.response_bytes == len(data)
    assert whole.error is None
    assert whole.finish_reason == "STOP"
    assert whole.response_id == "resp-fixture"
    assert whole.trace_id == "trace-fixture"
    assert whole.usage == _wire_events(data)[-1]["response"]["usageMetadata"]


def test_sse_chunking_invariance_many_seeds():
    rng = random.Random(1120)
    for name in RESPONSES:
        data = _raw(name)
        expected = _feed([data])
        for _ in range(40):
            assert _feed(_random_chunks(data, rng, rng.choice([2, 7, 50, 400]))) == expected


def test_sse_turn1_thinking_and_two_tool_calls():
    resp = _feed([_raw("resp_turn1.sse")])
    assert len(resp.thinking) == 1
    assert resp.thinking[0]["type"] == "thinking"
    assert resp.thinking[0]["thinking"].startswith("Reading the file `note.txt`")
    assert [b["type"] for b in resp.blocks] == ["tool_use", "tool_use"]
    assert [b["name"] for b in resp.blocks] == ["read_file", "run_command"]
    assert resp.blocks[0]["input"] == {"Path": "/work/proj/note.txt"}
    assert resp.model_version == "gemini-4-hydrogen"


def test_sse_turn1_after_tools_merges_text_and_maps_usage():
    data = _raw("resp_turn1_after_tools.sse")
    resp = _feed([data])
    deltas = [e["response"]["candidates"][0]["content"]["parts"][0]["text"] for e in _wire_events(data)]
    assert len([d for d in deltas if d]) == 5
    assert resp.blocks == [{"type": "text", "text": "".join(deltas)}]
    assert "heliotrope" in resp.blocks[0]["text"]
    assert resp.thinking == []
    cols = usage_columns(resp.usage)
    assert cols["prompt_tokens"] == 10685
    assert cols["cache_read"] == 8225
    assert cols["input_tokens"] == 2460
    assert cols == {
        "prompt_tokens": 10685,
        "cache_read": 8225,
        "input_tokens": 2460,
        "output_tokens": 118,
        "thinking_tokens": 81,
        "total_tokens": 10884,
    }


def test_sse_monitoring_thought_run_then_text_run():
    resp = _feed([_raw("resp_monitoring.sse")])
    assert len(resp.thinking) == 1
    assert [b["type"] for b in resp.blocks] == ["text"]
    data = _raw("resp_monitoring.sse")
    parts = [e["response"]["candidates"][0]["content"]["parts"][0] for e in _wire_events(data)]
    assert resp.thinking[0]["thinking"] == "".join(p["text"] for p in parts if p.get("thought"))
    assert resp.blocks[0]["text"] == "".join(p["text"] for p in parts if not p.get("thought"))


def test_sse_tool_use_ends_text_run_and_thought_never_in_blocks():
    parts = [
        {"text": "a"},
        {"text": "b"},
        {"text": "t1", "thought": True},
        {"text": "c"},
        {"functionCall": {"name": "f", "args": {}}},
        {"text": ""},
        {"text": "d"},
    ]
    events = [{"response": {"candidates": [{"content": {"role": "model", "parts": [p]}}]}} for p in parts]
    resp = _feed([_sse(*events)])
    assert resp.blocks == [
        {"type": "text", "text": "ab"},
        {"type": "text", "text": "c"},
        {"type": "tool_use", "id": None, "name": "f", "input": {}},
        {"type": "text", "text": "d"},
    ]
    assert resp.thinking == [{"type": "thinking", "thinking": "t1"}]


@pytest.mark.parametrize("cut", [1, 2])
def test_sse_multibyte_character_split_across_chunks(cut):
    data = _sse(_text_event("비밀 단어"))
    start = data.index("비".encode())
    resp = _feed([data[: start + cut], data[start + cut :]])
    assert resp.blocks == [{"type": "text", "text": "비밀 단어"}]
    assert resp.n_bad_events == 0


def test_sse_error_event_sets_error_and_counts():
    error = {"error": {"code": 429, "message": "Resource has been exhausted", "status": "RESOURCE_EXHAUSTED"}}
    resp = _feed([_sse(_text_event("partial"), error)])
    assert resp.error == "RESOURCE_EXHAUSTED: Resource has been exhausted"
    assert resp.n_events == 2
    assert resp.blocks == [{"type": "text", "text": "partial"}]
    nested = _feed([_sse({"response": {"error": {"code": 500, "message": "internal"}}})])
    assert nested.error == "500: internal"
    assert nested.n_events == 1


def test_sse_bare_gemini_event():
    bare = {
        "candidates": [{"content": {"role": "model", "parts": [{"text": "hi"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 1, "totalTokenCount": 6},
        "modelVersion": "gemini-x",
        "responseId": "r1",
    }
    resp = _feed([_sse(bare)])
    assert resp.blocks == [{"type": "text", "text": "hi"}]
    assert resp.finish_reason == "STOP"
    assert resp.model_version == "gemini-x"
    assert resp.response_id == "r1"
    assert resp.trace_id is None
    assert usage_columns(resp.usage)["input_tokens"] == 5


def test_sse_final_line_without_newline_is_flushed():
    data = b"data: " + json.dumps(_text_event("tail")).encode()
    acc = SSEAccumulator()
    acc.feed(data)
    resp = acc.finish()
    assert resp.n_events == 1
    assert resp.blocks == [{"type": "text", "text": "tail"}]
    # Also when the final line ends with a lone CR.
    assert _feed([data + b"\r"]).blocks == [{"type": "text", "text": "tail"}]


def test_sse_ignored_and_bad_lines():
    data = (
        b": keep-alive\r\n"
        b"event: message\r\n"
        b"id: 7\r\n"
        b"data: [DONE]\r\n"
        b"data:\r\n"
        b"data:    \r\n"
        b"data: not json\r\n"
        b"data: [1, 2]\r\n"
        b'data: {"response": "odd"}\r\n'
        b"\r\n"
    )
    resp = _feed([data])
    assert resp.n_bad_events == 2
    assert resp.n_events == 1  # {"response": "odd"} is an object, just an empty one
    assert resp.blocks == [] and resp.thinking == [] and resp.error is None


def test_sse_finish_without_feed_and_non_bytes_chunks():
    empty = SSEAccumulator().finish()
    assert empty == AgyResponse(
        blocks=[],
        thinking=[],
        usage=None,
        model_version=None,
        finish_reason=None,
        response_id=None,
        trace_id=None,
        n_events=0,
        n_bad_events=0,
        error=None,
        response_bytes=0,
    )
    acc = SSEAccumulator()
    for junk in (None, 3, [b"x"], b"", bytearray(b"data: {}\n"), memoryview(b"data: {}\n")):
        acc.feed(junk)
    resp = acc.finish()
    assert resp.n_events == 2
    assert resp.response_bytes == 2 * len(b"data: {}\n")


# ── parse_json_response ──


def test_parse_json_response_array_matches_sse():
    data = _raw("resp_turn1_after_tools.sse")
    events = _wire_events(data)
    body = json.dumps(events, ensure_ascii=False).encode("utf-8")
    resp = parse_json_response(body)
    sse = _feed([data])
    assert resp.blocks == sse.blocks
    assert resp.usage == sse.usage
    assert resp.n_events == len(events)
    assert resp.response_bytes == len(body)


def test_parse_json_response_single_object_error_and_garbage():
    err = parse_json_response(b'{"error": {"code": 403, "message": "denied", "status": "PERMISSION_DENIED"}}')
    assert err.error == "PERMISSION_DENIED: denied"
    assert err.n_events == 1
    bad = parse_json_response(b"<html>oops</html>")
    assert (bad.n_events, bad.n_bad_events, bad.blocks) == (0, 1, [])
    assert parse_json_response(b"").n_bad_events == 0
    assert parse_json_response(None).n_events == 0


# ── last_user_prompt / extract_user_request ──


def test_last_user_prompt_skips_harness_notice():
    contents = _contents("req_turn2.json")
    assert contents[-1]["parts"][0]["text"].startswith("The following is a <SYSTEM_MESSAGE>")
    assert last_user_prompt(contents) == "Now reply with exactly: done"


def test_last_user_prompt_skips_tool_results_and_model_turns():
    expected = (
        "Do two things and report each in one short line. (1) Read note.txt in the current directory and "
        "report the secret word. (2) Run the shell command: echo hello > created.txt ."
    )
    assert last_user_prompt(_contents("req_turn1.json")) == expected
    assert last_user_prompt(_contents("req_turn1_after_tools.json")) == expected
    assert last_user_prompt(_contents("req_monitoring.json")) == "MONITORING PROMPT PLACEHOLDER (fixture)"


def test_last_user_prompt_garbage():
    assert last_user_prompt(None) is None
    assert last_user_prompt([]) is None
    assert last_user_prompt([None, 3, {"role": "user", "parts": [{"text": "   "}]}]) is None
    notice_only = [{"role": "user", "parts": [{"text": "The following is a <SYSTEM_MESSAGE> x"}]}]
    assert last_user_prompt(notice_only) is None


def test_extract_user_request():
    assert extract_user_request("<USER_REQUEST>\n hi \n</USER_REQUEST>\n<ADDITIONAL_METADATA>t") == "hi"
    assert extract_user_request("  plain text \n") == "plain text"
    assert extract_user_request("<USER_REQUEST> unclosed") == "<USER_REQUEST> unclosed"
    assert extract_user_request("<USER_REQUEST>a</USER_REQUEST><USER_REQUEST>b</USER_REQUEST>") == "a"
    assert extract_user_request(None) == ""


# ── first_message_fingerprint ──


def test_first_message_fingerprint_identifies_conversation():
    prints = [first_message_fingerprint(_contents(name)) for name in AGENT_REQUESTS]
    assert len(set(prints)) == 1
    fp = prints[0]
    assert fp is not None and len(fp) == 16
    int(fp, 16)
    monitoring = first_message_fingerprint(_contents("req_monitoring.json"))
    assert monitoring is not None and monitoring != fp
    # Same formula as the spec: sha256 over the sorted-key JSON of the first normalized message.
    contents = _contents("req_turn1.json")
    payload = json.dumps(normalize_contents(contents)[:1], sort_keys=True, ensure_ascii=False)
    assert fp == hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def test_first_message_fingerprint_ignores_thought_signature():
    contents = _contents("req_turn1.json")
    signed = json.loads(json.dumps(contents))
    signed[0]["parts"][0]["thoughtSignature"] = "c2lnbmF0dXJl"
    assert first_message_fingerprint(signed) == first_message_fingerprint(contents)


@pytest.mark.parametrize("contents", [None, [], "abc", {"role": "user"}])
def test_first_message_fingerprint_empty(contents):
    assert first_message_fingerprint(contents) is None


# ── usage_columns / parse_quota_summary ──


@pytest.mark.parametrize("usage", [None, "garbage", [], 12, {"promptTokenCount": "lots", "totalTokenCount": None}])
def test_usage_columns_zeros(usage):
    assert usage_columns(usage) == ZERO_USAGE


def test_usage_columns_coercion():
    cols = usage_columns({"promptTokenCount": 10, "cachedContentTokenCount": 25, "candidatesTokenCount": True})
    assert cols["input_tokens"] == 0  # never negative
    assert cols["output_tokens"] == 0  # bool is not a count
    assert usage_columns({"promptTokenCount": "7", "thoughtsTokenCount": 3.0})["prompt_tokens"] == 7
    assert usage_columns({"thoughtsTokenCount": 3.0})["thinking_tokens"] == 3


def test_parse_quota_summary_fixture():
    rows = parse_quota_summary(_raw("quota_summary.json"))
    assert len(rows) == 5
    assert rows[0] == {
        "group_name": "Gemini Models",
        "bucket_id": "gemini-weekly",
        "quota_window": "weekly",
        "remaining_fraction": 0.9669207,
        "reset_time": "2026-11-27T07:10:34Z",
        "display_name": "Weekly Limit Remaining",
    }
    bucket_ids = ["gemini-weekly", "gemini-5h", "3p-weekly", "3p-5h", "gemini-argon-wtu-weekly"]
    assert [r["bucket_id"] for r in rows] == bucket_ids
    assert all(isinstance(r["remaining_fraction"], float) for r in rows)


def test_parse_quota_summary_garbage_and_missing_bucket_id():
    for garbage in (b"not json", b"[]", b'{"groups": "x"}', b"", None):
        assert parse_quota_summary(garbage) == []
    body = {"groups": [{"displayName": "G", "buckets": [{"window": "5h"}, {"bucketId": "b", "window": "5h"}]}, "x"]}
    rows = parse_quota_summary(json.dumps(body).encode())
    assert [r["bucket_id"] for r in rows] == ["b"]
    assert rows[0]["remaining_fraction"] is None


# ── Never-raise contract ──


def test_public_functions_never_raise_on_garbage():
    nested = b"[" * 100000  # deep nesting makes json raise RecursionError
    garbage = [None, 0, 1.5, "s", b"\xff", nested, [], {}, [None], {"request": None}, {"groups": [None]}, object()]
    for value in garbage:
        parse_request(value, "m")
        normalize_contents(value)
        extract_user_request(value)
        last_user_prompt(value)
        first_message_fingerprint(value)
        parse_json_response(value)
        usage_columns(value)
        parse_quota_summary(value)
        acc = SSEAccumulator()
        acc.feed(value)
        acc.feed(b"data: " + nested + b"\n")
        assert isinstance(acc.finish(), AgyResponse)
    assert first_message_fingerprint([{"role": "user", "parts": [{"text": "\ud800"}]}]) is not None


# ── 2026-11 capture extension: candidate extras, part timing, content hashes ──

MIB = 1024 * 1024


def _canonical(obj) -> bytes:
    """Spec serialization, written out independently of wire.py."""
    text = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return text.encode("utf-8", "replace")


def _candidate_event(parts, metadata=None, **candidate_keys) -> dict:
    event = {"response": {"candidates": [{"content": {"role": "model", "parts": parts}, **candidate_keys}]}}
    if metadata is not None:
        event["metadata"] = metadata
    return event


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return self.now


def test_extra_collects_other_candidate_keys_last_non_empty_value_wins():
    events = [
        _candidate_event([{"text": "a"}], metadata={}, index=0, groundingMetadata={"webSearchQueries": ["first"]},
                         citationMetadata={"citations": [{"uri": "u1"}]}, avgLogprobs=0, blocked=False),
        _candidate_event([{"text": "b"}], metadata={"tag": "m1"}, groundingMetadata={"webSearchQueries": ["second"]},
                         citationMetadata={}, safetyRatings=[], note="", other=None),
        _candidate_event([], metadata={}, finishReason="STOP", citationMetadata=None),
    ]
    resp = _feed([_sse(*events)])
    assert resp.extra == {
        "groundingMetadata": {"webSearchQueries": ["second"]},  # last value
        "citationMetadata": {"citations": [{"uri": "u1"}]},  # later {} and None do not overwrite
        "avgLogprobs": 0,  # 0 and False are values, not empty
        "blocked": False,
        "_event_metadata": {"tag": "m1"},  # later {} does not overwrite
    }
    assert resp.blocks == [{"type": "text", "text": "ab"}] and resp.finish_reason == "STOP"


def test_extra_only_from_candidate_zero_and_event_metadata_without_candidates():
    event = {"response": {"candidates": [
        {"index": 1, "content": {"parts": [{"text": "other"}]}, "groundingMetadata": {"x": 1}},
        {"index": 0, "content": {"parts": [{"text": "ok"}]}, "logprobsResult": {"y": 2}},
    ]}}
    assert _feed([_sse(event)]).extra == {"logprobsResult": {"y": 2}}
    no_candidates = {"response": {"usageMetadata": {"promptTokenCount": 1}}, "metadata": {"m": 1}}
    assert _feed([_sse(no_candidates)]).extra == {"_event_metadata": {"m": 1}}
    bare = {"candidates": [{"content": {"parts": [{"text": "hi"}]}, "urlContextMetadata": {"u": [1]}}],
            "metadata": {"bare": True}}
    assert _feed([_sse(bare)]).extra == {"urlContextMetadata": {"u": [1]}, "_event_metadata": {"bare": True}}


def test_extra_above_one_mib_is_replaced_by_its_size():
    def extra_of_size(size):
        base = len(_canonical({"groundingMetadata": {"blob": ""}}))
        pad = size - base
        value = {"groundingMetadata": {"blob": "가" * (pad // 3) + "a" * (pad % 3)}}  # 가 = 3 UTF-8 bytes
        assert len(_canonical(value)) == size
        return value

    at_limit = extra_of_size(MIB)
    resp = _feed([_sse(_candidate_event([{"text": "x"}], **at_limit))])
    assert resp.extra == at_limit  # exactly 1 MiB is kept
    over = extra_of_size(MIB + 1)
    resp = _feed([_sse(_candidate_event([{"text": "x"}], **over))])
    assert resp.extra == {"_omitted_bytes": MIB + 1}
    assert resp.blocks == [{"type": "text", "text": "x"}]
    # The bound applies to the final value: a large early value later replaced is not counted.
    small = {"groundingMetadata": {"blob": "small"}}
    resp = _feed([_sse(_candidate_event([{"text": "x"}], **over), _candidate_event([{"text": "y"}], **small))])
    assert resp.extra == small


def test_parse_json_response_collects_extra_and_never_times():
    body = json.dumps([
        _candidate_event([{"text": "plan", "thought": True}]),
        _candidate_event([{"text": "answer"}], metadata={"k": "v"}, groundingMetadata={"q": ["x"]}),
    ]).encode()
    resp = parse_json_response(body)
    assert resp.extra == {"groundingMetadata": {"q": ["x"]}, "_event_metadata": {"k": "v"}}
    assert resp.first_thought_at is None and resp.first_answer_at is None


@pytest.mark.parametrize("name", RESPONSES)
def test_fixture_responses_have_no_extra_and_no_timing_without_clock(name):
    resp = _feed([_raw(name)])  # their candidates carry only content / finishReason, their metadata is {}
    assert resp.extra == {}
    assert resp.first_thought_at is None and resp.first_answer_at is None
    assert SSEAccumulator(clock=None).finish().first_answer_at is None


def test_clock_records_first_thought_and_first_answer_once():
    clock = _FakeClock()
    acc = SSEAccumulator(clock=clock)
    timeline = [
        (1.0, {"text": "", "thoughtSignature": "sig"}),  # nothing kept: not a thought, not an answer
        (2.0, {"text": "plan", "thought": True}),
        (3.0, {"text": " more", "thought": True}),
        (4.0, {"thoughtSignature": "sig"}),
        (5.0, {"text": "answer"}),
        (6.0, {"functionCall": {"name": "f", "args": {}}}),
        (7.0, {"text": "late thought", "thought": True}),
    ]
    for now, part in timeline:
        clock.now = now
        acc.feed(_sse(_candidate_event([part])))
    resp = acc.finish()
    assert (resp.first_thought_at, resp.first_answer_at) == (2.0, 5.0)
    assert clock.calls == 2  # read once per kind
    assert resp.thinking == [
        {"type": "thinking", "thinking": "plan more"},
        {"type": "thinking", "thinking": "late thought"},
    ]
    assert [b["type"] for b in resp.blocks] == ["text", "tool_use"]


def test_clock_tool_use_counts_as_answer_and_a_missing_kind_stays_none():
    data = _raw("resp_turn1.sse")  # thought deltas, then two functionCall parts
    events = _wire_events(data)
    kinds = []
    for event in events:
        parts = event["response"]["candidates"][0]["content"]["parts"]
        kinds.append({"thought" if p.get("thought") and p.get("text") else "answer"
                      for p in parts if p.get("functionCall") or p.get("text")})
    first_thought = next(i for i, k in enumerate(kinds) if "thought" in k)
    first_answer = next(i for i, k in enumerate(kinds) if "answer" in k)
    assert first_thought < first_answer
    clock = _FakeClock()
    acc = SSEAccumulator(clock=clock)
    for i, chunk in enumerate(data.split(b"\r\n\r\n")):
        clock.now = float(i)
        acc.feed(chunk + b"\r\n\r\n")
    resp = acc.finish()
    assert (resp.first_thought_at, resp.first_answer_at) == (float(first_thought), float(first_answer))
    assert [b["type"] for b in resp.blocks] == ["tool_use", "tool_use"]

    text_only = SSEAccumulator(clock=lambda: 9.5)
    text_only.feed(_sse(_text_event("hi")))
    resp = text_only.finish()
    assert (resp.first_thought_at, resp.first_answer_at) == (None, 9.5)


@pytest.mark.parametrize("clock_value", ["raise", "not a number"])
def test_failing_clock_is_dropped_and_parsing_continues(clock_value):
    calls = []

    def clock():
        calls.append(1)
        if clock_value == "raise":
            raise RuntimeError("clock broke")
        return clock_value

    acc = SSEAccumulator(clock=clock)
    acc.feed(_sse(_candidate_event([{"text": "t", "thought": True}]), _text_event("a"), _text_event("b")))
    resp = acc.finish()
    assert (resp.first_thought_at, resp.first_answer_at) == (None, None)
    assert resp.blocks == [{"type": "text", "text": "ab"}]
    assert resp.thinking == [{"type": "thinking", "thinking": "t"}]
    assert len(calls) == 1


def test_agy_response_new_fields_default_per_instance():
    args = dict(blocks=[], thinking=[], usage=None, model_version=None, finish_reason=None, response_id=None,
                trace_id=None, n_events=0, n_bad_events=0, error=None, response_bytes=0)
    first, second = AgyResponse(**args), AgyResponse(**args)
    assert first.extra == {} and first.extra is not second.extra
    assert first.first_thought_at is None and first.first_answer_at is None


# ── content_hash / parse_json_value / request originals ──


def test_content_hash_is_canonical_and_ignores_key_order():
    a = {"role": "user", "parts": [{"text": "시스템 프롬프트", "n": 1}, {"z": None, "a": [3, 1]}]}
    b = {"parts": [{"n": 1, "text": "시스템 프롬프트"}, {"a": [3, 1], "z": None}], "role": "user"}
    assert content_hash(a) == content_hash(b)
    data = _canonical(a)
    assert content_hash(a) == (hashlib.sha256(data).hexdigest(), len(data))
    digest, _ = content_hash(a)
    assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")
    assert content_hash("가") == (hashlib.sha256('"가"'.encode()).hexdigest(), 5)  # UTF-8 bytes, not chars
    assert content_hash({"a": 1}) != content_hash({"a": 2})
    assert content_hash([1, 2]) != content_hash([2, 1])  # list order is content


@pytest.mark.parametrize("value", [None, {}, [], ""])
def test_content_hash_of_nothing_is_none(value):
    assert content_hash(value) is None


def test_content_hash_never_raises():
    assert content_hash({1: "a", "b": 2}) is None  # sort_keys cannot order int and str keys
    deep = []
    node = deep
    for _ in range(100000):
        child = []
        node.append(child)
        node = child
    assert content_hash(deep) is None  # RecursionError
    assert content_hash({"s": "\ud800"}) is not None  # a lone surrogate is replaced, not fatal
    marker = object()
    assert content_hash({"o": marker}) == content_hash({"o": str(marker)})  # default=str


def test_parse_request_carries_system_instruction_and_tools():
    body = _body("req_turn1.json")
    req = parse_request(_raw("req_turn1.json"), "streamGenerateContent")
    assert req is not None
    assert req.system_instruction == body["request"]["systemInstruction"]
    assert req.tools == body["request"]["tools"]
    assert content_hash(req.system_instruction) == content_hash(body["request"]["systemInstruction"])
    monitoring = parse_request(_raw("req_monitoring.json"), "streamGenerateContent")
    assert monitoring.tools is None
    assert monitoring.system_instruction == _body("req_monitoring.json")["request"]["systemInstruction"]
    # Passed through as sent, not coerced; the counts still read 0.
    odd = parse_request(json.dumps({"request": {"systemInstruction": 7, "tools": "x"}}).encode(), "m")
    assert (odd.system_instruction, odd.tools, odd.system_chars, odd.tools_count) == (7, "x", 0, 0)
    bare = parse_request(b'{"request": {}}', "m")
    assert (bare.system_instruction, bare.tools) == (None, None)


def test_parse_json_value_decodes_any_json():
    models = {"models": {"gemini-x": {"displayName": "Gemini X", "quotaInfo": {"remainingFraction": 1}}}}
    assert parse_json_value(json.dumps(models).encode()) == models
    assert parse_json_value(b"[1, 2]") == [1, 2]
    assert parse_json_value("\ufeff{\"a\": \"한\"}".encode()) == {"a": "한"}
    assert parse_json_value(bytearray(b' {"a": 1}\n')) == {"a": 1}


@pytest.mark.parametrize("body", [b"", b"  \n", None, b"not json", b'{"a":', b"[" * 100000, 12, object()])
def test_parse_json_value_failure_is_none(body):
    assert parse_json_value(body) is None


def test_new_public_functions_never_raise_on_garbage():
    nested = b"[" * 100000
    for value in [None, 0, 1.5, "s", b"\xff", nested, [], {}, [None], {"metadata": None}, object()]:
        content_hash(value)
        parse_json_value(value)
        acc = SSEAccumulator(clock=lambda: 1.0)
        acc.feed(value)
        acc.feed(b"data: " + nested + b"\n")
        acc.feed(b'data: {"metadata": 1, "response": {"candidates": [{"x": {"y": 1}, "content": 3}]}}\n')
        assert isinstance(acc.finish(), AgyResponse)
