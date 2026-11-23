"""Milvus row operations for private KB spaces: write, delete, child-chunk lookup, re-embed edited chunks."""
from __future__ import annotations

import logging
from typing import Any

from core.models import embedding as vector_embedding
from features.knowledge import kb_vector, kb_vector_state, vector_client, vector_fields

logger = logging.getLogger(__name__)

WRITE_BATCH = 100
_lit = vector_fields.milvus_literal


def batched(items: list, size: int = WRITE_BATCH):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _scoped(expr: str, kb_id: str | None) -> str:
    """Add the space to a filter: ``kb_id`` is the partition key, so Milvus then
    touches one partition instead of all of them."""
    return f"kb_id == {_lit(kb_id)} and {expr}" if kb_id else expr


def _delete(filter_expr: str) -> None:
    client = vector_client.get_client()
    # No collection means nothing was ever indexed; don't create one just to delete.
    if not vector_client.existing_collection(client):
        return
    client.delete(collection_name=kb_vector_state.COLLECTION_NAME, filter=filter_expr)


def _write_rows(rows: list[dict[str, Any]], write) -> None:
    if not rows:
        return
    vector_client.get_or_create_collection()
    client = vector_client.get_client()
    for batch in batched(rows):
        write(client, batch)


def upsert_rows(rows: list[dict[str, Any]]) -> None:
    """Upsert rows whose primary keys may already exist (asset rows, edits).

    Each row carries the schema fields plus ``sparse_text`` (see build_sparse_text);
    ``vector_client.adapt_rows`` turns that into whatever the collection stores.
    """
    _write_rows(rows, vector_client.upsert)


def insert_rows(rows: list[dict[str, Any]]) -> None:
    """Insert rows with fresh primary keys (a document's first write after its old rows were cleared)."""
    _write_rows(rows, vector_client.insert)


def delete_document_vectors(document_id: str, kb_id: str | None = None) -> None:
    """Delete every Milvus row (chunk, question, asset) of a document, whoever wrote it.

    ``document_id`` is globally unique, so no owner filter is needed. Shared spaces write
    their rows under the system owner, so an owner-scoped delete can miss them.
    """
    _delete(_scoped(f"document_id == {_lit(document_id)}", kb_id))
    logger.info("Deleted Milvus rows for document_id=%s", document_id)


def delete_space_vectors(kb_id: str) -> None:
    """Delete every Milvus row of a KB space, whoever wrote it."""
    _delete(f"kb_id == {_lit(kb_id)}")
    logger.info("Deleted Milvus rows for kb_id=%s", kb_id)


def get_children_for_parent(parent_chunk_id: str, kb_id: str | None = None) -> list[dict]:
    """Return the child-chunk rows of a given parent chunk in Milvus (for the "view chunks" parent→child display).

    Child rows have ``chunk_id`` shaped like ``{pid}_{i}`` with ``parent_chunk_id == pid``; in flat mode a parent has only one
    representative row (``chunk_id == parent_chunk_id``), which is filtered out —— i.e. flat mode returns an empty list.
    Returns an empty list when Milvus is unavailable / the query fails, without affecting the parent-chunk list display.
    """
    try:
        client = vector_client.get_client()
        if not vector_client.existing_collection(client):
            return []
        rows = client.query(
            collection_name=kb_vector_state.COLLECTION_NAME,
            # In flat mode a parent has only one representative row (chunk_id == parent_chunk_id); exclude it right in the query
            filter=_scoped(
                f'parent_chunk_id == {_lit(parent_chunk_id)} and row_type == "chunk" '
                f"and chunk_id != {_lit(parent_chunk_id)}",
                kb_id,
            ),
            output_fields=["chunk_id", "parent_chunk_id", "chunk_index", "content"],
            limit=4096,
        )
    except Exception as exc:
        logger.warning("get_children_for_parent(%s) failed: %s", parent_chunk_id, exc)
        return []
    children = [
        {
            "chunk_id": r.get("chunk_id"),
            "chunk_index": r.get("chunk_index", 0),
            "content": r.get("content", ""),
        }
        for r in rows
    ]
    children.sort(key=lambda x: x["chunk_index"])
    return children


def delete_by_chunk(parent_chunk_id: str, kb_id: str | None = None) -> None:
    """Delete all Milvus rows (chunk + question) for a single parent chunk."""
    _delete(_scoped(f"parent_chunk_id == {_lit(parent_chunk_id)}", kb_id))
    logger.info("Deleted Milvus rows for parent_chunk_id=%s", parent_chunk_id)


