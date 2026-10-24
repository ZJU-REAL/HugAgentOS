"""kb assets responsibilities."""
from __future__ import annotations
import os
import uuid
from typing import Iterable, Optional, Sequence
from features.knowledge.parser_types import RawAsset
from features.knowledge.parser_extract import image_extension_for_mime
import logging
logger = logging.getLogger(__name__)
from features.knowledge import kb_assets_state

def multimodal_indexing_enabled(indexing_config: Optional[dict]) -> bool:
    """Whether to extract and index media for this document.

    Per-KB opt-out via ``indexing_config.multimodal_indexing``; the global default
    lives in ``KB_MULTIMODAL_INDEXING`` so an operator can turn the extra parse cost
    off fleet-wide without touching each knowledge base.
    """
    if isinstance(indexing_config, dict) and indexing_config.get("multimodal_indexing") is not None:
        return bool(indexing_config.get("multimodal_indexing"))
    return (os.getenv("KB_MULTIMODAL_INDEXING", "true") or "").strip().lower() not in (
        "false",
        "0",
        "no",
    )


def asset_public_url(asset_id: str) -> str:
    """Frontend-resolvable URL for an asset. Matches the route in ``api/routes/v1/kb.py``.

    The ``/api`` prefix mirrors the frontend's ``VITE_API_BASE_URL`` default (nginx
    strips it before the backend sees the path). It is written into stored chunk text,
    so a deployment that serves the API elsewhere overrides ``KB_ASSET_URL_PREFIX``
    before indexing rather than rewriting rows afterwards.
    """
    prefix = (os.getenv("KB_ASSET_URL_PREFIX", "/api") or "").rstrip("/")
    return f"{prefix}/v1/catalog/kb/assets/{asset_id}"


def is_worth_indexing(asset: RawAsset) -> bool:
    """Filter out layout noise (rules, bullets, logos) before it costs a vision call."""
    if len(asset.data) < kb_assets_state.MIN_ASSET_BYTES:
        return False
    try:
        import io

        from PIL import Image  # optional dependency (transitive via python-pptx)

        with Image.open(io.BytesIO(asset.data)) as im:
            width, height = im.size
        if min(width, height) < kb_assets_state.MIN_ASSET_EDGE_PX:
            return False
    except Exception:
        # Pillow missing or the bytes are not a decodable image — the byte-size floor
        # above is the fallback filter; never drop an asset because probing failed.
        pass
    return True


def locate_parent_chunk(parents: Sequence, placeholder: str) -> Optional[object]:
    """Find the parent chunk whose text references this asset."""
    for parent in parents:
        if placeholder and placeholder in (parent.content or ""):
            return parent
    return None


def rewrite_placeholder(parents: Sequence, placeholder: str, url: str) -> None:
    """Point the markdown image link at the stored asset.

    Before this, ``![](images/<sha>.jpg)`` in a chunk was a dead link — the layout
    service's temp path, with the bytes discarded. Rewriting in both the parent text
    and its children keeps the stored chunk, the embedded text, and what the UI renders
    consistent.
    """
    needle = f"]({placeholder})"
    replacement = f"]({url})"
    for parent in parents:
        if needle in (parent.content or ""):
            parent.content = parent.content.replace(needle, replacement)
        for child in getattr(parent, "children", []) or []:
            if needle in (child.content or ""):
                child.content = child.content.replace(needle, replacement)


