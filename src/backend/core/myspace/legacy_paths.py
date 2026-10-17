"""Plan and apply identity-preserving repairs of historical path-shaped filenames."""

from functools import wraps
from pathlib import PurePosixPath

from sqlalchemy.exc import OperationalError
from core.db.models import Artifact, UserFolder, UserShadow
from core.db.personal_file_names import check_name, validate_filename
from core.services.artifact_edition import personal_artifact_predicates
from core.services.folder_batch import lock_folder_owner
from core.space_sync.personal_metadata import domain
from fastapi import HTTPException


def _maintenance_transaction(operation):
    """Owner-first maintenance never waits for artifact-first application writers."""

    @wraps(operation)
    def run(db, plan):
        try:
            return operation(db, plan)
        except OperationalError as exc:
            db.rollback()
            original = exc.orig
            code = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
            if code == "55P03" or "database is locked" in str(original):
                raise ValueError(
                    "Space is being modified; retry the repair after the writer finishes"
                ) from exc
            raise

    return run


def _folder_path(db, user_id, folder_id):
    names, seen = [], set()
    while folder_id:
        if folder_id in seen:
            raise ValueError("Cyclic folder hierarchy")
        seen.add(folder_id)
        row = db.get(UserFolder, folder_id, populate_existing=True)
        if row is None or row.user_id != user_id or row.deleted_at is not None:
            raise ValueError("Missing, deleted or foreign ancestor")
        validate_filename(row.name)
        names.append(row.name)
        folder_id = row.parent_folder_id
    return "/".join(reversed(names))


def plan_repairs(db, user_id, *, lock=False):
    query = (
        db.query(Artifact)
        .filter(
            Artifact.user_id == user_id,
            Artifact.deleted_at.is_(None),
            *personal_artifact_predicates(Artifact),
        )
        .order_by(Artifact.artifact_id)
    )
    rows = (query.with_for_update(nowait=True) if lock else query).populate_existing().all()
    occupied = set()
    path_counts = {}
    candidates, skipped = [], []
    for row in rows:
        try:
            directory = _folder_path(db, user_id, row.user_folder_id)
            name = row.filename or ""
            parts = name.replace("\\", "/").split("/")
            for part in parts:
                validate_filename(part)
            path = "/".join(filter(None, (directory, "/".join(parts))))
            occupied.add(path)
            path_counts[path] = path_counts.get(path, 0) + 1
            if len(parts) > 1:
                candidates.append((row, path))
        except (ValueError, HTTPException) as exc:
            skipped.append({"artifact_id": row.artifact_id, "error": type(exc).__name__})
    directory_paths = set()
    for folder in db.query(UserFolder).filter_by(user_id=user_id, deleted_at=None):
        try:
            directory_path = _folder_path(db, user_id, folder.folder_id)
            occupied.add(directory_path)
            directory_paths.add(directory_path)
        except (ValueError, HTTPException):
            continue
    repairs = []
    for row, path in sorted(candidates, key=lambda pair: pair[0].artifact_id):
        directory, _, name = path.rpartition("/")
        parts = directory.split("/") if directory else []
        if any(
            "/".join(parts[:i]) in occupied and "/".join(parts[:i]) not in directory_paths
            for i in range(1, len(parts) + 1)
        ):
            skipped.append({"artifact_id": row.artifact_id, "error": "Ancestor is a live file"})
            continue
        # Never merge two identities or assume equal names mean equal bytes.
        suffix = PurePosixPath(name).suffix
        stem = name[: -len(suffix)] if suffix else name
        attempt = 0
        while True:
            if path_counts[path] == 1 and path not in directory_paths:
                candidate = name
                destination = path
                break
            tag = "__legacy_" + row.artifact_id + (f"_{attempt}" if attempt else "") + suffix
            prefix = stem.encode("utf-8")[: max(0, 255 - len(tag.encode("utf-8")))]
            candidate = prefix.decode("utf-8", errors="ignore") + tag
            destination = "/".join(filter(None, (directory, candidate)))
            if destination not in occupied:
                break
            attempt += 1
        occupied.add(destination)
        repairs.append(
            {
                "artifact_id": row.artifact_id,
                "old_filename": row.filename,
                "old_folder_id": row.user_folder_id,
                "old_storage_key": row.storage_key,
                "old_updated_at": row.updated_at.isoformat() if row.updated_at else None,
                "new_directory": directory,
                "new_filename": candidate,
            }
        )
    return {"user_id": user_id, "repairs": repairs, "skipped": skipped}


@_maintenance_transaction
def apply_repairs(db, plan):
    """Caller owns commit and writes the plan to protected storage before calling."""
    user_id = plan["user_id"]
    lock_folder_owner(db, UserShadow, "user_id", user_id)
    # Recompute under the owner lock; reject stale or edited plans before changes.
    if plan_repairs(db, user_id, lock=True) != plan:
        raise ValueError("Repair plan changed; preview again")
    from core.infra.time import utc_now

    for item in plan["repairs"]:
        row = db.get(Artifact, item["artifact_id"], populate_existing=True)
        folder = domain(user_id).directory(db, item["new_directory"])
        check_name(db, user_id, folder, item["new_filename"], exclude_id=row.artifact_id)
        before = {
            "filename": row.filename,
            "title": row.title,
            "user_folder_id": row.user_folder_id,
        }
        row.filename = item["new_filename"]
        row.user_folder_id = folder
        if row.title == item["old_filename"]:
            row.title = row.filename
        row.updated_at = utc_now()
        row.extra_data = {
            **(row.extra_data or {}),
            "legacy_path_repair": {"before": before, "repaired_at": row.updated_at.isoformat()},
        }
        db.flush()
    return len(plan["repairs"])


@_maintenance_transaction
def restore_repairs(db, plan):
    """Restore only rows unchanged since this repair; leave all object bytes intact."""
    from datetime import datetime
    from sqlalchemy import update

    user_id = plan["user_id"]
    lock_folder_owner(db, UserShadow, "user_id", user_id)
    restore = []
    for item in sorted(plan["repairs"], key=lambda item: item["artifact_id"]):
        row = (
            db.query(Artifact)
            .filter_by(artifact_id=item["artifact_id"], user_id=user_id)
            .populate_existing()
            .with_for_update(nowait=True)
            .one_or_none()
        )
        marker = (row.extra_data or {}).get("legacy_path_repair") if row else None
        if (
            row is None
            or row.user_id != user_id
            or row.deleted_at is not None
            or not marker
            or row.filename != item["new_filename"]
            or row.storage_key != item["old_storage_key"]
            or row.updated_at != datetime.fromisoformat(marker["repaired_at"])
        ):
            raise ValueError("Repaired file changed; rollback requires review")
        restore.append((row, marker, item))
    for row, marker, item in restore:
        meta = dict(row.extra_data or {})
        meta.pop("legacy_path_repair")
        # Maintenance-only Core update restores the historically invalid filename.
        table = Artifact.__table__
        result = db.connection().execute(
            update(table)
            .where(
                table.c.artifact_id == row.artifact_id,
                table.c.user_id == user_id,
                table.c.storage_key == item["old_storage_key"],
                table.c.updated_at == row.updated_at,
                table.c.filename == item["new_filename"],
                table.c.deleted_at.is_(None),
            )
            .values(
                {
                    **{table.c[key]: value for key, value in marker["before"].items()},
                    table.c.metadata: meta,
                    table.c.updated_at: datetime.fromisoformat(item["old_updated_at"]),
                }
            )
        )
        if result.rowcount != 1:
            raise ValueError("Repaired file changed during rollback")
    db.expire_all()
    return len(restore)
