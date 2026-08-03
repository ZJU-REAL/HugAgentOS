"""Shadow mapping between external memory ids and stable local refs (GCE ticket 01).

L2/L3 memories are owned by the vector / graph store, which renumbers them on
merge and update.  Attribution needs to look at a run from weeks ago and say
"this memory was injected" — so it cannot key on an id the store is free to
change.  This module pins a content-derived ``ref_id`` that stays valid across
id churn, and records the external id alongside it purely as a breadcrumb.

Every entry point here is best-effort: retrieval sits on the user's critical
path and a bookkeeping failure must never surface as a failed turn.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Iterable, List, Optional

from core.db.engine import SessionLocal
from core.db.models import MemoryRefShadow
from core.memory.retrieval_types import (
    LAYER_GRAPH,
    MemoryRetrievalResult,
    RetrievedMemory,
)

logger = logging.getLogger(__name__)

_PREVIEW_MAX = 160


def build_ref_id(*, layer: str, user_id: str, workspace_id: str, content_hash: str) -> str:
    """Deterministic ref id — same content always yields the same ref."""
    raw = f"{layer}|{user_id}|{workspace_id or 'default'}|{content_hash}"
    return "mref_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _preview(text: str) -> str:
    """Short, sanitized excerpt.

    Classified content is dropped entirely rather than truncated — a preview is
    a convenience for the console, never a reason to let restricted material
    leak into a second table.
    """
    collapsed = " ".join((text or "").split())
    if not collapsed:
        return ""
    try:
        from core.memory.sanitizer import sanitize

        result = sanitize(collapsed)
        if result.reject:
            return "[REDACTED]"
        collapsed = result.text or collapsed
    except Exception:  # pragma: no cover - sanitizer is optional at this layer
        pass
    return collapsed[:_PREVIEW_MAX]


def record_retrieved_refs(
    result: MemoryRetrievalResult,
    *,
    user_id: str,
    workspace_id: str = "default",
    episode_id: Optional[str] = None,
) -> List[str]:
    """Upsert shadow rows for everything this retrieval surfaced.

    Returns the ref ids in injection order so the caller can attach them to a
    trace event without a second query.
    """
    if result.is_empty:
        return []

    refs: List[str] = []
    try:
        with SessionLocal() as db:
            for item in result.items:
                ref_id = _upsert(
                    db,
                    layer=item.layer,
                    user_id=user_id,
                    workspace_id=item.workspace_id or workspace_id,
                    content_hash=item.content_hash,
                    external_id=item.memory_id,
                    preview=_preview(item.content),
                    episode_id=episode_id,
                )
                refs.append(ref_id)

            for relation in result.relations:
                if not relation.is_complete:
                    continue
                ref_id = _upsert(
                    db,
                    layer=LAYER_GRAPH,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    content_hash=relation.content_hash,
                    # The graph store's stable relation id (grel_*). Without it
                    # an L3 evidence ref cannot be resolved back to the Neo4j
                    # edge it came from, which makes graph evidence unauditable.
                    external_id=relation.relation_id or None,
                    preview=_preview(
                        f"{relation.source} → {relation.relationship} → {relation.target}"
                    ),
                    episode_id=episode_id,
                )
                refs.append(ref_id)
            db.commit()
    except Exception as exc:
        logger.debug("[memory-ref] shadow upsert failed, ignoring: %s", exc)
        return []
    return refs


def _upsert(
    db,
    *,
    layer: str,
    user_id: str,
    workspace_id: str,
    content_hash: str,
    external_id: Optional[str],
    preview: str,
    episode_id: Optional[str],
) -> str:
    ref_id = build_ref_id(
        layer=layer,
        user_id=user_id,
        workspace_id=workspace_id,
        content_hash=content_hash,
    )
    now = datetime.utcnow()
    row = db.query(MemoryRefShadow).filter(MemoryRefShadow.ref_id == ref_id).first()
    if row is None:
        db.add(
            MemoryRefShadow(
                ref_id=ref_id,
                layer=layer,
                user_id=user_id,
                workspace_id=workspace_id or "default",
                content_hash=content_hash,
                external_id=external_id,
                content_preview=preview,
                first_seen_episode_id=episode_id,
                first_seen_at=now,
                last_seen_at=now,
                seen_count=1,
            )
        )
        return ref_id

    row.last_seen_at = now
    row.seen_count = (row.seen_count or 0) + 1
    # Track id churn: the newest external id wins, but the ref itself is stable.
    if external_id and row.external_id != external_id:
        row.external_id = external_id
    if episode_id and not row.first_seen_episode_id:
        row.first_seen_episode_id = episode_id
    return ref_id


def resolve_ref(
    *,
    user_id: str,
    content_hash: str,
    layer: str,
    workspace_id: str = "default",
) -> Optional[MemoryRefShadow]:
    """Recover the shadow row for a memory by content, ignoring external ids."""
    ref_id = build_ref_id(
        layer=layer, user_id=user_id, workspace_id=workspace_id, content_hash=content_hash
    )
    try:
        with SessionLocal() as db:
            return db.query(MemoryRefShadow).filter(MemoryRefShadow.ref_id == ref_id).first()
    except Exception as exc:
        logger.debug("[memory-ref] resolve failed: %s", exc)
        return None


def resolve_by_external_id(external_id: str) -> Optional[MemoryRefShadow]:
    """Look up by the store's own id.

    Only useful while the id is still current — this is the lookup that breaks
    when the store renumbers, which is precisely why ``resolve_ref`` exists.
    """
    if not external_id:
        return None
    try:
        with SessionLocal() as db:
            return (
                db.query(MemoryRefShadow)
                .filter(MemoryRefShadow.external_id == external_id)
                .order_by(MemoryRefShadow.last_seen_at.desc())
                .first()
            )
    except Exception as exc:
        logger.debug("[memory-ref] resolve by external id failed: %s", exc)
        return None


def refs_for_items(items: Iterable[RetrievedMemory], *, user_id: str) -> List[str]:
    """Ref ids for already-retrieved items, without touching the database."""
    return [
        build_ref_id(
            layer=item.layer,
            user_id=user_id,
            workspace_id=item.workspace_id,
            content_hash=item.content_hash,
        )
        for item in items
    ]
