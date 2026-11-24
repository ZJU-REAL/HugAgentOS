"""UserFolderService: personal folder tree, ownership checks and storage-backed moves."""

from __future__ import annotations

import pytest

from core.db.models import UserShadow
from core.files.spaces.files import SpaceFiles
from core.files.storage.local import LocalStorageBackend
from core.projects import user_folders
from core.projects.user_folders import UserFolderService, _sanitize_name

OWNER = "u_test_1"
OTHER = "u_other"


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "storage"))
    backend = LocalStorageBackend()
    monkeypatch.setattr(user_folders, "get_storage", lambda: backend)
    return backend


@pytest.fixture
def svc(db_session, storage):
    db_session.add_all([UserShadow(user_id=OWNER, username="t1"), UserShadow(user_id=OTHER, username="o")])
    db_session.commit()
    return UserFolderService(db_session)


def _create(svc, parent, name, user=OWNER):
    result = svc.create_folder(user, parent, name, actor=user)
    assert result.ok, result.message
    return result.folder_id


@pytest.mark.parametrize("name", ["", "  ", "a/b", "a\\b", ".", "..", "x" * 256, "a\x01b"])
def test_sanitize_name_rejects_invalid_names(name):
    assert _sanitize_name(name)[0] is False


def test_sanitize_name_strips_whitespace():
    assert _sanitize_name(" hello ") == (True, "hello")


def test_create_folder_rejects_duplicates_and_foreign_parent(svc):
    root = _create(svc, None, "工作")
    assert root.startswith("ufld_")

    dup = svc.create_folder(OWNER, None, "工作", actor=OWNER)
    assert not dup.ok and "同名" in dup.message

    foreign = svc.create_folder(OTHER, root, "x", actor=OTHER)
    assert not foreign.ok and "不属于" in foreign.message

    # Acting on someone else's space is refused even with a valid parent.
    assert not svc.create_folder(OWNER, None, "x", actor=OTHER).ok


def test_tree_and_breadcrumb_are_scoped_to_owner(svc):
    root = _create(svc, None, "工作")
    child = _create(svc, root, "项目A")

    tree = svc.get_tree(OWNER)
    assert [n["name"] for n in tree] == ["工作"]
    assert [n["name"] for n in tree[0]["children"]] == ["项目A"]

    assert [b["name"] for b in svc.get_breadcrumb(child, OWNER)] == ["工作", "项目A"]
    assert svc.get_breadcrumb(child, OTHER) == []
    assert svc.get_tree(OTHER) == []


def test_rename_folder_updates_name_and_rejects_other_user(svc):
    root = _create(svc, None, "工作")
    child = _create(svc, root, "项目A")

    assert svc.rename_folder(child, "项目B", actor=OWNER).ok
    assert svc.get(child).name == "项目B"
    assert not svc.rename_folder(child, "x", actor=OTHER).ok


def test_move_folder_rejects_cycles(svc):
    root = _create(svc, None, "工作")
    child = _create(svc, root, "项目A")
    archive = _create(svc, None, "归档")

    assert svc.move_folder(child, archive, actor=OWNER).ok
    assert svc.get(child).parent_folder_id == archive

    cycle = svc.move_folder(archive, child, actor=OWNER)
    assert not cycle.ok
    assert not svc.move_folder(archive, archive, actor=OWNER).ok


def test_delete_folder_cascades_to_descendants_and_files(svc, db_session, storage):
    archive = _create(svc, None, "归档")
    child = _create(svc, archive, "项目A")
    files = SpaceFiles(db_session, OWNER, storage)
    files.write("personal", "归档/项目A/t.txt", b"x")
    files.write("personal", "归档/top.txt", b"y")

    assert svc.count_affected_artifacts(archive, OWNER) == 2
    assert svc.count_affected_artifacts(child, OWNER) == 1
    assert svc.count_affected_artifacts(archive, OTHER) == 0

    result, affected = svc.delete_folder(archive, actor=OWNER)
    assert result.ok and affected == 2
    assert svc.get(archive) is None
    assert svc.get(child) is None
    assert svc.count_affected_artifacts(archive, OWNER) == 0


def test_delete_folder_rejects_other_user(svc):
    root = _create(svc, None, "工作")
    result, affected = svc.delete_folder(root, actor=OTHER)
    assert not result.ok and affected == 0
    assert svc.get(root) is not None
