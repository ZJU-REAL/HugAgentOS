"""Milvus vector store for private knowledge base.

Collection: hugagent_kb_private
- Stores both chunk rows (row_type='chunk') and question rows (row_type='question')
- Child chunk vectors used for retrieval; parent chunk content fetched from PostgreSQL
- User isolation enforced via user_id field in every query expression
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

COLLECTION_NAME = "hugagent_kb_private"

# Milvus VARCHAR field capacity (**bytes**, not characters) —— the schema definition and the write truncation share the same
# set of constants, to avoid changing max_length but forgetting the truncation and hitting "length exceeds max length" again.
# The content column is only a display fallback for retrieval results (dense/sparse vectors are computed from the full text,
# and returned content is fetched full from PostgreSQL), so safe byte-wise truncation does not affect retrieval quality.
TITLE_FIELD_MAX_BYTES = 500
CONTENT_FIELD_MAX_BYTES = 4096
TAGS_FIELD_MAX_BYTES = 1000


def truncate_utf8(text: str, max_bytes: int = CONTENT_FIELD_MAX_BYTES) -> str:
    """Safe truncation by UTF-8 bytes (Chinese is 3 bytes/char); never cuts in the middle of a multi-byte character.

    Historical bug: using ``text[:4096]`` truncated by "character"; 4096 Chinese chars ≈ 12KB bytes, still exceeding Milvus's
    4096 **byte** limit → ``length exceeds max length`` error.
    """
    if not text:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", "ignore")


# Fallback dimension: used only when the real dimension can't be probed from the online embedding endpoint (not configured/unreachable).
# In normal operation the dimension is auto-determined by detect_embed_dim() from the actual output of the chosen embedding model, no manual filling needed.
_EMBED_DIMS = int(os.getenv("MEM0_EMBED_DIMS", "1024"))

# Cache of probed dimensions: key=(embed_url, embed_model) → dim, to avoid hitting /embeddings on every index.
_DETECTED_DIM_CACHE: dict[tuple[str, str], int] = {}
# Collection dimension already confirmed in this process (avoids a describe on every get_or_create).
_VERIFIED_COLLECTION_DIM: Optional[int] = None

# Sparse vector dimension space (hash modulo)
_SPARSE_DIM_SPACE = 100_000


def _resolve_embed_config() -> tuple[str, str, str]:
    """Resolve embedding config from DB, with env fallback."""
    try:
        from core.services.model_config import ModelConfigService
        cfg = ModelConfigService.get_instance().resolve("embedding")
        if cfg:
            return cfg.base_url.rstrip("/"), cfg.model_name, cfg.api_key
    except Exception:
        pass
    return (
        os.getenv("MEM0_EMBED_URL", "").rstrip("/"),
        os.getenv("MEM0_EMBED_MODEL", ""),
        os.getenv("MEM0_EMBED_API_KEY", ""),
    )


def _resolve_reranker_config() -> tuple[str, str, str]:
    """Resolve reranker config from DB, with env fallback."""
    try:
        from core.services.model_config import ModelConfigService
        cfg = ModelConfigService.get_instance().resolve("reranker")
        if cfg:
            return cfg.base_url.rstrip("/"), cfg.model_name, cfg.api_key
    except Exception:
        pass
    return (
        os.getenv("RERANKER_URL", "").rstrip("/"),
        os.getenv("RERANKER_MODEL", ""),
        os.getenv("RERANKER_API_KEY", ""),
    )


# ── Embedding ──────────────────────────────────────────────────────────────────

# Max number of texts packed into a single embedding request. A large document is split into hundreds or thousands of chunks;
# sending the whole batch at once to a slow embedding model (e.g. 8B, ~0.45s/item serially) will inevitably hit the single-request
# read timeout; splitting into sub-batches at this granularity keeps each request's latency under the timeout line.
# Overridable via KB_EMBED_BATCH_SIZE.
_EMBED_BATCH_SIZE = max(1, int(os.getenv("KB_EMBED_BATCH_SIZE", "32")))
# Read timeout (seconds) for a single embedding request. Raise it to tolerate a slow model's per-batch latency; the real timeout
# prevention is sub-batching, the two work together —— a sub-batch always has an upper bound, and a larger timeout is only a fallback.
# Overridable via KB_EMBED_TIMEOUT.
_EMBED_TIMEOUT = max(1, int(os.getenv("KB_EMBED_TIMEOUT", "120")))


def _embed_request(url: str, headers: dict, inputs: list[str], embed_model: str) -> list[list[float]]:
    """Post a single embedding request and return vectors ordered by input index."""
    payload = {"input": inputs, "model": embed_model}
    resp = requests.post(url, headers=headers, json=payload, timeout=_EMBED_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return [item["embedding"] for item in sorted(data["data"], key=lambda x: x["index"])]


def embed_text(text: str) -> list[float]:
    """Call the configured embedding service and return a dense vector."""
    embed_url, embed_model, api_key = _resolve_embed_config()

    if not embed_url:
        raise RuntimeError("Embedding model is not configured")

    url = f"{embed_url}/embeddings"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    return _embed_request(url, headers, [text], embed_model)[0]


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Batch embed multiple texts, splitting into sub-batches with one-by-one fallback.

    A large document is split into hundreds or thousands of chunks. Sending the whole batch at once to a slow embedding
    model (e.g. 8B, ~0.45s/item serially) would time out the single request and fail the whole document's vectorization. So we
    split into sub-batches of _EMBED_BATCH_SIZE and request them one sub-batch at a time; if a sub-batch fails, we downgrade to
    embedding one item at a time within that sub-batch, preserving as many other chunks as possible so one timeout doesn't drag
    down the whole document. Only if the one-by-one attempt still fails do we re-raise (treated as a true failure).
    """
    embed_url, embed_model, api_key = _resolve_embed_config()

    if not embed_url:
        raise RuntimeError("Embedding model is not configured")

    url = f"{embed_url}/embeddings"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    results: list[list[float]] = []
    for start in range(0, len(texts), _EMBED_BATCH_SIZE):
        sub = texts[start:start + _EMBED_BATCH_SIZE]
        try:
            results.extend(_embed_request(url, headers, sub, embed_model))
        except Exception as exc:
            logger.warning(
                "Sub-batch embed failed for [%d:%d] (%s); falling back to one-by-one",
                start, start + len(sub), exc,
            )
            for one in sub:
                results.extend(_embed_request(url, headers, [one], embed_model))
    return results


