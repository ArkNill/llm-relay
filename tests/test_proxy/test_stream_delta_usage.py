"""Streaming message_delta usage merge (relay undercount of server-side web_search, 2026-10-19)."""

from llm_relay.proxy.proxy import _apply_delta_usage


def _acc(**kw):
    base = {"input_tokens": 0, "output_tokens": 0, "cache_creation": 0, "cache_read": 0,
            "ephemeral_1h_tokens": 0, "ephemeral_5m_tokens": 0}
    base.update(kw)
    return base


def test_plain_delta_updates_output_only():
    acc = _acc(input_tokens=10, cache_read=500, output_tokens=1)
    _apply_delta_usage(acc, {"output_tokens": 42})
    assert acc["output_tokens"] == 42
    assert acc["input_tokens"] == 10
    assert acc["cache_read"] == 500
    assert "web_search_requests" not in acc


def test_server_tool_delta_carries_final_totals():
    acc = _acc(input_tokens=1200, cache_read=1000, cache_creation=200, ephemeral_1h_tokens=200)
    _apply_delta_usage(acc, {
        "input_tokens": 48000, "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 900,
        "cache_creation": {"ephemeral_1h_input_tokens": 900, "ephemeral_5m_input_tokens": 0},
        "output_tokens": 7000, "server_tool_use": {"web_search_requests": 4},
    })
    assert acc["input_tokens"] == 48000
    assert acc["cache_read"] == 5000
    assert acc["cache_creation"] == 900
    assert acc["ephemeral_1h_tokens"] == 900
    assert acc["output_tokens"] == 7000
    assert acc["web_search_requests"] == 4


def test_counts_never_decrease():
    acc = _acc(input_tokens=100, cache_read=50, ephemeral_5m_tokens=30)
    _apply_delta_usage(acc, {"input_tokens": 0, "cache_read_input_tokens": None, "output_tokens": 3,
                             "cache_creation": {"ephemeral_5m_input_tokens": 10}})
    assert acc["input_tokens"] == 100
    assert acc["cache_read"] == 50
    assert acc["ephemeral_5m_tokens"] == 30
    assert acc["output_tokens"] == 3


def test_non_dict_usage_is_ignored():
    acc = _acc(output_tokens=5)
    _apply_delta_usage(acc, None)
    assert acc["output_tokens"] == 5
