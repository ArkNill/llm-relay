"""Native env-file loading (2026-09-29): .env.local overrides .env.public, explicit env wins."""

from __future__ import annotations

import os

import pytest

from llm_relay.proxy import db, proxy

KEYS = ("R92_BOTH", "R92_PUBLIC_ONLY", "R92_LOCAL_ONLY", "R92_EXPLICIT")
LOADERS = pytest.mark.parametrize("loader", [proxy._load_env_files, db._load_env_files], ids=["proxy", "db"])


@pytest.fixture(autouse=True)
def _clean_keys(monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    yield
    for key in KEYS:
        os.environ.pop(key, None)


@LOADERS
def test_local_overrides_public_and_explicit_env_wins(tmp_path, monkeypatch, loader):
    (tmp_path / ".env.public").write_text(
        "# comment\nR92_BOTH=public\nR92_PUBLIC_ONLY=public\nR92_EXPLICIT=public\nnot-a-pair\n",
        encoding="utf-8",
    )
    (tmp_path / ".env.local").write_text(
        "R92_BOTH=local\nR92_LOCAL_ONLY=local\nR92_EXPLICIT=local\n", encoding="utf-8",
    )
    monkeypatch.setenv("R92_EXPLICIT", "env")

    loader(tmp_path)

    assert os.environ["R92_BOTH"] == "local"
    assert os.environ["R92_PUBLIC_ONLY"] == "public"
    assert os.environ["R92_LOCAL_ONLY"] == "local"
    assert os.environ["R92_EXPLICIT"] == "env"


@LOADERS
def test_public_alone_is_loaded_and_missing_files_are_a_no_op(tmp_path, loader):
    loader(tmp_path)
    assert "R92_PUBLIC_ONLY" not in os.environ

    (tmp_path / ".env.public").write_text("R92_PUBLIC_ONLY=public\n", encoding="utf-8")
    loader(tmp_path)
    assert os.environ["R92_PUBLIC_ONLY"] == "public"
