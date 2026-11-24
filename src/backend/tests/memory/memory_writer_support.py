"""Fixtures shared by the memory writer tests."""
import pytest

from core.memory.extractors import writers as W


@pytest.fixture(autouse=True)
def _breaker_closed(monkeypatch):
    monkeypatch.setattr(W.milvus_breaker, "is_open", lambda: False)
    monkeypatch.setattr(W.milvus_breaker, "record_success", lambda: None)
    monkeypatch.setattr(W.milvus_breaker, "record_failure", lambda: None)
