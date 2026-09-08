"""tests/misc: the desktop bridge now consults the capability index (name preferences)."""

from __future__ import annotations

import pytest
from tests._capability_index import bind_capability_index


@pytest.fixture(autouse=True)
def _capability_index(tmp_path, monkeypatch):
    engine, _factory = bind_capability_index(tmp_path, monkeypatch)
    yield
    engine.dispose()
