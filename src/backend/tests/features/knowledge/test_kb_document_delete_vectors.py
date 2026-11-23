"""Deleting a KB document or space must take its vectors out of retrieval."""
from __future__ import annotations

from features.knowledge import vector_indexing
from tests.features.knowledge.kb_vector_support import kb_document


def test_vector_purge_failure_does_not_fail_the_delete(monkeypatch):
    from features.knowledge.service import KBService

    def _boom(*_args):
        raise ConnectionError("milvus down")

    monkeypatch.setattr(vector_indexing, "delete_document_vectors", _boom)

    KBService._purge_vectors("delete_document_vectors", "doc_abc", "kb_1")


def test_hits_from_deleted_or_unknown_documents_are_dropped(memory_sessions):
    from extensions.mcp_servers.retrieve_dataset_content_mcp.local_impl import (
        _drop_deleted_document_hits,
    )

    with memory_sessions() as db:
        db.add_all([kb_document("doc_live"), kb_document("doc_gone", deleted=True)])
        db.commit()
    hits = [
        {"chunk_id": "a", "document_id": "doc_live"},
        {"chunk_id": "b", "document_id": "doc_gone"},
        {"chunk_id": "c", "document_id": "doc_never_existed"},
        {"chunk_id": "d", "document_id": None},
    ]

    kept = _drop_deleted_document_hits(hits)

    assert [h["chunk_id"] for h in kept] == ["a", "d"]
