"""Create a bounded manifest in one transaction; edition models are supplied by callers."""

import hashlib
import uuid
from threading import Lock

from core.db.repository import AuditLogRepository
from sqlalchemy import event, or_
from sqlalchemy.orm import Session

_SQLITE_LOCKS = [Lock() for _ in range(64)]


def _release_sqlite_locks(session, transaction):
    if transaction.parent is None:
        for lock in session.info.pop("folder_upload_locks", []):
            lock.release()


def lock_folder_owner(db: Session, owner_model, owner_column: str, owner_id: str) -> None:
    if db.get_bind().dialect.name == "sqlite":
        # SQLite ignores FOR UPDATE. Its supported single-process deployment still
        # serves requests on multiple threads, so retain a bounded process lock
        # until commit/rollback/close, including early-return and error paths.
        if not db.in_transaction():
            db.begin()
        if not db.info.get("folder_upload_lock_listener"):
            event.listen(db, "after_transaction_end", _release_sqlite_locks)
            db.info["folder_upload_lock_listener"] = True
        digest = hashlib.sha256((owner_model.__tablename__ + ":" + owner_id).encode()).digest()
        lock = _SQLITE_LOCKS[int.from_bytes(digest[:2], "big") % len(_SQLITE_LOCKS)]
        held = db.info.setdefault("folder_upload_locks", [])
        if lock not in held:
            lock.acquire()
            held.append(lock)
    # NO KEY UPDATE does not conflict with FK KEY SHARE taken by artifact inserts.
    owner = (
        db.query(owner_model)
        .filter(getattr(owner_model, owner_column) == owner_id)
        .with_for_update(key_share=True)
        .first()
    )
    if owner is None:
        raise ValueError("文件夹所属空间不存在")


def create_folder_batch(
    db, *, model, owner_model, owner_column, owner_id, actor, paths, parent_folder_id=None
):
    expanded = set()
    for path in paths:
        parts = path.split("/")
        if len(parts) > 8 or any(
            not p or p != p.strip() or p in (".", "..") or "\\" in p or "\x00" in p or len(p) > 255
            for p in parts
        ):
            raise ValueError("文件夹路径非法或超过 8 级")
        expanded.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    if len(expanded) > 1600:
        raise ValueError("文件夹数量超出批次上限")
    scope = getattr(model, owner_column) == owner_id
    try:
        lock_folder_owner(db, owner_model, owner_column, owner_id)
        depth, current, seen = 0, parent_folder_id, set()
        while current:
            if current in seen:
                raise ValueError("文件夹层级非法")
            seen.add(current)
            parent = (
                db.query(model)
                .filter(scope, model.folder_id == current, model.deleted_at.is_(None))
                .populate_existing()
                .with_for_update()
                .first()
            )
            if parent is None:
                raise ValueError("目标文件夹不存在")
            current = parent.parent_folder_id
            depth += 1
        if depth + max(p.count("/") + 1 for p in expanded) > 8:
            raise ValueError("文件夹层级超过 8 级")
        mapping = {"": parent_folder_id}
        for level in sorted({p.count("/") for p in expanded}):
            level_paths = sorted(p for p in expanded if p.count("/") == level)
            # One sibling lookup per level, not a query/commit per directory.
            parents = {mapping[p.rpartition("/")[0]] for p in level_paths}
            conditions = [model.parent_folder_id.in_([p for p in parents if p is not None])]
            if None in parents:
                conditions.append(model.parent_folder_id.is_(None))
            existing = (
                db.query(model).filter(scope, model.deleted_at.is_(None), or_(*conditions)).all()
            )
            siblings = {(f.parent_folder_id, f.name): f for f in existing}
            for path in level_paths:
                parent_path, _, name = path.rpartition("/")
                parent_id = mapping[parent_path]
                row = siblings.get((parent_id, name))
                if row is None:
                    values = dict(
                        folder_id="fld_" + uuid.uuid4().hex, parent_folder_id=parent_id, name=name
                    )
                    values[owner_column] = owner_id
                    if hasattr(model, "created_by"):
                        values["created_by"] = actor
                    row = model(**values)
                    db.add(row)
                    siblings[(parent_id, name)] = row
                mapping[path] = row.folder_id
            db.flush()
        db.commit()
    except Exception:
        db.rollback()
        raise
    AuditLogRepository(db).create(
        dict(
            user_id=actor,
            action="folder.batch_create",
            resource_type="folder",
            resource_id=owner_id,
            details={"count": len(paths)},
            status="success",
        )
    )
    return {p: mapping[p] for p in paths}
