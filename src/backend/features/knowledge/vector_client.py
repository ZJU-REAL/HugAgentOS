"""vector client responsibilities."""
from __future__ import annotations
import os
from typing import Any, Optional
import logging
logger = logging.getLogger(__name__)
from features.knowledge import kb_vector_state
from core.models import embedding as vector_embedding

def milvus_uri() -> str:
    from core.config.settings import settings

    return os.getenv("MILVUS_URL") or settings.memory.milvus_url


def is_lite() -> bool:
    """True when the vector backend is embedded Milvus Lite (a local file uri).

    Milvus Lite (no-Docker local profile) does **not** support SPARSE_FLOAT_VECTOR
    / SPARSE_INVERTED_INDEX / hybrid search / IVF / scalar INVERTED indexes, so the
    KB degrades to **dense-only** on it. A server uri (``http(s)://`` / ``tcp://``)
    is a full Milvus and keeps the dense+sparse hybrid path byte-for-byte unchanged.
    """
    return not milvus_uri().startswith(("http://", "https://", "tcp://", "unix:"))


def get_client():
    """Return a MilvusClient connected to the configured Milvus instance."""
    from pymilvus import MilvusClient

    url = milvus_uri()
    token = os.getenv("MILVUS_TOKEN", "")
    if token:
        return MilvusClient(uri=url, token=token)
    return MilvusClient(uri=url)


def upsert(client, data: list[dict[str, Any]]) -> None:
    """Upsert helper that drops the ``sparse_embedding`` field on Milvus Lite.

    On Lite the collection has no sparse field (see ``get_or_create_collection``),
    and dynamic fields are disabled, so an extra ``sparse_embedding`` key would be
    rejected. Stripping it here keeps every caller's row-building code identical
    across both backends.
    """
    if is_lite():
        data = [{k: v for k, v in row.items() if k != "sparse_embedding"} for row in data]
    client.upsert(collection_name=kb_vector_state.COLLECTION_NAME, data=data)


def collection_dense_dim(client, *, timeout: float | None = None) -> Optional[int]:
    """Read the dense_embedding dimension of the existing collection.

    Connection and schema-read errors deliberately propagate to the caller. Treating
    an unavailable schema as a dimension mismatch used to trigger a destructive
    collection rebuild.
    """
    desc = client.describe_collection(kb_vector_state.COLLECTION_NAME, timeout=timeout)
    for f in desc.get("fields", []):
        if f.get("name") == "dense_embedding":
            dim = f.get("params", {}).get("dim")
            return int(dim) if dim is not None else None
    return None


def get_or_create_collection(*, timeout: float | None = None) -> None:
    """Idempotently create hugagent_kb_private (hybrid retrieval schema) at the current embedding model's real dimension.

    New collections use the current embedding model's detected dimension. Existing
    collections are never dropped implicitly: an unreadable schema or dimension
    mismatch preserves all vectors and aborts the operation until an operator can
    verify the embedding configuration and explicitly re-index the collection.
    """
    pass  # State is owned by its responsibility module.
    from pymilvus import DataType

    client = get_client()
    target_dim = vector_embedding.detect_embed_dim()

    if client.has_collection(kb_vector_state.COLLECTION_NAME, timeout=timeout):
        if kb_vector_state.VERIFIED_COLLECTION_DIM == target_dim:
            return
        try:
            existing_dim = collection_dense_dim(client, timeout=timeout)
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

    lite = is_lite()

    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("chunk_id", DataType.VARCHAR, max_length=64, is_primary=True)
    schema.add_field("parent_chunk_id", DataType.VARCHAR, max_length=64)
    schema.add_field("row_type", DataType.VARCHAR, max_length=16)  # "chunk" | "question"
    schema.add_field("user_id", DataType.VARCHAR, max_length=64)
    schema.add_field("kb_id", DataType.VARCHAR, max_length=64)
    schema.add_field("document_id", DataType.VARCHAR, max_length=64)
    schema.add_field("title", DataType.VARCHAR, max_length=kb_vector_state.TITLE_FIELD_MAX_BYTES)
    schema.add_field("content", DataType.VARCHAR, max_length=kb_vector_state.CONTENT_FIELD_MAX_BYTES)
    schema.add_field(
        "tags_text", DataType.VARCHAR, max_length=kb_vector_state.TAGS_FIELD_MAX_BYTES
    )  # BM25 augmentation
    schema.add_field("chunk_index", DataType.INT64)
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
        collection_name=kb_vector_state.COLLECTION_NAME,
        schema=schema,
        index_params=index_params,
        timeout=timeout,
    )
    kb_vector_state.VERIFIED_COLLECTION_DIM = target_dim
    logger.info(
        "Created Milvus collection: %s (dim=%d, mode=%s)",
        kb_vector_state.COLLECTION_NAME,
        target_dim,
        "lite/dense-only" if lite else "hybrid",
    )
