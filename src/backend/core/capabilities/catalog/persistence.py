"""Durable administrator choices for catalogs using object storage."""
from __future__ import annotations

import os
from copy import deepcopy

BLOCK_ID = "catalog_admin_choices"
KINDS = ("skills", "agents", "mcp", "kb")


def enabled() -> bool:
    return os.getenv("STORAGE_TYPE", "local").lower() in ("s3", "oss", "obs")


def save(data):
    from core.db.engine import SessionLocal
    from core.db.models import ContentBlock
    from core.infra.time import utc_now

    # Descriptions and executable configuration remain owned by their sources.
    choices = {
        kind: [{"id": str(item["id"]), "enabled": bool(item.get("enabled"))}
               for item in data.get(kind, []) if isinstance(item, dict) and item.get("id")]
        for kind in KINDS
    }
    with SessionLocal() as db:
        row = db.get(ContentBlock, BLOCK_ID)
        if row is None:
            db.add(ContentBlock(id=BLOCK_ID, payload=choices, updated_at=utc_now()))
        else:
            row.payload = choices
            row.updated_at = utc_now()
        db.commit()


def apply(data):
    from core.db.engine import SessionLocal
    from core.db.models import ContentBlock

    with SessionLocal() as db:
        row = db.get(ContentBlock, BLOCK_ID)
        choices = deepcopy(row.payload) if row else {}
    if not isinstance(choices, dict):
        raise ValueError("Invalid persisted catalog choices")
    for kind in KINDS:
        by_id = {str(item["id"]): item for item in data.get(kind, [])}
        ordered = []
        for choice in choices.get(kind, []):
            item = by_id.pop(str(choice["id"]), None)
            if item is not None:
                item["enabled"] = bool(choice["enabled"])
                ordered.append(item)
        data[kind] = ordered + list(by_id.values())
    return data
