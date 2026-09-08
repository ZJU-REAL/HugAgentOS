"""Shared fixtures: an isolated capability root and an isolated SQLite index."""

from __future__ import annotations

import pytest
from tests._capability_index import bind_capability_index


@pytest.fixture
def caps_root(tmp_path, monkeypatch):
    root = tmp_path / "caps"
    monkeypatch.setenv("HUGAGENT_CAPS_ROOT", str(root))
    return root


@pytest.fixture
def index_db(tmp_path, monkeypatch):
    engine, factory = bind_capability_index(tmp_path, monkeypatch)
    yield factory
    engine.dispose()
