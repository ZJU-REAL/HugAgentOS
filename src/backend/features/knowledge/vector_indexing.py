"""vector indexing responsibilities."""
from __future__ import annotations
from features.knowledge import kb_vector
from typing import Any
import logging
logger = logging.getLogger(__name__)
from features.knowledge import kb_vector
from features.knowledge import kb_vector_state
from features.knowledge import vector_client
from core.models import embedding as vector_embedding
from features.knowledge import vector_fields

def upsert_rows(rows: list[dict[str, Any]]) -> None:
    """Upsert a batch of rows into hugagent_kb_private.

    Each row must include all schema fields. dense_embedding and sparse_embedding
    must be pre-computed by the caller (use embed_text / build_sparse_text).
    """
    if not rows:
        return
    vector_client.get_or_create_collection()
    client = vector_client.get_client()
    vector_client.upsert(client, rows)
    logger.debug("Upserted %d rows into %s", len(rows), kb_vector_state.COLLECTION_NAME)


def delete_by_document(document_id: str, user_id: str) -> None:
    """Delete all Milvus rows (chunk + question) for a document."""
    vector_client.get_or_create_collection()
    client = vector_client.get_client()
    expr = f'document_id == "{document_id}" and user_id == "{user_id}"'
    client.delete(collection_name=kb_vector_state.COLLECTION_NAME, filter=expr)
    logger.info("Deleted Milvus rows for document_id=%s user_id=%s", document_id, user_id)


def delete_by_kb(kb_id: str, user_id: str) -> None:
    """Delete all Milvus rows for an entire KB space."""
    vector_client.get_or_create_collection()
    client = vector_client.get_client()
    expr = f'kb_id == "{kb_id}" and user_id == "{user_id}"'
    client.delete(collection_name=kb_vector_state.COLLECTION_NAME, filter=expr)
    logger.info("Deleted Milvus rows for kb_id=%s user_id=%s", kb_id, user_id)


def get_children_for_parent(parent_chunk_id: str) -> list[dict]:
    """Return the child-chunk rows of a given parent chunk in Milvus (for the "view chunks" parent→child display).

    Child rows have ``chunk_id`` shaped like ``{pid}_{i}`` with ``parent_chunk_id == pid``; in flat mode a parent has only one
    representative row (``chunk_id == parent_chunk_id``), which is filtered out —— i.e. flat mode returns an empty list.
    Returns an empty list when Milvus is unavailable / the query fails, without affecting the parent-chunk list display.
    """
    try:
        vector_client.get_or_create_collection()
        client = vector_client.get_client()
        rows = client.query(
            collection_name=kb_vector_state.COLLECTION_NAME,
            # In flat mode a parent has only one representative row (chunk_id == parent_chunk_id); exclude it right in the query
            filter=(
                f'parent_chunk_id == "{parent_chunk_id}" and row_type == "chunk" '
                f'and chunk_id != "{parent_chunk_id}"'
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


def delete_by_chunk(parent_chunk_id: str) -> None:
    """Delete all Milvus rows (chunk + question) for a single parent chunk."""
    vector_client.get_or_create_collection()
    client = vector_client.get_client()
    expr = f'parent_chunk_id == "{parent_chunk_id}"'
    client.delete(collection_name=kb_vector_state.COLLECTION_NAME, filter=expr)
    logger.info("Deleted Milvus rows for parent_chunk_id=%s", parent_chunk_id)


def reindex_chunk_tags(chunk_id: str, content: str, tags: list[str]) -> None:
    """Re-compute and upsert sparse_embedding for a chunk row after tag changes."""
    # Fetch the existing row to preserve all other fields
    vector_client.get_or_create_collection()
    client = vector_client.get_client()
    rows = client.query(
        collection_name=kb_vector_state.COLLECTION_NAME,
        filter=f'chunk_id == "{chunk_id}"',
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
    row["sparse_embedding"] = kb_vector.text_to_sparse(sparse_text)
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

    Removes the parent's existing chunk rows (child or flat) and upserts a single
    fresh chunk row with recomputed dense + sparse embeddings. Question rows are
    managed separately by :func:`upsert_question_rows`. Retrieval returns the parent
    content from PostgreSQL, so this single representative row is sufficient for the
    edited chunk to be matched and surfaced.
    """
    vector_client.get_or_create_collection()
    client = vector_client.get_client()
    # Drop existing chunk rows for this parent (keep questions; they are re-upserted separately)
    client.delete(
        collection_name=kb_vector_state.COLLECTION_NAME,
        filter=f'parent_chunk_id == "{chunk_id}" and row_type == "chunk"',
    )
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
        "dense_embedding": vector_embedding.embed_text(content),
        "sparse_embedding": kb_vector.text_to_sparse(sparse_text),
    }
    vector_client.upsert(client, [row])


def upsert_question_rows(
    parent_chunk_id: str,
    questions: list[str],
    user_id: str,
    kb_id: str,
    document_id: str,
    title: str,
    chunk_index: int,
) -> None:
    """Delete existing question rows for a chunk and insert fresh ones."""
    vector_client.get_or_create_collection()
    client = vector_client.get_client()

    # Delete old question rows
    expr = f'parent_chunk_id == "{parent_chunk_id}" and row_type == "question"'
    client.delete(collection_name=kb_vector_state.COLLECTION_NAME, filter=expr)

    if not questions:
        return

    # Embed all questions in one batch call
    vecs = vector_embedding.embed_batch(questions)
    rows = []
    for i, (q, vec) in enumerate(zip(questions, vecs)):
        rows.append(
            {
                "chunk_id": f"q_{parent_chunk_id}_{i}",
                "parent_chunk_id": parent_chunk_id,
                "row_type": "question",
                "user_id": user_id,
                "kb_id": kb_id,
                "document_id": document_id,
                "title": vector_fields.truncate_utf8(title, kb_vector_state.TITLE_FIELD_MAX_BYTES),
                "content": vector_fields.truncate_utf8(q),
                "tags_text": "",
                "chunk_index": chunk_index,
                "dense_embedding": vec,
                "sparse_embedding": kb_vector.text_to_sparse(q),
            }
        )
    vector_client.upsert(client, rows)
