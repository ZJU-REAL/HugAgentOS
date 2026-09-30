"""Cleanup only generated local E2E identities, including immutable versions."""

import asyncio
import hashlib
import shutil
import sqlite3
from pathlib import Path

from core.db.engine import SessionLocal
from core.db.models import Artifact, Project, Team, UserShadow
from core.sandbox._common import myspace_cache_dir
from core.storage import get_storage


async def cleanup(users, team_id, chats, clients):
    assert users and all(user.startswith("e2e_sync_") for user in users)
    assert team_id.startswith("e2e_sync_")
    errors = []
    from core.sandbox import get_sandbox_provider

    results = await asyncio.gather(
        *(get_sandbox_provider().close_session(chat) for chat in chats), return_exceptions=True
    )
    results += await asyncio.gather(
        *(client.aclose() for client in clients.values()), return_exceptions=True
    )
    errors.extend(result for result in results if isinstance(result, Exception))
    storage = get_storage()
    assert hasattr(storage, "base_path"), "This opt-in test requires local storage cleanup"
    base = storage.base_path.resolve()
    with SessionLocal() as db:
        artifacts = (
            db.query(Artifact)
            .filter((Artifact.user_id.in_(users)) | (Artifact.team_id == team_id))
            .all()
        )
        ids = [row.artifact_id for row in artifacts]
        keys = [row.storage_key for row in artifacts if row.storage_key]
        projects = [
            identity
            for identity, in db.query(Project.project_id).filter(Project.owner_user_id.in_(users))
        ]
    roots = [myspace_cache_dir(user) for user in users]
    namespaces = [hashlib.sha256(f"{user}:{team_id}".encode()).hexdigest() for user in users]
    roots += [base / "team_workspaces" / namespace for namespace in namespaces]
    roots += [base / "myspace" / user for user in users]
    roots += [base / "file_versions" / identity for identity in ids]
    roots += [base / "team_sources" / team_id]
    roots += [base / "project_sources" / identity for identity in projects]
    roots += [key / user for key in base.glob("*/") for user in users if (key / user).is_dir()]
    # Filesystem events finish while their temporary owners still exist.
    for root in roots:
        try:
            resolved = root.resolve()
            assert resolved != base and resolved.is_relative_to(base)
            if root.exists():
                shutil.rmtree(root)
        except Exception as exc:
            errors.append(exc)
    await asyncio.sleep(0.3)
    with SessionLocal() as db:
        db.query(Team).filter_by(team_id=team_id).delete(synchronize_session=False)
        db.query(UserShadow).filter(UserShadow.user_id.in_(users)).delete(synchronize_session=False)
        db.commit()
    for key in keys:
        try:
            storage.delete(key)
        except Exception as exc:
            errors.append(exc)
    for filename in ("personal.sqlite", "team.sqlite", "identities.sqlite"):
        path = base / "space_sync" / filename
        if not path.exists():
            continue
        with sqlite3.connect(path, timeout=10) as journal:
            if filename == "identities.sqlite":
                for user in users:
                    journal.execute("DELETE FROM identities WHERE owner=?", (user,))
                    journal.execute("DELETE FROM reflections WHERE owner=?", (user,))
            else:
                for namespace in users + namespaces:
                    journal.execute(
                        "DELETE FROM events WHERE substr(key,1,?)=?",
                        (len(namespace) + 1, namespace + ":"),
                    )
    if errors:
        raise RuntimeError(
            "E2E cleanup failures: " + ",".join(type(error).__name__ for error in errors)
        )
    print(
        "Temporary identities, sandboxes, mounts, historical objects and sync state removed",
        flush=True,
    )
