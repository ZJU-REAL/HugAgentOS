"""kb vector responsibilities."""
from __future__ import annotations
import json
from typing import Any
from features.knowledge import kb_vector_state
from features.knowledge import vector_client

def hit_to_dict(ent, score: float) -> dict:
    """Build the shared KB search-result row. ``ent`` is any entity accessor with
    ``.get`` (a dict from Milvus Lite, or a Hit entity from hybrid search)."""
    return {
        "chunk_id": ent.get("chunk_id"),
        "parent_chunk_id": ent.get("parent_chunk_id") or ent.get("chunk_id"),
        "row_type": ent.get("row_type"),
        "kb_id": ent.get("kb_id"),
        "document_id": ent.get("document_id"),
        "title": ent.get("title"),
        "content": ent.get("content"),
        "chunk_index": ent.get("chunk_index"),
        "score": score,
    }


def hybrid_search(
    user_id: str,
    kb_ids: list[str],
    query: str,
    query_vec: list[float],
    top_k: int = 10,
    *,
    public_kb_ids: list[str] | None = None,
    timeout: float | None = None,
) -> list[dict[str, Any]]:
    """Hybrid search over dense + sparse vectors with RRF fusion.

    Searches both chunk rows and question rows simultaneously.
    Results are sorted by fused score; dedup by parent_chunk_id is done by the caller.

    ``kb_ids`` are private spaces and are filtered by ``user_id`` (owner isolation).
    ``public_kb_ids`` are admin-managed public spaces visible to everyone — they are
    filtered by ``kb_id`` only (no owner check), since public KBs are global and
    ``kb_id`` is globally unique.
    """
    from pymilvus import AnnSearchRequest, MilvusClient, RRFRanker

    vector_client.get_or_create_collection(timeout=timeout)
    client = vector_client.get_client()

    clauses: list[str] = []
    if kb_ids:
        clauses.append(f'(user_id == "{user_id}" and kb_id in {json.dumps(kb_ids)})')
    if public_kb_ids:
        clauses.append(f"kb_id in {json.dumps(public_kb_ids)}")
    if not clauses:
        return []
    expr = " or ".join(clauses)

    output_fields = [
        "chunk_id",
        "parent_chunk_id",
        "row_type",
        "kb_id",
        "document_id",
        "title",
        "content",
        "chunk_index",
    ]

    # Milvus Lite (no-Docker local): no sparse field / hybrid search — fall back to
    # a plain dense ANN search over the same collection. Same result shape.
    if vector_client.is_lite():
        lite_res = client.search(
            collection_name=kb_vector_state.COLLECTION_NAME,
            data=[query_vec],
            anns_field="dense_embedding",
            search_params={"metric_type": "IP"},
            limit=top_k * 2,
            filter=expr,
            output_fields=output_fields,
            timeout=timeout,
        )
        return [
            hit_to_dict(hit.get("entity", hit), hit.get("distance", 0.0)) for hit in lite_res[0]
        ]

    # Dense vector search request
    dense_req = AnnSearchRequest(
        data=[query_vec],
        anns_field="dense_embedding",
        param={"metric_type": "IP", "params": {"nprobe": 10}},
        limit=top_k * 3,
        expr=expr,
    )

    # Sparse search request — bag-of-words vector
    sparse_vec = text_to_sparse(query)
    sparse_req = AnnSearchRequest(
        data=[sparse_vec],
        anns_field="sparse_embedding",
        param={"metric_type": "IP"},
        limit=top_k * 3,
        expr=expr,
    )

    results = client.hybrid_search(
        collection_name=kb_vector_state.COLLECTION_NAME,
        reqs=[dense_req, sparse_req],
        ranker=RRFRanker(k=60),
        limit=top_k * 2,
        output_fields=output_fields,
        timeout=timeout,
    )

    return [hit_to_dict(hit.entity, hit.score) for hit in results[0]]


def build_sparse_text(content: str, tags: list[str]) -> str:
    """Build combined text for sparse vectorisation."""
    if not tags:
        return content
    tag_str = " ".join(f"[{t}]" for t in tags)
    return f"{content} {tag_str}"


def text_to_sparse(text: str) -> dict[int, float]:
    """Convert text to a bag-of-words sparse vector using term-frequency + hashing.

    Compatible with Milvus SPARSE_FLOAT_VECTOR field (v2.4+).
    Each unique token is hashed to a dimension index; value = TF.
    """
    import hashlib
    import re
    from collections import Counter

    # Simple tokenisation: split on non-alphanumeric (works for CJK + Latin)
    tokens = re.findall(r"[\w\u4e00-\u9fff]+", text.lower())
    if not tokens:
        return {0: 1.0}

    counts = Counter(tokens)
    total = sum(counts.values())

    sparse: dict[int, float] = {}
    for token, cnt in counts.items():
        dim = int(hashlib.md5(token.encode()).hexdigest()[:8], 16) % kb_vector_state.SPARSE_DIM_SPACE
        tf = cnt / total
        sparse[dim] = sparse.get(dim, 0.0) + tf

    return sparse if sparse else {0: 1.0}
