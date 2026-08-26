"""Revisioned ProfileMemory store.

This module is the only place that performs profile read-modify-write.  Every
commit compares the revision it read, so a stale updater cannot overwrite a
field committed by another worker while an LLM compaction was in flight.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Generic, TypeVar

from core.db.engine import SessionLocal
from core.db.models import ProfileMemory
from sqlalchemy.exc import IntegrityError

T = TypeVar("T")


@dataclass(frozen=True)
class ProfileSnapshot:
    user_id: str
    workspace_id: str
    content_md: str
    revision: int
    effect_receipts: dict = field(default_factory=dict)
    exists: bool = True


@dataclass(frozen=True)
class ProfileMutation(Generic[T]):
    value: T
    snapshot: ProfileSnapshot
    changed: bool
    replayed: bool = False


class ProfileConflictError(RuntimeError):
    pass


def read_snapshot(user_id: str, workspace_id: str = "default") -> ProfileSnapshot:
    with SessionLocal() as db:
        row = db.query(ProfileMemory).filter_by(user_id=user_id, workspace_id=workspace_id).first()
        if row is None:
            return ProfileSnapshot(
                user_id=user_id,
                workspace_id=workspace_id,
                content_md="",
                revision=-1,
                effect_receipts={},
                exists=False,
            )
        return ProfileSnapshot(
            user_id=user_id,
            workspace_id=workspace_id,
            content_md=row.content_md or "",
            revision=int(row.revision or 0),
            effect_receipts=dict(row.effect_receipts or {}),
        )


def commit_snapshot(
    snapshot: ProfileSnapshot,
    content_md: str,
    *,
    compacted: bool = False,
    effect_receipts: dict | None = None,
) -> bool:
    """Commit only if the durable revision still matches ``snapshot``."""

    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        if not snapshot.exists:
            row = ProfileMemory(
                user_id=snapshot.user_id,
                workspace_id=snapshot.workspace_id,
                content_md=content_md,
                revision=0,
                effect_receipts=dict(effect_receipts or snapshot.effect_receipts or {}),
                updated_at=now,
                last_compacted_at=now if compacted else None,
            )
            db.add(row)
            try:
                db.commit()
                return True
            except IntegrityError:
                db.rollback()
                return False

        fields = {
            "content_md": content_md,
            "revision": snapshot.revision + 1,
            "effect_receipts": dict(effect_receipts or snapshot.effect_receipts or {}),
            "updated_at": now,
        }
        if compacted:
            fields["last_compacted_at"] = now
        affected = (
            db.query(ProfileMemory)
            .filter(
                ProfileMemory.user_id == snapshot.user_id,
                ProfileMemory.workspace_id == snapshot.workspace_id,
                ProfileMemory.revision == snapshot.revision,
            )
            .update(fields, synchronize_session=False)
        )
        db.commit()
        return bool(affected)


def mutate_profile(
    user_id: str,
    workspace_id: str,
    transform: Callable[[str], tuple[str, T]],
    *,
    max_retries: int = 8,
    effect_id: str | None = None,
) -> ProfileMutation[T]:
    """Apply ``transform`` to the freshest profile and CAS its result.

    ``transform`` may be called again after a conflict, so it must be pure with
    respect to its input.  Its second return value is passed back to the caller.
    """

    for _attempt in range(max_retries):
        snapshot = read_snapshot(user_id, workspace_id)
        if effect_id and effect_id in snapshot.effect_receipts:
            return ProfileMutation(
                value=snapshot.effect_receipts[effect_id],
                snapshot=snapshot,
                changed=False,
                replayed=True,
            )
        content_md, value = transform(snapshot.content_md)
        receipts = dict(snapshot.effect_receipts)
        if effect_id:
            receipts[effect_id] = value
            if len(receipts) > 200:
                receipts = dict(list(receipts.items())[-200:])
        if content_md == snapshot.content_md and receipts == snapshot.effect_receipts:
            return ProfileMutation(value=value, snapshot=snapshot, changed=False)
        if commit_snapshot(snapshot, content_md, effect_receipts=receipts):
            return ProfileMutation(
                value=value,
                snapshot=ProfileSnapshot(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    content_md=content_md,
                    revision=snapshot.revision + 1,
                    effect_receipts=receipts,
                ),
                changed=True,
            )
    raise ProfileConflictError(f"profile CAS retries exhausted for {user_id}/{workspace_id}")


def delete_profile(user_id: str, workspace_id: str) -> bool:
    with SessionLocal() as db:
        affected = (
            db.query(ProfileMemory)
            .filter_by(user_id=user_id, workspace_id=workspace_id)
            .delete(synchronize_session=False)
        )
        db.commit()
        return bool(affected)