def detect_embed_dim() -> int:
    """Probe the real output dimension of the currently chosen embedding model (one /embeddings call, take the vector length).

    The result is cached by (embed_url, embed_model) to avoid repeated probing on every index; on probe failure (endpoint not
    configured/unreachable) it falls back to MEM0_EMBED_DIMS. This way the table dimension auto-follows after switching the
    embedding model, no manual filling needed.
    """
    embed_url, embed_model, _ = _resolve_embed_config()
    key = (embed_url, embed_model)
    cached = _DETECTED_DIM_CACHE.get(key)
    if cached:
        return cached
    try:
        dim = len(embed_text("dimension probe"))
        if dim > 0:
            _DETECTED_DIM_CACHE[key] = dim
            logger.info("Auto-detected embedding dim=%d (model=%s)", dim, embed_model or "?")
            return dim
    except Exception as exc:
        logger.warning(
            "Embedding dim auto-detect failed (%s); falling back to MEM0_EMBED_DIMS=%d",
            exc, _EMBED_DIMS,
        )
    return _EMBED_DIMS


# ── Reranker ──────────────────────────────────────────────────────────────────

def is_reranker_configured() -> bool:
    """Check if a reranker model endpoint is configured."""
    reranker_url, reranker_model, _ = _resolve_reranker_config()
    return bool(reranker_url and reranker_model)


