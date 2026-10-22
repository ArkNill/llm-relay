"""Logging setup of the proxy module (2026-09-29): the relay's loggers get an output under uvicorn.

pytest keeps its capture handlers on the root logger, so the install branch is exercised by
emptying root's handler list in place around the call instead of relying on import time.
"""

from __future__ import annotations

import logging

import pytest

from llm_relay.proxy import proxy


def _setup_with_bare_root():
    """Run _setup_logging() the way uvicorn sees it: a root logger without handlers."""
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers[:] = []
    try:
        proxy._setup_logging()
    finally:
        root.handlers[:] = saved


@pytest.fixture
def relay_logger(monkeypatch):
    logger = logging.getLogger("llm-relay")
    saved = logger.handlers[:]
    logger.handlers[:] = []
    monkeypatch.delenv("LLM_RELAY_LOG_LEVEL", raising=False)
    yield logger
    logger.handlers[:] = saved
    logger.setLevel(logging.NOTSET)  # back to inheriting from root


def test_installs_one_stderr_handler_and_keeps_warning_by_default(relay_logger):
    _setup_with_bare_root()
    _setup_with_bare_root()  # idempotent

    handlers = [h for h in relay_logger.handlers if isinstance(h, logging.StreamHandler)]
    assert len(handlers) == 1
    assert handlers[0].formatter._fmt == "%(levelname)s:     %(message)s"
    assert relay_logger.getEffectiveLevel() == logging.WARNING  # unchanged: diagnostics stay quiet
    assert logging.getLogger("llm-relay.access").getEffectiveLevel() == logging.INFO


def test_leaves_logging_alone_when_something_configured_root(relay_logger):
    assert logging.getLogger().handlers  # pytest's capture handlers stand in for the CLI's basicConfig
    proxy._setup_logging()
    assert relay_logger.handlers == []


def test_log_level_knob_sets_the_main_logger(relay_logger, monkeypatch):
    monkeypatch.setenv("LLM_RELAY_LOG_LEVEL", "info")
    _setup_with_bare_root()
    assert relay_logger.level == logging.INFO

    monkeypatch.setenv("LLM_RELAY_LOG_LEVEL", "not-a-level")
    _setup_with_bare_root()
    assert relay_logger.level == logging.WARNING
