"""Verified repair of the legacy artifacts/<id> placeholder, never arbitrary keys."""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import PurePosixPath

from sqlalchemy import update

logger = logging.getLogger(__name__)


def verified_legacy_key(artifact, storage) -> str | None:
    """Recognize one historical defect and verify the replacement bytes and size."""
    old = f"artifacts/{artifact.artifact_id}"
    if artifact.storage_key != old or storage.exists(old):
        return None
    suffix = PurePosixPath(artifact.filename or "").suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,12}", suffix):
        return None
    candidate = old + suffix
    if not storage.exists(candidate):
        return None
    data = storage.download_bytes(candidate)
    if not artifact.size_bytes or len(data) != artifact.size_bytes:
        return None
    return candidate


def repair_legacy_key(db, artifact, storage) -> bool:
    """Compare-and-swap a verified key, keeping the content version unchanged.

    The audit entry is committed with the key so repair evidence cannot be lost.
    A rename, content update or competing repair prevents this write.
    """
    from core.db.models import Artifact

    candidate = verified_legacy_key(artifact, storage)
    if not candidate:
        return False
    old = artifact.storage_key
    data = storage.download_bytes(candidate)
    if len(data) != artifact.size_bytes:
        return False
    meta = dict(artifact.extra_data or {})
    meta["storage_key_repair"] = {
        "old_key": old,
        "new_key": candidate,
        "sha256": hashlib.sha256(data).hexdigest(),
        "content_version": artifact.updated_at.isoformat() if artifact.updated_at else None,
    }
    result = db.execute(
        update(Artifact)
        .where(
            Artifact.artifact_id == artifact.artifact_id,
            Artifact.storage_key == old,
            Artifact.filename == artifact.filename,
            Artifact.size_bytes == artifact.size_bytes,
            Artifact.updated_at == artifact.updated_at,
        )
        .values(storage_key=candidate, extra_data=meta, updated_at=artifact.updated_at)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    db.refresh(artifact)
    if result.rowcount:
        logger.info("Artifact storage key repaired artifact=%s old=%s new=%s", artifact.artifact_id, old, candidate)
    return bool(result.rowcount)


def rollback_legacy_key(db, artifact) -> bool:
    """Undo only an audited repair whose content has not changed since repair."""
    from core.db.models import Artifact

    meta = dict(artifact.extra_data or {})
    audit = meta.get("storage_key_repair") or {}
    version = artifact.updated_at.isoformat() if artifact.updated_at else None
    if (not audit or artifact.storage_key != audit.get("new_key")
            or version != audit.get("content_version")
            or audit.get("old_key") != f"artifacts/{artifact.artifact_id}"):
        return False
    meta.pop("storage_key_repair")
    result = db.execute(
        update(Artifact)
        .where(Artifact.artifact_id == artifact.artifact_id,
               Artifact.storage_key == audit["new_key"], Artifact.updated_at == artifact.updated_at)
        .values(storage_key=audit["old_key"], extra_data=meta, updated_at=artifact.updated_at)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    db.refresh(artifact)
    return bool(result.rowcount)
