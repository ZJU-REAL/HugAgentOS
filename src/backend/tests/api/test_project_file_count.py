"""Project file counts walk the linked folder's whole subtree from one children map."""

from __future__ import annotations

from core.infra.time import utc_now

import pytest
from sqlalchemy import event

from core.db.models import Artifact, Project, UserFolder
from core.projects.base import BaseProjectService


def _folder(db, folder_id, user_id, parent=None, *, deleted=False):
    db.add(
        UserFolder(
            folder_id=folder_id,
            user_id=user_id,
            parent_folder_id=parent,
            name=folder_id,
            deleted_at=utc_now() if deleted else None,
        )
    )


def _files(db, user_id, folder_id, n, *, deleted=False):
    for i in range(n):
        artifact_id = f"{folder_id}-{i}{'-del' if deleted else ''}"
        db.add(
            Artifact(
                artifact_id=artifact_id,
                user_id=user_id,
                user_folder_id=folder_id,
                type="document",
                title="t",
                filename=f"{artifact_id}.txt",
                size_bytes=1,
                mime_type="text/plain",
                storage_key="k",
                deleted_at=utc_now() if deleted else None,
            )
        )


def _project(db, project_id, owner, folder_id):
    db.add(
        Project(
            project_id=project_id,
            name=project_id,
            kind="personal",
            owner_user_id=owner,
            linked_folder_id=folder_id,
        )
    )


@pytest.fixture
def trees(db_session, alice, bob):
    db = db_session
    a, b = alice.user_id, bob.user_id
    # alice: A ─┬─ B ── C
    #           ├─ D
    #           └─ E (deleted)        F (unrelated root)
    _folder(db, "A", a)
    db.flush()
    _folder(db, "B", a, "A")
    _folder(db, "D", a, "A")
    _folder(db, "E", a, "A", deleted=True)
    _folder(db, "F", a)
    db.flush()
    _folder(db, "C", a, "B")
    # bob: X ── Y
    _folder(db, "X", b)
    db.flush()
    _folder(db, "Y", b, "X")
    db.flush()
    _files(db, a, "A", 1)
    _files(db, a, "B", 1)
    _files(db, a, "C", 2)
    _files(db, a, "C", 1, deleted=True)
    _files(db, a, "D", 1)
    _files(db, a, "E", 3)
    _files(db, a, "F", 5)
    _files(db, b, "X", 1)
    _files(db, b, "Y", 2)
    _project(db, "p_root", a, "A")
    _project(db, "p_deep", a, "C")
    _project(db, "p_none", a, None)
    _project(db, "p_bob", b, "X")
    db.commit()
    return a, b


def test_list_visible_counts_nested_folders(db_session, trees):
    alice_id, _ = trees
    statements: list[str] = []

    def _record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", _record)
    try:
        items, total = BaseProjectService(db_session).list_visible(alice_id)
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert total == 3
    assert {it["project_id"]: it["file_count"] for it in items} == {
        "p_root": 5,  # A + B + C×2 + D; deleted folder E and deleted artifact excluded
        "p_deep": 2,
        "p_none": 0,
    }
    # One query for the folder names, one for the children map: not one per folder level.
    assert sum("user_folders" in s for s in statements) == 2


def test_file_count_without_map_and_per_owner(db_session, trees):
    alice_id, bob_id = trees
    svc = BaseProjectService(db_session)
    projects = {p.project_id: p for p in db_session.query(Project).all()}

    assert svc._file_count(projects["p_root"]) == 5
    assert svc._file_count(projects["p_bob"]) == 3
    assert svc._file_count(projects["p_bob"], svc._folder_children(bob_id)) == 3
    assert sorted(svc._subtree_ids("A", alice_id)) == ["A", "B", "C", "D"]
    # Another owner's map never reaches into alice's tree.
    assert svc._subtree_ids("A", alice_id, svc._folder_children(bob_id)) == ["A"]