def rerank(query: str, documents: list[str], top_n: int | None = None) -> list[dict]:
    """Call the configured reranker endpoint (OpenAI-compatible /rerank).

    Returns a list of {"index": int, "relevance_score": float} sorted by score descending.
    """
    reranker_url, reranker_model, reranker_key = _resolve_reranker_config()

    if not reranker_url or not reranker_model:
        raise RuntimeError("Reranker is not configured")

    url = f"{reranker_url}/rerank"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {reranker_key}",
    }
    payload: dict = {
        "model": reranker_model,
        "query": query,
        "documents": documents,
    }
    if top_n is not None:
        payload["top_n"] = top_n

    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    # Response format: {"results": [{"index": 0, "relevance_score": 0.95}, ...]}
    results = data.get("results", [])
    return sorted(results, key=lambda x: x.get("relevance_score", 0), reverse=True)


# ── Milvus helpers ─────────────────────────────────────────────────────────────

def _milvus_uri() -> str:
    from core.config.settings import settings
    return os.getenv("MILVUS_URL") or settings.memory.milvus_url


def _is_lite() -> bool:
    """True when the vector backend is embedded Milvus Lite (a local file uri).

    Milvus Lite (no-Docker local profile) does **not** support SPARSE_FLOAT_VECTOR
    / SPARSE_INVERTED_INDEX / hybrid search / IVF / scalar INVERTED indexes, so the
    KB degrades to **dense-only** on it. A server uri (``http(s)://`` / ``tcp://``)
    is a full Milvus and keeps the dense+sparse hybrid path byte-for-byte unchanged.
    """
    return not _milvus_uri().startswith(("http://", "https://", "tcp://", "unix:"))


def _get_client():
    """Return a MilvusClient connected to the configured Milvus instance."""
    from pymilvus import MilvusClient
    url = _milvus_uri()
    token = os.getenv("MILVUS_TOKEN", "")
    if token:
        return MilvusClient(uri=url, token=token)
    return MilvusClient(uri=url)


def _upsert(client, data: list[dict[str, Any]]) -> None:
    """Upsert helper that drops the ``sparse_embedding`` field on Milvus Lite.

    On Lite the collection has no sparse field (see ``get_or_create_collection``),
    and dynamic fields are disabled, so an extra ``sparse_embedding`` key would be
    rejected. Stripping it here keeps every caller's row-building code identical
    across both backends.
    """
    if _is_lite():
        data = [{k: v for k, v in row.items() if k != "sparse_embedding"} for row in data]
    client.upsert(collection_name=COLLECTION_NAME, data=data)


def _collection_dense_dim(client) -> Optional[int]:
    """Read the dense_embedding dimension of the existing collection; return None if it can't be read."""
    try:
        desc = client.describe_collection(COLLECTION_NAME)
        for f in desc.get("fields", []):
            if f.get("name") == "dense_embedding":
                dim = f.get("params", {}).get("dim")
                return int(dim) if dim is not None else None
    except Exception:
        return None
    return None