def reindex_chunk_tags(chunk_id: str, content: str, tags: list[str]) -> None:
    """Re-write a chunk row's sparse text (and so its keyword vector) after tag changes."""
    # Fetch the existing row to preserve all other fields
    client = vector_client.get_client()
    if not vector_client.existing_collection(client):
        logger.warning("reindex_chunk_tags: no KB collection, chunk_id %s not indexed", chunk_id)
        return
    rows = client.query(
        collection_name=kb_vector_state.COLLECTION_NAME,
        filter=f"chunk_id == {_lit(chunk_id)}",
        output_fields=[
            "chunk_id",
            "parent_chunk_id",
            "row_type",
            "user_id",
            "kb_id",
            "document_id",
            "title",
            "chunk_index",
            "dense_embedding",
        ],
    )
    if not rows:
        logger.warning("reindex_chunk_tags: chunk_id %s not found in Milvus", chunk_id)
        return

    row = rows[0]
    sparse_text = kb_vector.build_sparse_text(content, tags)
    row["content"] = vector_fields.truncate_utf8(content)
    row["tags_text"] = vector_fields.truncate_utf8(" ".join(tags), kb_vector_state.TAGS_FIELD_MAX_BYTES)
    row["sparse_text"] = sparse_text
    vector_client.upsert(client, [row])


def reindex_chunk_content(
    chunk_id: str,
    content: str,
    tags: list[str],
    user_id: str,
    kb_id: str,
    document_id: str,
    title: str,
    chunk_index: int,
) -> None:
    """Re-embed a parent chunk after a manual content edit.

    The new vector is computed first, so a failing embedding model leaves the old
    rows searchable instead of deleting them and writing nothing. Then the parent's
    existing chunk rows (child or flat) are replaced by a single fresh chunk row.
    Question rows are managed separately by :func:`upsert_question_rows`. Retrieval
    returns the parent content from PostgreSQL, so this single representative row is
    sufficient for the edited chunk to be matched and surfaced.
    """
    dense = vector_embedding.embed_text(content)
    sparse_text = kb_vector.build_sparse_text(content, tags or [])
    row = {
        "chunk_id": chunk_id,
        "parent_chunk_id": chunk_id,
        "row_type": "chunk",
        "user_id": user_id,
        "kb_id": kb_id,
        "document_id": document_id,
        "title": vector_fields.truncate_utf8(title, kb_vector_state.TITLE_FIELD_MAX_BYTES),
        "content": vector_fields.truncate_utf8(content),
        "tags_text": vector_fields.truncate_utf8(" ".join(tags or []), kb_vector_state.TAGS_FIELD_MAX_BYTES),
        "chunk_index": chunk_index,
        "dense_embedding": dense,
        "sparse_text": sparse_text,
    }
    vector_client.get_or_create_collection()
    # Drop the parent's other chunk rows (children); the representative row itself is upserted.
    _delete(_scoped(
        f'parent_chunk_id == {_lit(chunk_id)} and row_type == "chunk" and chunk_id != {_lit(chunk_id)}',
        kb_id,
    ))
    vector_client.upsert(vector_client.get_client(), [row])


def build_question_rows(
    parent_chunk_id: str,
    questions: list[str],
    vectors: list[list[float]],
    *,
    user_id: str,
    kb_id: str,
    document_id: str,
    title: str,
    chunk_index: int,
) -> list[dict[str, Any]]:
    """Milvus rows for a parent chunk's generated questions (vectors pre-computed)."""
    title_field = vector_fields.truncate_utf8(title, kb_vector_state.TITLE_FIELD_MAX_BYTES)
    return [
        {
            "chunk_id": f"q_{parent_chunk_id}_{i}",
            "parent_chunk_id": parent_chunk_id,
            "row_type": "question",
            "user_id": user_id,
            "kb_id": kb_id,
            "document_id": document_id,
            "title": title_field,
            "content": vector_fields.truncate_utf8(q),
            "tags_text": "",
            "chunk_index": chunk_index,
            "dense_embedding": vec,
            "sparse_text": q,
        }
        for i, (q, vec) in enumerate(zip(questions, vectors))
    ]


def upsert_question_rows(
    parent_chunk_id: str,
    questions: list[str],
    user_id: str,
    kb_id: str,
    document_id: str,
    title: str,
    chunk_index: int,
) -> None:
    """Replace one chunk's question rows (manual edit of its questions).

    Questions are embedded before the old rows are deleted, so an embedding failure
    keeps the previous questions searchable.
    """
    vecs = vector_embedding.embed_batch(questions) if questions else []
    vector_client.get_or_create_collection()
    _delete(_scoped(f'parent_chunk_id == {_lit(parent_chunk_id)} and row_type == "question"', kb_id))
    if not questions:
        return
    rows = build_question_rows(
        parent_chunk_id,
        questions,
        vecs,
        user_id=user_id,
        kb_id=kb_id,
        document_id=document_id,
        title=title,
        chunk_index=chunk_index,
    )
    vector_client.insert(vector_client.get_client(), rows)
