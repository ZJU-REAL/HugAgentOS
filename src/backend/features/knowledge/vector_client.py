"""Milvus client access for the private KB: URI / Lite detection, client, writes and collection lifecycle."""
from __future__ import annotations
import os
import threading
from typing import Any, Optional
import logging
logger = logging.getLogger(__name__)
from features.knowledge import kb_vector_state
from core.models import embedding as vector_embedding

_CLIENTS: dict[tuple[str, str], Any] = {}
_CLIENTS_LOCK = threading.Lock()


def milvus_uri() -> str:
    from core.config.settings import settings

    return os.getenv("MILVUS_URL") or settings.memory.milvus_url


def is_lite() -> bool:
    """True when the vector backend is embedded Milvus Lite (a local file uri).

    Milvus Lite (no-Docker local profile) does **not** support SPARSE_FLOAT_VECTOR
    / SPARSE_INVERTED_INDEX / hybrid search / IVF / scalar INVERTED indexes, so the
    KB degrades to **dense-only** on it. A server uri (``http(s)://`` / ``tcp://``)
    is a full Milvus and keeps the dense+sparse hybrid path.
    """
    return not milvus_uri().startswith(("http://", "https://", "tcp://", "unix:"))


class VectorStoreUnavailableError(RuntimeError):
    """The vector store cannot be reached; the message is written for the person who uploaded."""

    def __init__(self) -> None:
        super().__init__(
            "向量检索服务（Milvus）连接不上，这份文档的检索索引没有建成。"
            "请联系管理员启动向量数据库后点「重试」。"
        )


def get_client():
    """Return the process-wide MilvusClient for the configured Milvus instance.

    pymilvus already shares the gRPC channel per address, but building a client
    still costs a server-type round trip; one client per (uri, token) is reused.
    """
    url = milvus_uri()
    token = os.getenv("MILVUS_TOKEN", "")
    key = (url, token)
    client = _CLIENTS.get(key)
    if client is not None:
        return client
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(key)
        if client is None:
            from pymilvus import MilvusClient

            client = MilvusClient(uri=url, token=token) if token else MilvusClient(uri=url)
            _CLIENTS[key] = client
    return client


def invalidate_collection_cache() -> None:
    """Forget what this process knows about the collection (after a failed write,
    or after maintenance swapped collections) so the next call re-checks Milvus."""
    kb_vector_state.ACTIVE_INDEX = None
    kb_vector_state.COLLECTION_EXISTS = False
    kb_vector_state.VERIFIED_COLLECTION_DIM = None


def resolve_collection(client, *, timeout: float | None = None) -> str:
    """Pick the collection this process uses and remember it.

    Lite keeps the legacy dense-only collection. A server uses the current HNSW +
    partition-key collection when it exists, or when nothing exists yet (it will be
    created). A server that only has the legacy collection keeps serving it until
    an operator runs the migration, so no data disappears on upgrade.
    """
    if kb_vector_state.ACTIVE_INDEX is not None:
        return kb_vector_state.COLLECTION_NAME
    if is_lite():
        name, index = kb_vector_state.LEGACY_COLLECTION_NAME, "lite"
    elif client.has_collection(kb_vector_state.CURRENT_COLLECTION_NAME, timeout=timeout):
        name, index = kb_vector_state.CURRENT_COLLECTION_NAME, "hnsw"
    elif client.has_collection(kb_vector_state.LEGACY_COLLECTION_NAME, timeout=timeout):
        name, index = kb_vector_state.LEGACY_COLLECTION_NAME, "ivf"
        logger.warning(
            "KB vectors are still in legacy collection %s (IVF_FLAT, no partition key). "
            "Run `python -m features.knowledge.vector_maintenance migrate --apply` "
            "during a maintenance window to move them to %s.",
            name,
            kb_vector_state.CURRENT_COLLECTION_NAME,
        )
    else:
        name, index = kb_vector_state.CURRENT_COLLECTION_NAME, "hnsw"
    kb_vector_state.COLLECTION_NAME = name
    kb_vector_state.ACTIVE_INDEX = index
    return name


def existing_collection(client, *, timeout: float | None = None) -> bool:
    """True when the active collection exists. For deletes and lookups: they need
    neither the embedding model nor a collection to be created."""
    resolve_collection(client, timeout=timeout)
    if not kb_vector_state.COLLECTION_EXISTS:
        kb_vector_state.COLLECTION_EXISTS = bool(
            client.has_collection(kb_vector_state.COLLECTION_NAME, timeout=timeout)
        )
    return kb_vector_state.COLLECTION_EXISTS


def uses_bm25(index: str | None = None) -> bool:
    """True when the collection derives its sparse vectors with Milvus's BM25
    function (Chinese jieba analyzer) instead of the client-side hashed term counts."""
    return (index or kb_vector_state.ACTIVE_INDEX) == "hnsw"


