"""Edition-neutral knowledge retrieval facade."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from core.auth.kb_permissions import get_accessible_local_kb_ids, is_shared_visibility
from core.kb.external_retrieval import (
    MAX_RETRIEVE_TOKENS,
    RETRIEVE_MAX_CONCURRENCY,
    RETRIEVE_REQUEST_TIMEOUT_SECONDS,
    RETRIEVE_TOTAL_TIMEOUT_SECONDS,
    DatasetRetrievalTimeoutError,
    DatasetRetrievalUnavailableError,
    list_external_datasets,
    retrieve_dataset_content,
    retrieve_dataset_content_async,
)
from mcp_servers.retrieve_dataset_content_mcp.local_impl import (
    LOCAL_RETRIEVE_STAGE_TIMEOUT_SECONDS,
    LOCAL_RETRIEVE_TOTAL_TIMEOUT_SECONDS,
    LocalKnowledgeBaseTimeoutError,
    _build_runtime_local_kb_section,
    retrieve_local_kb,
)

_logger = logging.getLogger(__name__)


def _list_local_datasets(
    *, allowed_kb_ids: str | None, current_user_id: str | None
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    shared_items: List[Dict[str, Any]] = []
    private_items: List[Dict[str, Any]] = []
    try:
        from core.db.engine import SessionLocal
        from core.db.models import KBDocument, KBSpace

        allowed = {item.strip() for item in (allowed_kb_ids or "").split(",") if item.strip()}
        user_id = (current_user_id or "").strip() or os.getenv("CURRENT_USER_ID", "").strip()
        with SessionLocal() as db:
            query = db.query(KBSpace).filter(KBSpace.deleted_at.is_(None))
            if allowed:
                query = query.filter(KBSpace.kb_id.in_(allowed))
            elif user_id:
                accessible = get_accessible_local_kb_ids(db, user_id)
                query = query.filter(KBSpace.kb_id.in_(accessible or {"__none__"}))
            for space in query.all():
                docs = (
                    db.query(KBDocument)
                    .filter(
                        KBDocument.kb_id == space.kb_id,
                        KBDocument.deleted_at.is_(None),
                    )
                    .order_by(KBDocument.uploaded_at.desc())
                    .limit(20)
                    .all()
                )
                shared = is_shared_visibility(space.visibility)
                item = {
                    "kb_id": space.kb_id,
                    "name": space.name,
                    "description": space.description or "",
                    "document_count": space.document_count or len(docs),
                    "document_titles": [doc.title for doc in docs if doc.title],
                    "type": "public" if shared else "private",
                }
                (shared_items if shared else private_items).append(item)
    except Exception as exc:
        _logger.warning("Failed to list local knowledge bases: %s", exc)
    return shared_items, private_items


def list_all_datasets(
    *,
    allowed_dataset_ids: str | None = None,
    allowed_kb_ids: str | None = None,
    current_user_id: str | None = None,
) -> Dict[str, Any]:
    public_items = list_external_datasets(
        allowed_dataset_ids=allowed_dataset_ids,
        allowed_kb_ids=allowed_kb_ids,
        current_user_id=current_user_id,
    )
    local_shared, private_items = _list_local_datasets(
        allowed_kb_ids=allowed_kb_ids, current_user_id=current_user_id
    )
    public_items.extend(local_shared)
    return {
        "public_datasets": public_items,
        "private_datasets": private_items,
        "total": len(public_items) + len(private_items),
    }


__all__ = [
    "DatasetRetrievalTimeoutError",
    "DatasetRetrievalUnavailableError",
    "LOCAL_RETRIEVE_STAGE_TIMEOUT_SECONDS",
    "LOCAL_RETRIEVE_TOTAL_TIMEOUT_SECONDS",
    "LocalKnowledgeBaseTimeoutError",
    "MAX_RETRIEVE_TOKENS",
    "RETRIEVE_MAX_CONCURRENCY",
    "RETRIEVE_REQUEST_TIMEOUT_SECONDS",
    "RETRIEVE_TOTAL_TIMEOUT_SECONDS",
    "_build_runtime_local_kb_section",
    "list_all_datasets",
    "retrieve_dataset_content",
    "retrieve_dataset_content_async",
    "retrieve_local_kb",
]
