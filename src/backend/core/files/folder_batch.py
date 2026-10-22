"""Create a bounded manifest in one transaction; edition models are supplied by callers."""

import uuid

from core.db.repository import AuditLogRepository
from sqlalchemy import or_
from sqlalchemy.orm import Session

from core.db.repository.folder import lock_folder_owner


def create_folder_batch(
    db, *, model, owner_model, owner_column, owner_id, actor, paths, parent_folder_id=None
):
    expanded = set()
    for path in paths:
        parts = path.split("/")
        if any(
            not p or p != p.strip() or p in (".", "..") or "\\" in p or "\x00" in p or len(p) > 255
            for p in parts
        ):
            raise ValueError("文件夹路径非法")
        expanded.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    if len(expanded) > 1600:
        raise ValueError("文件夹数量超出批次上限")
    scope = getattr(model, owner_column) == owner_id
    try:
        lock_folder_owner(db, owner_model, owner_column, owner_id)
        current, seen = parent_folder_id, set()
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
        from core.files.spaces.files import SpaceFiles
        from core.files.spaces.projects import folder_path
        from core.files.edition import owner_space
        from core.files.storage import get_storage
        if owner_column == "user_id":
            if owner_id != actor:
                raise ValueError("Directory owner does not match the account")
            space = "personal"
        else:
            space = owner_space(owner_column, owner_id)
        base = folder_path(db, model, {owner_column: owner_id}, parent_folder_id)
        files = SpaceFiles(db, actor, get_storage())
        for path in sorted(expanded):
            location = "/".join(filter(None, [base, path]))
            grant = files.grant(space, write=True)
            entry = files.storage.stat_object(grant.key(location))
            if entry is None:
                files.mkdir(space, location)
            elif not entry.is_directory:
                raise ValueError("A file exists at the directory path")
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
