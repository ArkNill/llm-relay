"""Composition caches keep aggregates only and are bounded by entry count and idle time.

2026-09-29: the per-session composition state carried the full message list of every
session ever analyzed (never read; 45 MB for 49 sessions), the per-turn path
re-classified the whole context at every turn (3.75 s on the event loop for one
692-turn session), and nothing evicted an entry -- clear_cache() has no production
caller, so the caches grew for the lifetime of the relay process.
"""

import json

import pytest
from starlette.testclient import TestClient
from test_proxy.test_composition_cold_start import (
    SID,
    _all_rows,
    _insert,
    _make_db,
    _read,
    _result,
    _seed_session,
    _text,
)

from llm_relay.proxy import composition
from llm_relay.proxy.composition import (
    _BoundedCache,
    _CompositionState,
    _fold_delta_into_composition_state,
    _fold_delta_into_per_turn_state,
    _PerTurnState,
    _reconstruct_and_classify,
    _reconstruct_per_turn,
    analyze_file_composition,
    analyze_session_composition,
    analyze_session_composition_per_turn,
    cache_stats,
    clear_cache,
)

CACHES = (composition._cache, composition._per_turn_cache, composition._file_cache)


@pytest.fixture(autouse=True)
def _fresh_caches():
    for cache in CACHES:
        cache.clear()
        cache.evicted = 0
    yield
    for cache in CACHES:
        cache.clear()
        cache.evicted = 0


class _FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = _FakeClock()
    monkeypatch.setattr(composition, "_clock", fake)
    return fake


def _row(turn, mode, messages):
    return {
        "turn_number": turn,
        "storage_mode": mode,
        "request_messages": None if messages is None else json.dumps(messages),
    }


def _per_turn_rows():
    """full, delta, compaction, delta, longer full (new conversation), unusable row, delta."""
    return [
        _row(1, "full", [_text("user", "u1 " * 10), _text("assistant", "a1 " * 20)]),
        _row(2, "delta", [_read("/a.py"), _result("A" * 300)]),
        _row(3, "full", [_text("user", "summary " * 30)]),  # 4 -> 1 message: compaction
        _row(4, "delta", [_read("/b.py"), _result("B" * 500), {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "hmm " * 50}, {"type": "text", "text": "done"}]}]),
        _row(5, "full", [_text("user", "x")] * 6),  # more messages than before: not a compaction
        _row(6, "delta", None),
        _row(7, "delta", [_text("user", "u7"), _text("assistant", "a7 " * 40)]),
    ]


# ── aggregates only ──


def test_composition_state_holds_aggregates_not_messages():
    conn = _make_db()
    _seed_session(conn)
    state = _CompositionState()
    _fold_delta_into_composition_state(state, _all_rows(conn))

    assert not hasattr(state, "accumulated")
    totals, total, _dupes, calls, tool_bytes, thinking = _reconstruct_and_classify(_all_rows(conn))
    assert state.totals == totals
    assert sum(state.totals.values()) == total
    assert state.tool_call_counts == calls
    assert state.tool_byte_totals == tool_bytes
    assert state.thinking_count == thinking


def test_per_turn_fold_matches_reclassifying_every_turn():
    rows = _per_turn_rows()
    state = _PerTurnState()
    _fold_delta_into_per_turn_state(state, rows)

    assert state.turn_results == _reconstruct_per_turn(rows)
    assert [r["turn"] for r in state.turn_results] == [1, 2, 3, 4, 5, 7]
    assert [r["compacted"] for r in state.turn_results] == [False, False, True, False, False, False]
    assert state.msg_count == 8
    assert state.max_turn_processed == 7
    assert not hasattr(state, "accumulated")


def test_per_turn_fold_in_steps_equals_one_pass():
    rows = _per_turn_rows()
    one_pass = _PerTurnState()
    _fold_delta_into_per_turn_state(one_pass, rows)
    stepwise = _PerTurnState()
    _fold_delta_into_per_turn_state(stepwise, rows[:3])
    _fold_delta_into_per_turn_state(stepwise, rows[3:])
    assert stepwise == one_pass


# ── bounds ──


def test_least_recently_used_entry_is_evicted_first(monkeypatch, clock):
    monkeypatch.setattr(composition, "_CACHE_MAX", 3)
    cache = _BoundedCache()
    for key in ("a", "b", "c"):
        cache.put(key, key.upper())
    assert cache.get("a") == "A"  # refresh a; b is now the least recently used
    cache.put("d", "D")

    assert "b" not in cache
    assert all(key in cache for key in ("a", "c", "d"))
    assert len(cache) == 3
    assert cache.evicted == 1