def adapt_rows(data: list[dict[str, Any]], *, index: str | None = None) -> list[dict[str, Any]]:
    """Shape rows for a collection (default: the active one).

    Every row carries ``sparse_text`` (chunk text + tags). The current collection
    stores that text and lets the BM25 function tokenise it; the legacy collection
    gets the hashed sparse vector it was built with (so queries keep matching its
    stored rows); Lite has no sparse field at all. Dynamic fields are disabled
    everywhere, so any key the collection lacks would be rejected.
    """
    from features.knowledge import kb_vector, vector_fields

    index = index or kb_vector_state.ACTIVE_INDEX
    out = []
    for row in data:
        row = dict(row)
        text = row.pop("sparse_text")
        row.pop("sparse_embedding", None)
        if index == "hnsw":
            row["sparse_text"] = vector_fields.truncate_utf8(text, kb_vector_state.SPARSE_TEXT_FIELD_MAX_BYTES)
        elif index != "lite":
            row["sparse_embedding"] = kb_vector.text_to_sparse(text)
        out.append(row)
    return out


def sparse_search_request(query: str) -> tuple[list, dict[str, Any]]:
    """(data, search_params) for the sparse half of a hybrid search."""
    if uses_bm25():
        return [query], {"metric_type": "BM25"}
    from features.knowledge import kb_vector

    return [kb_vector.text_to_sparse(query)], {"metric_type": "IP"}


def _write(method: str, client, data: list[dict[str, Any]]) -> None:
    try:
        getattr(client, method)(collection_name=kb_vector_state.COLLECTION_NAME, data=adapt_rows(data))
    except Exception:
        invalidate_collection_cache()
        raise


def upsert(client, data: list[dict[str, Any]]) -> None:
    """Upsert rows whose primary keys may already exist (edits, re-tagging)."""
    _write("upsert", client, data)


def insert(client, data: list[dict[str, Any]]) -> None:
    """Insert rows with fresh primary keys. Cheaper than upsert, which Milvus runs as
    delete + insert and which leaves delete markers for compaction to clear."""
    _write("insert", client, data)


def collection_dense_dim(client, *, name: str | None = None, timeout: float | None = None) -> Optional[int]:
    """Read the dense_embedding dimension of an existing collection.

    Connection and schema-read errors deliberately propagate to the caller. Treating
    an unavailable schema as a dimension mismatch used to trigger a destructive
    collection rebuild.
    """
    desc = client.describe_collection(name or kb_vector_state.COLLECTION_NAME, timeout=timeout)
    for f in desc.get("fields", []):
        if f.get("name") == "dense_embedding":
            dim = f.get("params", {}).get("dim")
            return int(dim) if dim is not None else None
    return None


