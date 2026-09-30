"""Transactional directory and rename semantics shared by the two mounted space domains."""

import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath

from core.db.personal_file_names import validate_filename
from core.infra.time import utc_now
from fastapi import HTTPException


@dataclass
class Domain:
    folder_model: object
    owner_column: str
    owner_id: str
    artifact_folder_column: str
    artifact_predicates: tuple
    actor: str

    def folders(self, db):
        model = self.folder_model
        rows = (
            db.query(model)
            .filter(getattr(model, self.owner_column) == self.owner_id, model.deleted_at.is_(None))
            .all()
        )
        by_id = {r.folder_id: r for r in rows}
        paths = {None: ""}

        def visit(fid, seen):
            if fid in paths:
                return paths[fid]
            row = by_id.get(fid)
            if row is None or fid in seen:
                raise HTTPException(409, "Invalid folder hierarchy")
            validate_filename(row.name)
            parent = visit(row.parent_folder_id, seen | {fid})
            paths[fid] = "/".join(filter(None, (parent, row.name)))
            return paths[fid]

        for fid in by_id:
            visit(fid, set())
        if len(set(paths.values())) != len(paths):
            raise HTTPException(409, "Duplicate folder paths")
        return {path: by_id.get(fid) for fid, path in paths.items()}

    def directory(self, db, path):
        if not path:
            return None
        parts = PurePosixPath(path).parts
        if len(parts) > 8 or path.startswith("/") or any(p in (".", "..") for p in parts):
            raise HTTPException(400, "Invalid folder path")
        mapping = self.folders(db)
        parent = None
        for i, part in enumerate(parts):
            validate_filename(part)
            current = "/".join(parts[: i + 1])
            row = mapping.get(current)
            if row is None:
                from core.db.models import Artifact

                duplicate = (
                    db.query(Artifact)
                    .filter(
                        *self.artifact_predicates,
                        getattr(Artifact, self.artifact_folder_column) == parent,
                        Artifact.filename == part,
                        Artifact.deleted_at.is_(None),
                    )
                    .first()
                )
                if duplicate:
                    raise HTTPException(409, "Destination already exists")
                values = dict(
                    folder_id="fld_" + uuid.uuid4().hex, name=part, parent_folder_id=parent
                )
                values[self.owner_column] = self.owner_id
                if hasattr(self.folder_model, "created_by"):
                    values["created_by"] = self.actor
                row = self.folder_model(**values)
                db.add(row)
                db.flush()
                mapping[current] = row
            parent = row.folder_id
        return parent

    def artifact(self, db, path):
        from core.db.models import Artifact

        directory, _, name = path.rpartition("/")
        mapping = self.folders(db)
        if directory not in mapping:
            return None
        folder = mapping[directory]
        return (
            db.query(Artifact)
            .filter(
                *self.artifact_predicates,
                getattr(Artifact, self.artifact_folder_column)
                == (folder.folder_id if folder else None),
                Artifact.filename == name,
                Artifact.deleted_at.is_(None),
            )
            .with_for_update()
            .populate_existing()
            .one_or_none()
        )

    def move(self, db, source, destination, directory=False):
        from core.db.models import Artifact

        if source == destination:
            return None
        if directory and destination.startswith(source + "/"):
            raise HTTPException(409, "Cannot move a folder into itself")
        parent, _, name = destination.rpartition("/")
        validate_filename(name)
        mapping = self.folders(db)
        row = mapping.get(source) if directory else self.artifact(db, source)
        if row is None:
            return None
        if destination in mapping or self.artifact(db, destination):
            raise HTTPException(409, "Destination already exists; local bytes retained")
        if directory:
            deepest = max(
                (p.count("/") + 1 for p in mapping if p == source or p.startswith(source + "/")),
                default=1,
            )
            if deepest - source.count("/") + destination.count("/") > 8:
                raise HTTPException(400, "Folder depth exceeds limit")
        parent_id = self.directory(db, parent)
        if directory:
            row.parent_folder_id = parent_id
            row.name = name
        else:
            setattr(row, self.artifact_folder_column, parent_id)
            row.filename = row.title = name
        row.updated_at = utc_now()
        db.flush()
        return row

    def delete_directory(self, db, path):
        from core.db.models import Artifact

        mapping = self.folders(db)
        rows = [r for p, r in mapping.items() if r and (p == path or p.startswith(path + "/"))]
        ids = [r.folder_id for r in rows]
        now = utc_now()
        for row in rows:
            row.deleted_at = now
        if ids:
            db.query(Artifact).filter(
                *self.artifact_predicates,
                getattr(Artifact, self.artifact_folder_column).in_(ids),
                Artifact.deleted_at.is_(None),
            ).update({Artifact.deleted_at: now}, synchronize_session=False)
        return len(ids)