def persist_assets(
    db,
    *,
    kb_id: str,
    document_id: str,
    user_id: str,
    assets: Sequence[RawAsset],
    parents: Sequence,
) -> list:
    """Store asset bytes, create ``kb_assets`` rows, and link them to parent chunks.

    Must run **before** the parent chunks are embedded and written: it rewrites the
    placeholder links inside ``parents`` in place, and the rewritten text is what gets
    stored in PostgreSQL and vectorised.

    Failures are per-asset — a storage error on one figure must not fail the document,
    whose text is already parsed and perfectly usable.
    """
    from features.knowledge.models import KBAsset
    from core.files.storage import generate_storage_key, get_storage

    if not assets:
        return []

    env = os.getenv("ENVIRONMENT", "dev")
    storage = get_storage()
    created: list = []

    for asset in assets:
        if not is_worth_indexing(asset):
            continue

        asset_id = uuid.uuid4().hex[:32]
        suffix = image_extension_for_mime(asset.mime_type) if asset.kind == "image" else ".bin"
        storage_key = generate_storage_key(
            env=env,
            user_id=user_id,
            category="kb_assets",
            filename=f"{asset_id}{suffix}",
        )
        try:
            storage.upload_bytes(asset.data, storage_key)
        except Exception as exc:
            logger.warning("资产 %s 落盘失败，跳过: %s", asset.placeholder, exc)
            continue

        parent = locate_parent_chunk(parents, asset.placeholder)
        row = KBAsset(
            asset_id=asset_id,
            kb_id=kb_id,
            document_id=document_id,
            chunk_id=getattr(parent, "parent_id", None),
            kind=asset.kind,
            mime_type=asset.mime_type,
            storage_key=storage_key,
            size_bytes=len(asset.data),
            asset_index=asset.index,
            locator=asset.locator or {},
            text_content=asset.text_content or "",
            caption_status="pending",
            vector_state={},
            extra_data={"placeholder": asset.placeholder},
        )
        db.add(row)
        created.append(row)
        rewrite_placeholder(parents, asset.placeholder, asset_public_url(asset_id))

    if created:
        db.commit()
        for row in created:
            db.refresh(row)
        logger.info("文档 %s 抽出并落盘 %d 个资产", document_id, len(created))
    return created


def purge_document_assets(db, document_id: str) -> int:
    """Delete a document's assets (storage objects + rows). Used on re-index."""
    from features.knowledge.models import KBAsset

    rows = db.query(KBAsset).filter(KBAsset.document_id == document_id).all()
    if not rows:
        return 0

    try:
        from core.files.storage import get_storage

        storage = get_storage()
        for row in rows:
            try:
                storage.delete(row.storage_key)
            except Exception as exc:
                logger.warning("资产 %s 存储清理失败: %s", row.asset_id, exc)
    except Exception as exc:
        logger.warning("存储后端不可用，仅清理资产表: %s", exc)

    count = db.query(KBAsset).filter(KBAsset.document_id == document_id).delete()
    db.commit()
    return count


def fetch_assets_for_chunks(chunk_ids: Iterable[str], *, max_per_chunk: int = 4) -> dict:
    """Map parent-chunk id → its assets, for attaching to retrieval results.

    Also keyed by the synthetic ``a_<asset_id>`` parent used when an asset has no
    owning chunk, so a hit on such a row still resolves to its own image.
    """
    ids = [c for c in dict.fromkeys(chunk_ids) if c]
    if not ids:
        return {}

    direct = [c for c in ids if not c.startswith(kb_assets_state.ASSET_ROW_PREFIX)]
    orphan_asset_ids = [c[len(kb_assets_state.ASSET_ROW_PREFIX) :] for c in ids if c.startswith(kb_assets_state.ASSET_ROW_PREFIX)]

    try:
        from core.db.engine import SessionLocal
        from features.knowledge.models import KBAsset
        from sqlalchemy import or_

        with SessionLocal() as db:
            clauses = []
            if direct:
                clauses.append(KBAsset.chunk_id.in_(direct))
            if orphan_asset_ids:
                clauses.append(KBAsset.asset_id.in_(orphan_asset_ids))
            if not clauses:
                return {}
            rows = db.query(KBAsset).filter(or_(*clauses)).order_by(KBAsset.asset_index).all()
            grouped: dict[str, list[dict]] = {}
            for row in rows:
                key = row.chunk_id or f"{kb_assets_state.ASSET_ROW_PREFIX}{row.asset_id}"
                bucket = grouped.setdefault(key, [])
                if len(bucket) >= max_per_chunk:
                    continue
                bucket.append(
                    {
                        "asset_id": row.asset_id,
                        "kind": row.kind,
                        "url": asset_public_url(row.asset_id),
                        "caption": row.caption or row.text_content or "",
                        "locator": row.locator or {},
                    }
                )
            return grouped
    except Exception as exc:
        logger.warning("检索结果附图失败（不影响文本结果）: %s", exc)
        return {}