def create_collection(client, name: str, dim: int, *, lite: bool, timeout: float | None = None) -> None:
    """Create a KB collection with the schema and indexes for this backend.

    Server: HNSW dense index (filter-friendly, no nlist/nprobe recall cliff),
    BM25 sparse vectors computed by Milvus from ``sparse_text`` with a jieba
    analyzer (real Chinese word segmentation and corpus IDF), ``kb_id`` as partition key so a search only touches the
    partitions of the spaces it filters on, plus INVERTED scalar indexes for the
    other filter fields. Lite: dense AUTOINDEX only.
    """
    from pymilvus import DataType

    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("chunk_id", DataType.VARCHAR, max_length=64, is_primary=True)
    schema.add_field("parent_chunk_id", DataType.VARCHAR, max_length=64)
    schema.add_field("row_type", DataType.VARCHAR, max_length=16)  # "chunk" | "question" | "image"
    schema.add_field("user_id", DataType.VARCHAR, max_length=64)
    if lite:
        schema.add_field("kb_id", DataType.VARCHAR, max_length=64)
    else:
        schema.add_field("kb_id", DataType.VARCHAR, max_length=64, is_partition_key=True)
    schema.add_field("document_id", DataType.VARCHAR, max_length=64)
    schema.add_field("title", DataType.VARCHAR, max_length=kb_vector_state.TITLE_FIELD_MAX_BYTES)
    schema.add_field("content", DataType.VARCHAR, max_length=kb_vector_state.CONTENT_FIELD_MAX_BYTES)
    schema.add_field(
        "tags_text", DataType.VARCHAR, max_length=kb_vector_state.TAGS_FIELD_MAX_BYTES
    )  # BM25 augmentation
    schema.add_field("chunk_index", DataType.INT64)
    schema.add_field("dense_embedding", DataType.FLOAT_VECTOR, dim=dim)
    if not lite:
        from pymilvus import Function, FunctionType

        schema.add_field(
            "sparse_text",
            DataType.VARCHAR,
            max_length=kb_vector_state.SPARSE_TEXT_FIELD_MAX_BYTES,
            enable_analyzer=True,
            analyzer_params=kb_vector_state.SPARSE_ANALYZER_PARAMS,
        )
        schema.add_field("sparse_embedding", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(
            Function(
                name="sparse_text_bm25",
                function_type=FunctionType.BM25,
                input_field_names=["sparse_text"],
                output_field_names=["sparse_embedding"],
            )
        )

    index_params = client.prepare_index_params()
    extra: dict[str, Any] = {}
    if lite:
        # Milvus Lite: only a dense AUTOINDEX (FLAT under the hood). No sparse, no
        # scalar INVERTED indexes — filtering falls back to brute force, fine at
        # single-machine scale.
        index_params.add_index(field_name="dense_embedding", index_type="AUTOINDEX", metric_type="IP")
    else:
        index_params.add_index(
            field_name="dense_embedding",
            index_type="HNSW",
            metric_type="IP",
            params={"M": 16, "efConstruction": 200},
        )
        index_params.add_index(
            field_name="sparse_embedding",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
        )
        for field in ("user_id", "kb_id", "document_id", "parent_chunk_id", "row_type"):
            index_params.add_index(field_name=field, index_type="INVERTED")
        extra["num_partitions"] = kb_vector_state.PARTITION_COUNT

    client.create_collection(
        collection_name=name,
        schema=schema,
        index_params=index_params,
        timeout=timeout,
        **extra,
    )
    logger.info(
        "Created Milvus collection: %s (dim=%d, mode=%s)",
        name,
        dim,
        "lite/dense-only" if lite else "hybrid/hnsw/partition-key",
    )


def get_or_create_collection(*, timeout: float | None = None) -> None:
    """Make sure the active KB collection exists at the current embedding model's dimension.

    After the first successful check the result is cached for the process, so the
    hot read/write paths make no extra round trip. Existing collections are never
    dropped implicitly: an unreadable schema or dimension mismatch preserves all
    vectors and aborts the operation until an operator can verify the embedding
    configuration and explicitly re-index the collection.
    """
    target_dim = vector_embedding.detect_embed_dim()
    if kb_vector_state.VERIFIED_COLLECTION_DIM == target_dim:
        return
    try:
        client = get_client()
        name = resolve_collection(client, timeout=timeout)
        exists = client.has_collection(name, timeout=timeout)
    except Exception as exc:
        raise VectorStoreUnavailableError() from exc

    if exists:
        kb_vector_state.COLLECTION_EXISTS = True
        try:
            existing_dim = collection_dense_dim(client, name=name, timeout=timeout)
        except Exception as exc:
            message = (
                "无法确认现有知识库集合的向量维度，已保留集合并中止本次操作；"
                "请检查 Milvus 连接后重试。"
            )
            logger.error("%s 原因：%s", message, exc, exc_info=True)
            raise RuntimeError(message) from exc
        if existing_dim is None:
            message = (
                "无法确认现有知识库集合的向量维度，已保留集合并中止本次操作；"
                "dense_embedding 字段或 dim 参数缺失。"
            )
            logger.error(message)
            raise RuntimeError(message)
        if existing_dim == target_dim:
            kb_vector_state.VERIFIED_COLLECTION_DIM = target_dim
            return
        message = (
            f"KB collection 维度不匹配（已存在={existing_dim}，当前模型={target_dim}），"
            "已保留集合并中止本次操作；请核实 embedding 配置并通过显式重建流程重新索引。"
        )
        logger.error(message)
        raise RuntimeError(message)

    if vector_embedding.measured_embed_dim() != target_dim:
        # target_dim is only the configured fallback: creating the collection with it
        # would bake a guessed width into the schema.
        raise RuntimeError("向量模型暂时不可用，无法确认向量维度，知识库集合尚未创建；请检查向量模型配置后重试。")
    create_collection(client, name, target_dim, lite=is_lite(), timeout=timeout)
    kb_vector_state.VERIFIED_COLLECTION_DIM = target_dim
    kb_vector_state.COLLECTION_EXISTS = True


def dense_search_params(limit: int) -> dict[str, Any]:
    """Dense search params for the active collection's index type."""
    if kb_vector_state.ACTIVE_INDEX == "hnsw":
        return {"metric_type": "IP", "params": {"ef": max(64, limit)}}
    if kb_vector_state.ACTIVE_INDEX == "ivf":
        # Legacy IVF_FLAT(nlist=128): 32 of 128 lists keeps filtered recall usable.
        return {"metric_type": "IP", "params": {"nprobe": 32}}
    return {"metric_type": "IP"}
