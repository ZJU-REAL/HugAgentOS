"""Load and cache the stored prompt pool for this worker."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from core.files.content.content_blocks_constants import DEFAULT_PROMPT_VERSIONS, PROMPT_VERSIONS_BLOCK_ID
from core.db.engine import SessionLocal
from core.db.models import ContentBlock
from sqlalchemy.orm import Session
from core.llm import prompt_versions_state

def load_payload(db: Optional[Session] = None) -> Dict[str, Any]:
    """Read the committed pool revision before reusing a worker-local payload.

    Workers do not share Python memory. Local invalidation alone leaves other
    workers serving old versions indefinitely, including on read-modify-write.
    Query columns directly so an existing ORM identity-map entry cannot hide a
    newer payload. The payload and its revision are loaded in the same query.
    """
    pass  # State is owned by its responsibility module.
    own_session = db is None
    if own_session:
        db = SessionLocal()
    try:
        revision = (
            db.query(ContentBlock.updated_at)
            .filter(ContentBlock.id == PROMPT_VERSIONS_BLOCK_ID)
            .first()
        )
        cache_key = (db.get_bind(), tuple(revision) if revision is not None else None)
        with prompt_versions_state.process_state().payload_cache_lock:
            if prompt_versions_state.process_state().payload_cache is not None and prompt_versions_state.process_state().payload_cache_key == cache_key:
                return clone(prompt_versions_state.process_state().payload_cache)

        row = (
            db.query(ContentBlock.updated_at, ContentBlock.payload)
            .filter(ContentBlock.id == PROMPT_VERSIONS_BLOCK_ID)
            .first()
        )
        payload = (
            row.payload
            if row is not None and isinstance(row.payload, dict)
            else clone(DEFAULT_PROMPT_VERSIONS)
        )
        # A write may have committed between the two SELECTs. Cache the revision
        # returned with the payload, never the earlier revision probe.
        cache_key = (db.get_bind(), (row.updated_at,) if row is not None else None)
        with prompt_versions_state.process_state().payload_cache_lock:
            prompt_versions_state.process_state().payload_cache = payload
            prompt_versions_state.process_state().payload_cache_key = cache_key
        return clone(payload)
    finally:
        if own_session:
            db.close()


def save_payload(
    payload: Dict[str, Any],
    *,
    db: Optional[Session] = None,
    updated_by: str = "system",
) -> None:
    own_session = db is None
    if own_session:
        db = SessionLocal()
    try:
        row = db.query(ContentBlock).filter(ContentBlock.id == PROMPT_VERSIONS_BLOCK_ID).first()
        now = datetime.now(timezone.utc)
        if row:
            row.payload = payload
            row.updated_at = now
            row.updated_by = updated_by
        else:
            row = ContentBlock(
                id=PROMPT_VERSIONS_BLOCK_ID,
                payload=payload,
                updated_at=now,
                updated_by=updated_by,
            )
            db.add(row)
        db.commit()
    finally:
        if own_session:
            db.close()

    invalidate_cache()


def invalidate_cache() -> None:
    """Drop this worker's cache; other workers detect the DB revision on read."""
    pass  # State is owned by its responsibility module.
    with prompt_versions_state.process_state().payload_cache_lock:
        prompt_versions_state.process_state().payload_cache = None
        prompt_versions_state.process_state().payload_cache_key = None


def clone(obj: Any) -> Any:
    import copy

    return copy.deepcopy(obj)
