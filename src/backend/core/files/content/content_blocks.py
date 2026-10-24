"""Export and import content blocks and the prompt pool as snapshots."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Mapping
from core.db.models import ContentBlock
from sqlalchemy.orm import Session
from core.files.content import branding
from core.files.content import content_blocks_constants

class ContentSnapshotError(ValueError):
    """Raised when a docs content snapshot is invalid."""


def serialize_block(row: ContentBlock | None, alias: str | None = None) -> dict[str, Any]:
    return {
        "payload": row.payload if row else branding.default_payload(alias or ""),
        "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
        "updated_by": row.updated_by if row else None,
    }


def build_docs_snapshot(
    db: Session,
    block_map: Mapping[str, str] = content_blocks_constants.DOCS_BLOCK_MAP,
) -> dict[str, Any]:
    """Build a portable JSON snapshot for content blocks.

    Defaults to the docs blocks; pass ``block_map=PROMPT_BLOCK_MAP`` (or use
    ``build_prompt_snapshot``) to snapshot the system prompt pool instead.
    """
    rows = db.query(ContentBlock).filter(ContentBlock.id.in_(list(block_map.values()))).all()
    row_map = {row.id: row for row in rows}

    return {
        "schema_version": content_blocks_constants.SNAPSHOT_SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "blocks": {
            alias: serialize_block(row_map.get(db_id), alias) for alias, db_id in block_map.items()
        },
    }


def parse_snapshot_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ContentSnapshotError("updated_at must be an ISO datetime string")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContentSnapshotError(f"Invalid datetime: {value}") from exc


def normalize_docs_snapshot(
    snapshot: Mapping[str, Any],
    block_map: Mapping[str, str] = content_blocks_constants.DOCS_BLOCK_MAP,
) -> dict[str, Any]:
    """Validate and normalize an incoming content snapshot."""
    if not isinstance(snapshot, Mapping):
        raise ContentSnapshotError("Snapshot body must be a JSON object")

    schema_version = snapshot.get("schema_version", content_blocks_constants.SNAPSHOT_SCHEMA_VERSION)
    if schema_version != content_blocks_constants.SNAPSHOT_SCHEMA_VERSION:
        raise ContentSnapshotError(
            f"Unsupported schema_version: {schema_version}. Expected {content_blocks_constants.SNAPSHOT_SCHEMA_VERSION}"
        )

    raw_blocks = snapshot.get("blocks")
    if not isinstance(raw_blocks, Mapping):
        raise ContentSnapshotError("Snapshot.blocks must be an object")

    unknown_blocks = set(raw_blocks.keys()) - set(block_map.keys()) - content_blocks_constants.RETIRED_BLOCK_ALIASES
    if unknown_blocks:
        raise ContentSnapshotError(
            f"Unknown blocks in snapshot: {', '.join(sorted(unknown_blocks))}"
        )

    normalized_blocks: dict[str, dict[str, Any]] = {}
    for alias in block_map:
        raw_block = raw_blocks.get(alias)
        if raw_block is None:
            continue
        if not isinstance(raw_block, Mapping):
            raise ContentSnapshotError(f"Snapshot block '{alias}' must be an object")

        payload = raw_block.get("payload", branding.default_payload(alias))
        expected_type = dict if alias in content_blocks_constants.DICT_PAYLOAD_BLOCKS else list
        if not isinstance(payload, expected_type):
            kind = "object" if expected_type is dict else "list"
            raise ContentSnapshotError(f"Snapshot block '{alias}'.payload must be a {kind}")

        normalized_blocks[alias] = {
            "payload": payload,
            "updated_at": parse_snapshot_datetime(raw_block.get("updated_at")),
            "updated_by": raw_block.get("updated_by"),
        }

    if not normalized_blocks:
        raise ContentSnapshotError("Snapshot does not contain any importable content blocks")

    exported_at = snapshot.get("exported_at")
    if exported_at not in (None, "") and not isinstance(exported_at, str):
        raise ContentSnapshotError("Snapshot.exported_at must be a string")

    return {
        "schema_version": content_blocks_constants.SNAPSHOT_SCHEMA_VERSION,
        "exported_at": exported_at,
        "blocks": normalized_blocks,
    }


def import_docs_snapshot(
    db: Session,
    snapshot: Mapping[str, Any],
    *,
    overwrite: bool = True,
    default_updated_by: str | None = None,
    block_map: Mapping[str, str] = content_blocks_constants.DOCS_BLOCK_MAP,
) -> dict[str, Any]:
    """Import a content snapshot into the current database."""
    normalized = normalize_docs_snapshot(snapshot, block_map=block_map)
    imported: list[str] = []
    skipped: list[str] = []

    for alias, block in normalized["blocks"].items():
        db_id = block_map[alias]
        row = db.query(ContentBlock).filter(ContentBlock.id == db_id).first()
        if row and not overwrite:
            skipped.append(alias)
            continue

        updated_at = block["updated_at"] or datetime.now(timezone.utc)
        updated_by = block["updated_by"] or default_updated_by

        if row:
            row.payload = block["payload"]
            row.updated_at = updated_at
            row.updated_by = updated_by
        else:
            row = ContentBlock(
                id=db_id,
                payload=block["payload"],
                updated_at=updated_at,
                updated_by=updated_by,
            )
            db.add(row)
        imported.append(alias)

    db.commit()

    return {
        "schema_version": normalized["schema_version"],
        "imported": imported,
        "skipped": skipped,
        "count": len(imported),
        "overwrite": overwrite,
    }


def build_prompt_snapshot(db: Session) -> dict[str, Any]:
    """Build a portable snapshot of the system prompt pool (prompt_versions).

    Same format as the docs snapshot, but scoped to PROMPT_BLOCK_MAP so it can
    be migrated across environments independently of docs/page config.
    """
    return build_docs_snapshot(db, block_map=content_blocks_constants.PROMPT_BLOCK_MAP)


def import_prompt_snapshot(
    db: Session,
    snapshot: Mapping[str, Any],
    *,
    overwrite: bool = True,
    default_updated_by: str | None = None,
) -> dict[str, Any]:
    """Import a system prompt pool snapshot (counterpart of build_prompt_snapshot)."""
    return import_docs_snapshot(
        db,
        snapshot,
        overwrite=overwrite,
        default_updated_by=default_updated_by,
        block_map=content_blocks_constants.PROMPT_BLOCK_MAP,
    )