def get_or_create_collection() -> None:
    """Idempotently create hugagent_kb_private (hybrid retrieval schema) at the current embedding model's real dimension.

    The dimension auto-follows the model: if an existing collection's dimension differs from the current model's output (meaning
    the embedding model was changed), it auto-drops and recreates —— old vectors come from another model and are inherently
    incomparable, so documents must be re-indexed.
    """
    global _VERIFIED_COLLECTION_DIM
    from pymilvus import DataType

    client = _get_client()
    target_dim = detect_embed_dim()

    if client.has_collection(COLLECTION_NAME):
        if _VERIFIED_COLLECTION_DIM == target_dim:
            return
        existing_dim = _collection_dense_dim(client)
        if existing_dim == target_dim:
            _VERIFIED_COLLECTION_DIM = target_dim
            return
        logger.warning(
            "KB collection 维度不匹配（已存在=%s，当前模型=%d）：drop 重建。"
            "旧向量来自不同 embedding 模型、不可复用，需重新索引文档。",
            existing_dim, target_dim,
        )
        client.drop_collection(COLLECTION_NAME)
        _VERIFIED_COLLECTION_DIM = None

    lite = _is_lite()

    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("chunk_id",        DataType.VARCHAR, max_length=64, is_primary=True)
    schema.add_field("parent_chunk_id", DataType.VARCHAR, max_length=64)
    schema.add_field("row_type",        DataType.VARCHAR, max_length=16)   # "chunk" | "question"
    schema.add_field("user_id",         DataType.VARCHAR, max_length=64)
    schema.add_field("kb_id",           DataType.VARCHAR, max_length=64)
    schema.add_field("document_id",     DataType.VARCHAR, max_length=64)
    schema.add_field("title",           DataType.VARCHAR, max_length=TITLE_FIELD_MAX_BYTES)
    schema.add_field("content",         DataType.VARCHAR, max_length=CONTENT_FIELD_MAX_BYTES)
    schema.add_field("tags_text",       DataType.VARCHAR, max_length=TAGS_FIELD_MAX_BYTES)  # BM25 augmentation
    schema.add_field("chunk_index",     DataType.INT64)
    schema.add_field("dense_embedding", DataType.FLOAT_VECTOR, dim=target_dim)
    if not lite:
        schema.add_field("sparse_embedding", DataType.SPARSE_FLOAT_VECTOR)

    index_params = client.prepare_index_params()
    if lite:
        # Milvus Lite: only a dense AUTOINDEX (FLAT under the hood). No sparse, no
        # scalar INVERTED indexes — filtering falls back to brute force, fine at
        # single-machine scale.
        index_params.add_index(
            field_name="dense_embedding",
            index_type="AUTOINDEX",
            metric_type="IP",
        )
    else:
        index_params.add_index(
            field_name="dense_embedding",
            index_type="IVF_FLAT",
            metric_type="IP",
            params={"nlist": 128},
        )
        index_params.add_index(
            field_name="sparse_embedding",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="IP",
        )
        # Scalar indexes for filtering acceleration
        for field in ("user_id", "kb_id", "document_id", "row_type"):
            index_params.add_index(field_name=field, index_type="INVERTED")

    client.create_collection(
        collection_name=COLLECTION_NAME,
        schema=schema,
        index_params=index_params,
    )
    _VERIFIED_COLLECTION_DIM = target_dim
    logger.info(
        "Created Milvus collection: %s (dim=%d, mode=%s)",
        COLLECTION_NAME, target_dim, "lite/dense-only" if lite else "hybrid",
    )


# ── Write ──────────────────────────────────────────────────────────────────────

def upsert_rows(rows: list[dict[str, Any]]) -> None:
    """Upsert a batch of rows into hugagent_kb_private.

    Each row must include all schema fields. dense_embedding and sparse_embedding
    must be pre-computed by the caller (use embed_text / build_sparse_text).
    """
    if not rows:
        return
    get_or_create_collection()
    client = _get_client()
    _upsert(client, rows)
    logger.debug("Upserted %d rows into %s", len(rows), COLLECTION_NAME)


def delete_by_document(document_id: str, user_id: str) -> None:
    """Delete all Milvus rows (chunk + question) for a document."""
    get_or_create_collection()
    client = _get_client()
    expr = f'document_id == "{document_id}" and user_id == "{user_id}"'
    client.delete(collection_name=COLLECTION_NAME, filter=expr)
    logger.info("Deleted Milvus rows for document_id=%s user_id=%s", document_id, user_id)


