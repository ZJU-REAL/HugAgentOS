from __future__ import annotations

import sys
from types import ModuleType

import pytest


def _install_fake_pymilvus(monkeypatch) -> None:
    fake_pymilvus = ModuleType("pymilvus")
    fake_pymilvus.DataType = object
    monkeypatch.setitem(sys.modules, "pymilvus", fake_pymilvus)


class _ExistingCollectionClient:
    def __init__(
        self,
        existing_dim: int | None = None,
        describe_error: Exception | None = None,
    ):
        self.existing_dim = existing_dim
        self.describe_error = describe_error
        self.drop_called = False

    def has_collection(self, *_args, **_kwargs) -> bool:
        return True

    def describe_collection(self, *_args, **_kwargs) -> dict:
        if self.describe_error is not None:
            raise self.describe_error
        params = {} if self.existing_dim is None else {"dim": self.existing_dim}
        return {"fields": [{"name": "dense_embedding", "params": params}]}

    def drop_collection(self, *_args, **_kwargs) -> None:
        self.drop_called = True

    def create_schema(self, *_args, **_kwargs):
        raise AssertionError("an existing collection must never be recreated implicitly")


def test_dimension_mismatch_preserves_existing_collection(monkeypatch):
    from core.kb import kb_vector

    _install_fake_pymilvus(monkeypatch)
    client = _ExistingCollectionClient(existing_dim=2048)
    monkeypatch.setattr(kb_vector, "_get_client", lambda: client)
    monkeypatch.setattr(kb_vector, "detect_embed_dim", lambda: 1024)
    monkeypatch.setattr(kb_vector, "_VERIFIED_COLLECTION_DIM", None)

    with pytest.raises(RuntimeError, match="维度不匹配"):
        kb_vector.get_or_create_collection(timeout=3.0)

    assert client.drop_called is False
    assert kb_vector._VERIFIED_COLLECTION_DIM is None


def test_dimension_probe_failure_preserves_existing_collection(monkeypatch):
    from core.kb import kb_vector

    _install_fake_pymilvus(monkeypatch)
    client = _ExistingCollectionClient(describe_error=ConnectionError("milvus unavailable"))
    monkeypatch.setattr(kb_vector, "_get_client", lambda: client)
    monkeypatch.setattr(kb_vector, "detect_embed_dim", lambda: 2048)
    monkeypatch.setattr(kb_vector, "_VERIFIED_COLLECTION_DIM", None)

    with pytest.raises(RuntimeError, match="无法确认现有知识库集合的向量维度"):
        kb_vector.get_or_create_collection(timeout=3.0)

    assert client.drop_called is False
    assert kb_vector._VERIFIED_COLLECTION_DIM is None


def test_missing_dimension_metadata_preserves_existing_collection(monkeypatch):
    from core.kb import kb_vector

    _install_fake_pymilvus(monkeypatch)
    client = _ExistingCollectionClient(existing_dim=None)
    monkeypatch.setattr(kb_vector, "_get_client", lambda: client)
    monkeypatch.setattr(kb_vector, "detect_embed_dim", lambda: 2048)
    monkeypatch.setattr(kb_vector, "_VERIFIED_COLLECTION_DIM", None)

    with pytest.raises(RuntimeError, match="dim 参数缺失"):
        kb_vector.get_or_create_collection(timeout=3.0)

    assert client.drop_called is False
    assert kb_vector._VERIFIED_COLLECTION_DIM is None


def test_matching_dimension_marks_existing_collection_verified(monkeypatch):
    from core.kb import kb_vector

    _install_fake_pymilvus(monkeypatch)
    client = _ExistingCollectionClient(existing_dim=2048)
    monkeypatch.setattr(kb_vector, "_get_client", lambda: client)
    monkeypatch.setattr(kb_vector, "detect_embed_dim", lambda: 2048)
    monkeypatch.setattr(kb_vector, "_VERIFIED_COLLECTION_DIM", None)

    kb_vector.get_or_create_collection(timeout=3.0)

    assert client.drop_called is False
    assert kb_vector._VERIFIED_COLLECTION_DIM == 2048
