"""kb vector responsibilities."""
from __future__ import annotations
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from features.knowledge import kb_vector_state
from features.knowledge import vector_client

RRF_K = 60
# Sparse half of a hybrid query; the dense half runs on the caller's thread.
_SUB_SEARCH_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="kb-sparse-search")

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

    # Retrieve each ranking with its payload in the same RPC, then fuse locally.
    # Milvus 2.5 combined hybrid_search always requeries payloads by primary key,
    # and that requery can miss rows under concurrent writes (code 2200,
    # "inconsistent requery result"; milvus-io/milvus#38126). A plain search with
    # scalar output fields returns payloads inline and never requeries.
    # Both rankings keep the identical access scope and run concurrently, so the
    # wall time stays close to a single search and each honours the same timeout.
    def _ranking(field: str, data: list, search_params: dict) -> list:
        return client.search(
            collection_name=kb_vector_state.COLLECTION_NAME,
            data=data,
            anns_field=field,
            search_params=search_params,
            limit=top_k * 3,
            filter=expr,
            output_fields=output_fields,
            timeout=timeout,
        )[0]

    sparse_future = _SUB_SEARCH_POOL.submit(
        _ranking, "sparse_embedding", [text_to_sparse(query)], {"metric_type": "IP"}
    )
    dense_hits = _ranking(
        "dense_embedding", [query_vec], {"metric_type": "IP", "params": {"nprobe": 10}}
    )
    sparse_hits = sparse_future.result()

    fused: dict[str, dict[str, Any]] = {}
    for hits in (dense_hits, sparse_hits):
        for rank, hit in enumerate(hits, start=1):
            entity = hit.get("entity", hit)
            row = fused.setdefault(entity["chunk_id"], hit_to_dict(entity, 0.0))
            row["score"] += 1.0 / (RRF_K + rank)
    return sorted(fused.values(), key=lambda row: row["score"], reverse=True)[: top_k * 2]


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
