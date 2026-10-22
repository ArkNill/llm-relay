"""Composition analysis reads a session's turn rows in keyset batches, not with one fetchall.

2026-09-29: one fetchall of a 692-turn session added 340 MB to the relay during a
composition analysis (a 2026-10-14 session reached 1,127 turns). Folding is incremental,
so reading and folding _COMPOSITION_BATCH_TURNS rows at a time must give exactly the
same result as reading everything at once (batch size 0).
"""

import pytest
from test_proxy.test_composition_cold_start import SID, _insert, _make_db, _read, _result, _text

from llm_relay.proxy import composition
from llm_relay.proxy.composition import (
    _iter_turn_batches,
    analyze_session_composition,
    analyze_session_composition_per_turn,
)

CACHES = (composition._cache, composition._per_turn_cache)


@pytest.fixture(autouse=True)
def _fresh_caches():
    for cache in CACHES:
        cache.clear()
    yield
    for cache in CACHES:
        cache.clear()


def _turn_messages(turn, mode):
    """Distinct, varied content per turn: repeated reads, tool results, thinking, plain text."""
    path = f"/src/f{turn % 7}.py"
    if mode == "full":
        return [_text("user", f"summary {turn} " * 20), _read(path), _result("S" * (100 + turn)),
                _text("assistant", f"resumed {turn}")]
    msgs = [_read(path), _result(chr(65 + turn % 26) * (50 + turn * 3))]
    if turn % 5 == 0:
        msgs.append({"role": "assistant", "content": [
            {"type": "thinking", "thinking": "think " * turn}, {"type": "text", "text": f"ok {turn}"}]})
    else:
        msgs.append(_text("assistant", f"a{turn} " * (turn % 9 + 1)))
    return msgs


def _seed(conn, n, full_at=(1,), null_at=(), session_id=SID, first_turn=1):
    for turn in range(first_turn, first_turn + n):
        mode = "full" if turn in full_at else "delta"
        messages = None if turn in null_at else _turn_messages(turn, mode)
        _insert(conn, turn, mode, messages, session_id=session_id)


def _analyze_cold(conn, monkeypatch, batch_size, session_id=SID):
    monkeypatch.setattr(composition, "_COMPOSITION_BATCH_TURNS", batch_size)
    for cache in CACHES:
        cache.clear()
    return (analyze_session_composition(conn, session_id),
            analyze_session_composition_per_turn(conn, session_id))


class _FoldSpy:
    """Wraps both fold functions and records the rows of every call."""

    def __init__(self, monkeypatch, fail_on_call=None):
        self.calls = []  # (kind, [row ids])
        self._fail_on_call = fail_on_call
        for kind, name in (("comp", "_fold_delta_into_composition_state"),
                           ("per_turn", "_fold_delta_into_per_turn_state")):
            monkeypatch.setattr(composition, name, self._wrap(kind, getattr(composition, name)))

    def _wrap(self, kind, original):
        def spy(state, new_turns):
            self.calls.append((kind, [row["id"] for row in new_turns]))
            if self._fail_on_call is not None and self._fail_on_call == (kind, len(self.of(kind))):
                raise ValueError("injected fold failure")
            return original(state, new_turns)
        return spy

    def of(self, kind):
        return [ids for k, ids in self.calls if k == kind]

    def sizes(self, kind):
        return [len(ids) for ids in self.of(kind)]

    def ids(self, kind):
        return [i for ids in self.of(kind) for i in ids]