def test_idle_entries_expire(monkeypatch, clock):
    monkeypatch.setattr(composition, "_CACHE_TTL_S", 100.0)
    cache = _BoundedCache()
    cache.put("a", 1)
    clock.now += 50
    cache.put("b", 2)
    clock.now += 70  # a idle 120 s, b idle 70 s

    assert cache.get("b") == 2  # the access drops a and refreshes b
    assert "a" not in cache
    assert cache.evicted == 1

    clock.now += 101
    cache.sweep()
    assert len(cache) == 0
    assert cache.evicted == 2


def test_zero_disables_both_bounds(monkeypatch, clock):
    monkeypatch.setattr(composition, "_CACHE_MAX", 0)
    monkeypatch.setattr(composition, "_CACHE_TTL_S", 0.0)
    cache = _BoundedCache()
    for i in range(200):
        cache.put(str(i), i)
    clock.now += 10 ** 9
    cache.sweep()
    assert len(cache) == 200
    assert cache.evicted == 0


def test_evicted_session_reloads_to_the_same_result(monkeypatch):
    monkeypatch.setattr(composition, "_CACHE_MAX", 1)
    conn = _make_db()
    _seed_session(conn)
    _insert(conn, 1, "full", [_text("user", "other"), _text("assistant", "reply")], session_id="other")

    first = analyze_session_composition(conn, SID)
    assert analyze_session_composition(conn, "other") is not None
    assert SID not in composition._cache
    assert composition._cache.evicted == 1
    assert analyze_session_composition(conn, SID) == first  # cold reload from the newest full row


def test_evicted_per_turn_session_reloads_to_the_same_result(monkeypatch):
    monkeypatch.setattr(composition, "_CACHE_MAX", 1)
    conn = _make_db()
    _seed_session(conn)
    _insert(conn, 1, "full", [_text("user", "other")], session_id="other")

    first = analyze_session_composition_per_turn(conn, SID)
    assert analyze_session_composition_per_turn(conn, "other") is not None
    assert SID not in composition._per_turn_cache
    assert analyze_session_composition_per_turn(conn, SID) == first


def test_file_cache_is_bounded(monkeypatch, tmp_path):
    monkeypatch.setattr(composition, "_CACHE_MAX", 2)
    paths = []
    for i in range(3):
        path = tmp_path / "session-{}.json".format(i)
        path.write_text(json.dumps({"messages": [{"type": "user", "content": "hello {}".format(i)}]}))
        paths.append(str(path))
        assert analyze_file_composition(str(path), "gemini-cli") is not None

    assert len(composition._file_cache) == 2
    assert paths[0] not in composition._file_cache


# ── clear_cache / stats ──


def test_clear_cache_for_one_session_keeps_the_rest(tmp_path):
    conn = _make_db()
    _seed_session(conn)
    _insert(conn, 1, "full", [_text("user", "other")], session_id="other")
    analyze_session_composition(conn, SID)
    analyze_session_composition(conn, "other")
    analyze_session_composition_per_turn(conn, SID)
    path = tmp_path / "session.json"
    path.write_text(json.dumps({"messages": [{"type": "user", "content": "hi"}]}))
    analyze_file_composition(str(path), "gemini-cli")

    clear_cache(SID)
    assert SID not in composition._cache
    assert SID not in composition._per_turn_cache
    assert "other" in composition._cache
    assert len(composition._file_cache) == 1  # path-keyed: only a full clear empties it

    clear_cache()
    assert len(composition._cache) == 0
    assert len(composition._per_turn_cache) == 0
    assert len(composition._file_cache) == 0


def test_cache_stats_reported_by_health_endpoint():
    conn = _make_db()
    _seed_session(conn)
    analyze_session_composition(conn, SID)
    analyze_session_composition_per_turn(conn, SID)

    stats = cache_stats()
    assert stats == {
        "sessions": 1,
        "per_turn_sessions": 1,
        "per_turn_turns": 5,
        "files": 0,
        "evicted": 0,
        "max": composition._CACHE_MAX,
        "ttl_s": composition._CACHE_TTL_S,
    }

    from llm_relay.proxy.proxy import app

    body = TestClient(app).get("/_health").json()
    assert body["composition_cache"] == stats
