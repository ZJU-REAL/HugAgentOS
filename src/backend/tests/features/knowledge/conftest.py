from __future__ import annotations

import pytest

from features.knowledge import kb_vector_state


@pytest.fixture(autouse=True)
def fresh_vector_collection_state(monkeypatch):
    """The resolved collection is cached per process; start every test unresolved."""
    monkeypatch.setattr(kb_vector_state, "COLLECTION_NAME", kb_vector_state.LEGACY_COLLECTION_NAME)
    monkeypatch.setattr(kb_vector_state, "ACTIVE_INDEX", None)
    monkeypatch.setattr(kb_vector_state, "COLLECTION_EXISTS", False)
    monkeypatch.setattr(kb_vector_state, "VERIFIED_COLLECTION_DIM", None)
