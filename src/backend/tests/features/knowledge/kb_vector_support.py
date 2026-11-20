"""Shared fakes for the private-KB Milvus tests: a recording client, a pymilvus stub, row builders."""
from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

from core.infra.time import utc_now
from features.knowledge import kb_vector_state

LEGACY = kb_vector_state.LEGACY_COLLECTION_NAME
CURRENT = kb_vector_state.CURRENT_COLLECTION_NAME


class FakeSchema:
    def __init__(self):
        self.fields: dict[str, dict] = {}
        self.functions: list = []

    def add_field(self, name, dtype, **kwargs):
        self.fields[name] = kwargs

    def add_function(self, function):
        self.functions.append(function)


class FakeIndexParams:
    def __init__(self):
        self.indexes: dict[str, dict] = {}

    def add_index(self, field_name, **kwargs):
        self.indexes[field_name] = kwargs


class FakeClient:
    """Records every call; ``collections`` are the names that exist."""

    def __init__(self, collections=(), dim=8):
        self.collections = set(collections)
        self.dim = dim
        self.calls: list[tuple] = []
        self.created: dict = {}

    def has_collection(self, name, **_):
        self.calls.append(("has_collection", name))
        return name in self.collections

    def describe_collection(self, name, **_):
        self.calls.append(("describe_collection", name))
        return {"fields": [{"name": "dense_embedding", "params": {"dim": self.dim}}]}

    def create_schema(self, **_):
        return FakeSchema()

    def prepare_index_params(self):
        return FakeIndexParams()

    def create_collection(self, collection_name, schema, index_params, **kwargs):
        self.collections.add(collection_name)
        self.created[collection_name] = (schema, index_params, kwargs)

    def delete(self, **kwargs):
        self.calls.append(("delete", kwargs))

    def insert(self, **kwargs):
        self.calls.append(("insert", kwargs))

    def upsert(self, **kwargs):
        self.calls.append(("upsert", kwargs))

    def made(self, method: str) -> list:
        return [c[1] for c in self.calls if c[0] == method]


def install_fake_pymilvus(monkeypatch) -> None:
    mod = ModuleType("pymilvus")
    mod.DataType = SimpleNamespace(
        VARCHAR="VARCHAR", INT64="INT64", FLOAT_VECTOR="FLOAT_VECTOR",
        SPARSE_FLOAT_VECTOR="SPARSE_FLOAT_VECTOR",
    )
    mod.FunctionType = SimpleNamespace(BM25="BM25")
    mod.Function = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "pymilvus", mod)


def kb_document(document_id, *, kb_id="kb_live", deleted=False, status="completed"):
    from features.knowledge.models import KBDocument

    return KBDocument(
        document_id=document_id, kb_id=kb_id, title=document_id, filename=f"{document_id}.txt",
        size_bytes=1, mime_type="text/plain", storage_key=document_id,
        indexing_status=status, deleted_at=utc_now() if deleted else None,
    )
