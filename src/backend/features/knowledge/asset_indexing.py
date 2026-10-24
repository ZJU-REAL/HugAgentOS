"""asset indexing responsibilities."""
from __future__ import annotations
from typing import Sequence
import logging
logger = logging.getLogger(__name__)
from features.knowledge import kb_assets_state

def asset_index_text(asset, *, title: str = "") -> str:
    """The text an asset is retrieved by: 视觉桥概述 + 图注/OCR 转写 + 文档标题。

    Audio/video reuse this unchanged — ``text_content`` becomes the ASR transcript.
    """
    parts = [
        (asset.caption or "").strip(),
        (asset.text_content or "").strip(),
        (title or "").strip(),
    ]
    return "\n".join(p for p in parts if p)


def index_assets(
    assets: Sequence, *, user_id: str, kb_id: str, document_id: str, title: str
) -> int:
    """Write one ``row_type="image"`` row per asset into the existing collection.

    ``parent_chunk_id`` points at the chunk that contains the asset, so a hit here
    resolves to that chunk's full text through the existing parent-fetch path. Assets
    with no owning chunk (a standalone upload whose placeholder was lost) point at
    themselves and fall back to the caption as their content.
    """
    if not assets:
        return 0

    from features.knowledge.kb_vector_state import TAGS_FIELD_MAX_BYTES, TITLE_FIELD_MAX_BYTES
    from core.models.embedding import embed_batch
    from features.knowledge.kb_vector import text_to_sparse
    from features.knowledge.vector_fields import truncate_utf8
    from features.knowledge.vector_indexing import upsert_rows

    indexable = [(a, asset_index_text(a, title=title)) for a in assets]
    indexable = [(a, t) for a, t in indexable if t.strip()]
    if not indexable:
        return 0

    try:
        vectors = embed_batch([t for _, t in indexable])
    except Exception as exc:
        logger.error("资产向量化失败，文档 %s 的图片不可检索: %s", document_id, exc)
        return 0

    rows = []
    for (asset, text), vector in zip(indexable, vectors):
        rows.append(
            {
                "chunk_id": f"{kb_assets_state.ASSET_ROW_PREFIX}{asset.asset_id}",
                "parent_chunk_id": asset.chunk_id or f"{kb_assets_state.ASSET_ROW_PREFIX}{asset.asset_id}",
                "row_type": kb_assets_state.ROW_TYPE_IMAGE,
                "user_id": user_id,
                "kb_id": kb_id,
                "document_id": document_id,
                "title": truncate_utf8(title, TITLE_FIELD_MAX_BYTES),
                "content": truncate_utf8(text),
                "tags_text": truncate_utf8(asset.text_content or "", TAGS_FIELD_MAX_BYTES),
                "chunk_index": asset.asset_index or 0,
                "dense_embedding": vector,
                "sparse_embedding": text_to_sparse(text),
            }
        )

    BATCH = 100
    for i in range(0, len(rows), BATCH):
        upsert_rows(rows[i : i + BATCH])
    logger.info("文档 %s 写入 %d 条资产检索行", document_id, len(rows))
    return len(rows)


def mark_vector_state(db, assets: Sequence, space: str, state: str) -> None:
    """Record that these assets are indexed into a vector space.

    ``space`` is ``"text"`` for the caption/OCR rows written by :func:`index_assets`.
    路径 B 落地时用 ``"visual"``，与文本行互不覆盖。
    """
    for row in assets:
        current = dict(row.vector_state or {})
        current[space] = state
        # 同上：赋新字典而不是原地改键，属性事件自然捕获得到。
        row.vector_state = current
    db.commit()