def _full_points(n):
    """Turn 1 is a full row, plus one or two compactions inside the session with deltas after the last."""
    if n < 3:
        return (1,)
    if n < 100:
        return (1, n // 2)
    return (1, n // 3, 2 * n // 3)


# ── 1. Equivalence ──


@pytest.mark.parametrize("n", [1, 63, 64, 65, 130, 200])
def test_batched_results_equal_single_fetch(monkeypatch, n):
    conn = _make_db()
    null_at = (n - 3,) if n > 10 else ()
    _seed(conn, n, full_at=_full_points(n), null_at=null_at)

    expected = _analyze_cold(conn, monkeypatch, 0)
    spy = _FoldSpy(monkeypatch)
    actual = _analyze_cold(conn, monkeypatch, 64)

    assert expected[0] is not None and expected[1] is not None
    assert actual == expected
    assert actual[1]["total_turns"] == n - len(null_at)  # a NULL payload row yields no per-turn entry
    # The per-turn path reads every row, so it really was split for n > 64.
    assert spy.sizes("per_turn") == [64] * (n // 64) + ([n % 64] if n % 64 else [])


def test_equivalence_with_odd_batch_size(monkeypatch):
    conn = _make_db()
    _seed(conn, 130, full_at=(1, 40, 101))
    assert _analyze_cold(conn, monkeypatch, 7) == _analyze_cold(conn, monkeypatch, 0)


# ── 2. Duplicate turn_number rows (pre-2026-09-28 restarts) ──


def test_duplicate_turn_numbers_across_batch_boundary_are_folded_once(monkeypatch):
    conn = _make_db()
    _seed(conn, 63, full_at=())                                   # ids 1..63, turns 1..63
    _insert(conn, 64, "delta", [_text("user", "first copy of 64")])   # id 64 -- last row of batch 1
    _insert(conn, 64, "delta", [_text("user", "second copy of 64")])  # id 65 -- first row of batch 2
    _seed(conn, 20, full_at=(), first_turn=65)                    # ids 66..85
    seeded = 85

    expected = _analyze_cold(conn, monkeypatch, 0)
    spy = _FoldSpy(monkeypatch)
    actual = _analyze_cold(conn, monkeypatch, 64)

    assert actual == expected
    for kind in ("comp", "per_turn"):
        assert spy.ids(kind) == list(range(1, seeded + 1))
        assert spy.sizes(kind) == [64, 21]


def test_many_rows_sharing_one_turn_number_span_several_batches(monkeypatch):
    conn = _make_db()
    for i in range(10):
        _insert(conn, 1, "delta", [_text("user", f"restart {i}")])
    for i in range(10):
        _insert(conn, 2, "delta", [_text("user", f"restart {i}")])

    expected = _analyze_cold(conn, monkeypatch, 0)
    spy = _FoldSpy(monkeypatch)
    assert _analyze_cold(conn, monkeypatch, 3) == expected
    assert spy.ids("per_turn") == list(range(1, 21))
    assert spy.sizes("per_turn") == [3] * 6 + [2]


def test_iter_turn_batches_keyset(monkeypatch):
    conn = _make_db()
    _seed(conn, 128, full_at=())

    assert [len(b) for b in _iter_turn_batches(conn, SID, 0, 64)] == [64, 64]
    assert [len(b) for b in _iter_turn_batches(conn, SID, 0, 0)] == [128]
    # Rows with turn_number == start_after_turn are excluded, as before.
    batches = list(_iter_turn_batches(conn, SID, 100, 10))
    assert [b[0]["turn_number"] for b in batches] == [101, 111, 121]
    assert sorted(batches[0][0]) == ["id", "request_messages", "storage_mode", "turn_number"]
    assert list(_iter_turn_batches(conn, SID, 128, 64)) == []
    assert list(_iter_turn_batches(conn, "missing", 0, 0)) == []


# ── 3. Rows resident per fold ──


def test_fold_never_receives_more_than_batch_size_rows(monkeypatch):
    conn = _make_db()
    _seed(conn, 200, full_at=(1,))  # only turn 1 is full, so the cold composition path reads every row too
    spy = _FoldSpy(monkeypatch)
    _analyze_cold(conn, monkeypatch, 64)

    for kind in ("comp", "per_turn"):
        assert max(spy.sizes(kind)) == 64
        assert sum(spy.sizes(kind)) == 200

    spy.calls.clear()
    composition._full_rebuild_composition_state(conn, SID)
    composition._full_rebuild_per_turn_state(conn, SID)
    for kind in ("comp", "per_turn"):
        assert spy.sizes(kind) == [64, 64, 64, 8]


# ── 4. Incremental cache ──


def test_warm_cache_folds_only_new_rows_in_batches(monkeypatch):
    conn = _make_db()
    _seed(conn, 130, full_at=(1, 60))
    monkeypatch.setattr(composition, "_COMPOSITION_BATCH_TURNS", 64)
    analyze_session_composition(conn, SID)
    analyze_session_composition_per_turn(conn, SID)
    assert composition._cache[SID].max_turn_processed == 130
    assert composition._per_turn_cache[SID].max_turn_processed == 130

    _seed(conn, 70, full_at=(), first_turn=131)
    spy = _FoldSpy(monkeypatch)
    warm = (analyze_session_composition(conn, SID), analyze_session_composition_per_turn(conn, SID))

    for kind in ("comp", "per_turn"):
        assert spy.sizes(kind) == [64, 6]
        assert spy.ids(kind) == list(range(131, 201))
    assert composition._cache[SID].max_turn_processed == 200
    assert composition._per_turn_cache[SID].max_turn_processed == 200
    assert warm == _analyze_cold(conn, monkeypatch, 0)


# ── 5. Cold start from the newest full row ──


def test_cold_composition_starts_at_newest_full_row(monkeypatch):
    conn = _make_db()
    _seed(conn, 200, full_at=(1, 70, 133))
    spy = _FoldSpy(monkeypatch)
    _analyze_cold(conn, monkeypatch, 64)

    assert spy.ids("comp") == list(range(133, 201))
    assert spy.sizes("comp") == [64, 4]
    assert spy.ids("per_turn") == list(range(1, 201))  # per-turn needs every turn


# ── 6. Fold failure falls back to a batched rebuild ──


@pytest.mark.parametrize("kind", ["comp", "per_turn"])
def test_fold_failure_in_a_later_batch_rebuilds(monkeypatch, kind):
    conn = _make_db()
    _seed(conn, 150, full_at=(1,))
    expected = _analyze_cold(conn, monkeypatch, 0)

    spy = _FoldSpy(monkeypatch, fail_on_call=(kind, 2))
    actual = _analyze_cold(conn, monkeypatch, 64)

    assert actual == expected
    # Batch 1, the failing batch 2, then the rebuild from scratch in batches of 64.
    assert spy.sizes(kind) == [64, 64, 64, 64, 22]
    cache = composition._cache if kind == "comp" else composition._per_turn_cache
    assert cache[SID].max_turn_processed == 150


def test_fold_failure_with_rows_gone_drops_the_cache_entry(monkeypatch):
    conn = _make_db()
    _seed(conn, 10, full_at=(1,))
    def broken_fold(state, rows):
        raise ValueError("boom")

    monkeypatch.setattr(composition, "_COMPOSITION_BATCH_TURNS", 64)
    monkeypatch.setattr(composition, "_fold_delta_into_composition_state", broken_fold)
    monkeypatch.setattr(composition, "_full_rebuild_composition_state", lambda conn, sid: None)
    assert analyze_session_composition(conn, SID) is None
    assert SID not in composition._cache


# ── 7. Empty session ──


@pytest.mark.parametrize("batch_size", [0, 64])
def test_empty_session_returns_none(monkeypatch, batch_size):
    conn = _make_db()
    _seed(conn, 5, session_id="other")
    assert _analyze_cold(conn, monkeypatch, batch_size) == (None, None)
    assert composition._full_rebuild_composition_state(conn, SID) is None
    assert composition._full_rebuild_per_turn_state(conn, SID) is None
    assert SID not in composition._cache and SID not in composition._per_turn_cache
