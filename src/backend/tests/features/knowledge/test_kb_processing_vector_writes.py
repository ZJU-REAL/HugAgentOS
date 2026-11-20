"""Document indexing writes its vectors in few round trips and never upserts fresh rows."""
from __future__ import annotations

import pytest

from core.models import embedding as vector_embedding
from features.knowledge import (
    enrichment,
    kb_assets,
    kb_parser,
    processing,
    vector_client,
    vector_indexing,
)
from features.knowledge.parser_types import ChildChunk, ParentChunk


@pytest.fixture
def run(monkeypatch, memory_sessions):
    parents = [
        ParentChunk(f"p{i}", f"parent {i}", [ChildChunk(f"p{i}_{j}", f"child {i}.{j}", j) for j in range(2)])
        for i in range(3)
    ]
    monkeypatch.setattr(kb_parser, "parse_and_chunk_rich", lambda *a, **k: (parents, []))
    monkeypatch.setattr(kb_assets, "multimodal_indexing_enabled", lambda cfg: False)
    monkeypatch.setattr(processing, "persist_document_assets", lambda **k: [])
    monkeypatch.setattr(vector_client, "get_or_create_collection", lambda **k: None)
    monkeypatch.setattr(
        enrichment, "generate_questions_llm", lambda content, n: [f"{content}?{k}" for k in range(n)]
    )

    log: dict = {"embed": [], "deleted": [], "inserted": [], "status": []}

    def embed(texts):
        log["embed"].append(list(texts))
        return [[float(len(t))] for t in texts]

    monkeypatch.setattr(vector_embedding, "embed_batch", embed)
    monkeypatch.setattr(
        vector_indexing, "delete_document_vectors", lambda d, kb_id=None: log["deleted"].append((d, kb_id))
    )
    monkeypatch.setattr(vector_indexing, "insert_rows", lambda rows: log["inserted"].append(list(rows)))
    monkeypatch.setattr(
        vector_indexing, "upsert_question_rows",
        lambda *a, **k: pytest.fail("questions must be written for the whole document at once"),
    )
    monkeypatch.setattr(
        processing, "update_document_status", lambda d, s, error=None: log["status"].append((s, error))
    )

    def _run(questions: int):
        processing.vectorise_document_background(
            document_id="doc1", kb_id="kb1", user_id="u1", title="T", file_bytes=b"x",
            mime_type="text/plain", chunk_method="semantic", db_url="",
            indexing_config={"auto_questions_count": questions}, index_modes=["rag"],
        )
        return log

    return _run


def test_chunks_clear_old_rows_then_insert(run):
    log = run(questions=0)

    assert log["status"] == [("completed", None)]
    assert log["deleted"] == [("doc1", "kb1")]
    assert len(log["inserted"]) == 1
    assert [r["chunk_id"] for r in log["inserted"][0]] == [f"p{i}_{j}" for i in range(3) for j in range(2)]


def test_questions_are_embedded_and_inserted_once_per_document(run):
    log = run(questions=2)

    assert log["status"] == [("completed", None)]
    # one embed call for the child chunks, one for every question of the document
    assert len(log["embed"]) == 2
    assert log["embed"][1] == [f"parent {i}?{k}" for i in range(3) for k in range(2)]
    question_rows = log["inserted"][1]
    assert [(r["chunk_id"], r["parent_chunk_id"], r["chunk_index"]) for r in question_rows] == [
        (f"q_p{i}_{k}", f"p{i}", i) for i in range(3) for k in range(2)
    ]
    assert all(r["dense_embedding"] == [float(len(r["content"]))] for r in question_rows)