def delete_by_kb(kb_id: str, user_id: str) -> None:
    """Delete all Milvus rows for an entire KB space."""
    get_or_create_collection()
    client = _get_client()
    expr = f'kb_id == "{kb_id}" and user_id == "{user_id}"'
    client.delete(collection_name=COLLECTION_NAME, filter=expr)
    logger.info("Deleted Milvus rows for kb_id=%s user_id=%s", kb_id, user_id)


def get_children_for_parent(parent_chunk_id: str) -> list[dict]:
    """Return the child-chunk rows of a given parent chunk in Milvus (for the "view chunks" parent→child display).

    Child rows have ``chunk_id`` shaped like ``{pid}_{i}`` with ``parent_chunk_id == pid``; in flat mode a parent has only one
    representative row (``chunk_id == parent_chunk_id``), which is filtered out —— i.e. flat mode returns an empty list.
    Returns an empty list when Milvus is unavailable / the query fails, without affecting the parent-chunk list display.
    """
    try:
        get_or_create_collection()
        client = _get_client()
        rows = client.query(
            collection_name=COLLECTION_NAME,
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
    get_or_create_collection()
    client = _get_client()
    expr = f'parent_chunk_id == "{parent_chunk_id}"'
    client.delete(collection_name=COLLECTION_NAME, filter=expr)
    logger.info("Deleted Milvus rows for parent_chunk_id=%s", parent_chunk_id)


# ── Search ─────────────────────────────────────────────────────────────────────

def _hit_to_dict(ent, score: float) -> dict:
    """Build the shared KB search-result row. ``ent`` is any entity accessor with
    ``.get`` (a dict from Milvus Lite, or a Hit entity from hybrid search)."""
    return {
        "chunk_id":        ent.get("chunk_id"),
        "parent_chunk_id": ent.get("parent_chunk_id") or ent.get("chunk_id"),
        "row_type":        ent.get("row_type"),
        "kb_id":           ent.get("kb_id"),
        "document_id":     ent.get("document_id"),
        "title":           ent.get("title"),
        "content":         ent.get("content"),
        "chunk_index":     ent.get("chunk_index"),
        "score":           score,
    }


def hybrid_search(
    user_id: str,
    kb_ids: list[str],
    query: str,
    query_vec: list[float],
    top_k: int = 10,
    *,
    public_kb_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Hybrid search over dense + sparse vectors with RRF fusion.

    Searches both chunk rows and question rows simultaneously.
    Results are sorted by fused score; dedup by parent_chunk_id is done by the caller.

    ``kb_ids`` are private spaces and are filtered by ``user_id`` (owner isolation).
    ``public_kb_ids`` are admin-managed public spaces visible to everyone — they are
    filtered by ``kb_id`` only (no owner check), since public KBs are global and
    ``kb_id`` is globally unique.
    """
    from pymilvus import AnnSearchRequest, RRFRanker, MilvusClient

    get_or_create_collection()
    client = _get_client()

    clauses: list[str] = []
    if kb_ids:
        clauses.append(f'(user_id == "{user_id}" and kb_id in {json.dumps(kb_ids)})')
    if public_kb_ids:
        clauses.append(f'kb_id in {json.dumps(public_kb_ids)}')
    if not clauses:
        return []
    expr = " or ".join(clauses)

    output_fields = [
        "chunk_id", "parent_chunk_id", "row_type",
        "kb_id", "document_id", "title", "content", "chunk_index",
    ]

    # Milvus Lite (no-Docker local): no sparse field / hybrid search — fall back to
    # a plain dense ANN search over the same collection. Same result shape.
    if _is_lite():
        lite_res = client.search(
            collection_name=COLLECTION_NAME,
            data=[query_vec],
            anns_field="dense_embedding",
            search_params={"metric_type": "IP"},
            limit=top_k * 2,
            filter=expr,
            output_fields=output_fields,
        )
        return [_hit_to_dict(hit.get("entity", hit), hit.get("distance", 0.0))
                for hit in lite_res[0]]

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
        collection_name=COLLECTION_NAME,
        reqs=[dense_req, sparse_req],
        ranker=RRFRanker(k=60),
        limit=top_k * 2,
        output_fields=output_fields,
    )

    return [_hit_to_dict(hit.entity, hit.score) for hit in results[0]]


# ── Tag/question re-index ──────────────────────────────────────────────────────

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
    tokens = re.findall(r'[\w\u4e00-\u9fff]+', text.lower())
    if not tokens:
        return {0: 1.0}

    counts = Counter(tokens)
    total = sum(counts.values())

    sparse: dict[int, float] = {}
    for token, cnt in counts.items():
        dim = int(hashlib.md5(token.encode()).hexdigest()[:8], 16) % _SPARSE_DIM_SPACE
        tf = cnt / total
        sparse[dim] = sparse.get(dim, 0.0) + tf

    return sparse if sparse else {0: 1.0}


def reindex_chunk_tags(chunk_id: str, content: str, tags: list[str]) -> None:
    """Re-compute and upsert sparse_embedding for a chunk row after tag changes."""
    # Fetch the existing row to preserve all other fields
    get_or_create_collection()
    client = _get_client()
    rows = client.query(
        collection_name=COLLECTION_NAME,
        filter=f'chunk_id == "{chunk_id}"',
        output_fields=["chunk_id", "parent_chunk_id", "row_type", "user_id",
                       "kb_id", "document_id", "title", "chunk_index",
                       "dense_embedding"],
    )
    if not rows:
        logger.warning("reindex_chunk_tags: chunk_id %s not found in Milvus", chunk_id)
        return

    row = rows[0]
    sparse_text = build_sparse_text(content, tags)
    row["content"] = truncate_utf8(content)
    row["tags_text"] = truncate_utf8(" ".join(tags), TAGS_FIELD_MAX_BYTES)
    row["sparse_embedding"] = text_to_sparse(sparse_text)
    _upsert(client, [row])


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
    get_or_create_collection()
    client = _get_client()
    # Drop existing chunk rows for this parent (keep questions; they are re-upserted separately)
    client.delete(
        collection_name=COLLECTION_NAME,
        filter=f'parent_chunk_id == "{chunk_id}" and row_type == "chunk"',
    )
    sparse_text = build_sparse_text(content, tags or [])
    row = {
        "chunk_id": chunk_id,
        "parent_chunk_id": chunk_id,
        "row_type": "chunk",
        "user_id": user_id,
        "kb_id": kb_id,
        "document_id": document_id,
        "title": truncate_utf8(title, TITLE_FIELD_MAX_BYTES),
        "content": truncate_utf8(content),
        "tags_text": truncate_utf8(" ".join(tags or []), TAGS_FIELD_MAX_BYTES),
        "chunk_index": chunk_index,
        "dense_embedding": embed_text(content),
        "sparse_embedding": text_to_sparse(sparse_text),
    }
    _upsert(client, [row])


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
    get_or_create_collection()
    client = _get_client()

    # Delete old question rows
    expr = f'parent_chunk_id == "{parent_chunk_id}" and row_type == "question"'
    client.delete(collection_name=COLLECTION_NAME, filter=expr)

    if not questions:
        return

    # Embed all questions in one batch call
    vecs = embed_batch(questions)
    rows = []
    for i, (q, vec) in enumerate(zip(questions, vecs)):
        rows.append({
            "chunk_id":        f"q_{parent_chunk_id}_{i}",
            "parent_chunk_id": parent_chunk_id,
            "row_type":        "question",
            "user_id":         user_id,
            "kb_id":           kb_id,
            "document_id":     document_id,
            "title":           truncate_utf8(title, TITLE_FIELD_MAX_BYTES),
            "content":         truncate_utf8(q),
            "tags_text":       "",
            "chunk_index":     chunk_index,
            "dense_embedding": vec,
            "sparse_embedding": text_to_sparse(q),
        })
    _upsert(client, rows)
