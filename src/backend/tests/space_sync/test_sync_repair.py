"""Regression coverage for historical paths, deep trees and independent reads."""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from core.db.models import Artifact, UserFolder
from core.myspace.projection import pull_myspace_updates
from core.space_sync.personal import MySpaceRegistry
from core.space_sync import personal_registry, personal_policy
from core.storage import get_storage
from tests.myspace.test_registry_watcher import registry
from tests.edition_ee.test_team_workspace_access import team
from tests.space_sync.test_personal_recovery import personal_environment


def test_legacy_path_cannot_stop_other_projections(personal_environment):
    db, root = personal_environment
    # Core insert recreates historical data predating ORM filename validation.
    values = dict(user_id="alice", type="other", title="file", size_bytes=4, mime_type="text/plain")
    db.execute(Artifact.__table__.insert(), [
        dict(values, artifact_id="legacy", filename="Sub/a.txt", storage_key="old"),
        dict(values, artifact_id="good", filename="good.txt", storage_key="good"),
    ])
    db.commit()
    get_storage().upload_bytes(b"good", "good")
    report = pull_myspace_updates(user_id="alice")
    assert (root / "good.txt").read_bytes() == b"good"
    assert report.failed == 1


def test_sync_supports_more_than_eight_directory_levels(personal_environment):
    db, root = personal_environment
    from core.space_sync.personal_metadata import domain
    path = "/".join("folder" + str(i) for i in range(32))
    folder = domain("alice").directory(db, path)
    db.commit()
    assert db.get(UserFolder, folder).name == "folder31"


@pytest.mark.asyncio
async def test_read_does_not_fail_when_unrelated_write_failed(monkeypatch):
    monkeypatch.setattr(personal_registry, "_registry",
                        SimpleNamespace(flush=AsyncMock(side_effect=RuntimeError("old failure"))))
    monkeypatch.setattr("core.myspace.projection.pull_myspace_updates",
                        lambda **kw: SimpleNamespace(failed=0, conflicted=0))
    await personal_registry.flush_user("alice")


def test_find_chat_unpacks_active_runs(monkeypatch):
    from orchestration import chat_run_executor as cre
    monkeypatch.setattr(cre, "has_local_runs", lambda: True)
    monkeypatch.setattr(cre, "list_active_runs_for_user",
                        lambda uid: ([SimpleNamespace(run_id="run", chat_id="chat")], []))
    monkeypatch.setattr(cre, "is_local_run", lambda rid: rid == "run")
    assert personal_policy.find_chat("alice") == "chat"


@pytest.mark.asyncio
async def test_full_permission_does_not_even_lookup_chat(monkeypatch):
    monkeypatch.setattr("core.llm.tool_permissions.preset_answers_for_user", lambda *a, **k: True)
    ask = SimpleNamespace(get=AsyncMock(side_effect=AssertionError("unneeded chat lookup")))
    assert await MySpaceRegistry()._approved(user_id="alice", ask=ask,
        logical_path="/myspace/a", op="edit", summary="edit")

def test_legacy_repair_preserves_both_file_ids_and_bytes(personal_environment):
    from core.myspace.legacy_paths import plan_repairs, apply_repairs, restore_repairs
    db, root = personal_environment
    db.add(UserFolder(folder_id="sub", user_id="alice", name="Sub"))
    db.commit()
    values = dict(user_id="alice", type="other", title="file", size_bytes=4, mime_type="text/plain")
    db.execute(Artifact.__table__.insert(), [
        dict(values, artifact_id="legacy", filename="Sub/a.txt", storage_key="old", user_folder_id=None),
        dict(values, artifact_id="canonical", filename="a.txt", storage_key="new", user_folder_id="sub"),
    ])
    db.commit()
    storage = get_storage()
    storage.upload_bytes(b"old!", "old")
    storage.upload_bytes(b"new!", "new")
    plan = plan_repairs(db, "alice")
    assert len(plan["repairs"]) == 1
    assert apply_repairs(db, plan) == 1
    db.commit()
    legacy = db.get(Artifact, "legacy")
    assert legacy.filename == "a__legacy_legacy.txt"
    assert legacy.user_folder_id == "sub"
    assert legacy.storage_key == "old" and legacy.deleted_at is None
    assert db.get(Artifact, "canonical").filename == "a.txt"
    report = pull_myspace_updates(user_id="alice")
    assert report.failed == report.conflicted == 0
    assert (root / "Sub/a.txt").read_bytes() == b"new!"
    assert (root / "Sub/a__legacy_legacy.txt").read_bytes() == b"old!"
    assert plan_repairs(db, "alice")["repairs"] == []
    assert restore_repairs(db, plan) == 1
    db.commit()
    assert db.get(Artifact, "legacy").filename == "Sub/a.txt"


def test_legacy_repair_rejects_stale_or_edited_plan(personal_environment):
    from core.myspace.legacy_paths import plan_repairs, apply_repairs
    db, _ = personal_environment
    db.execute(Artifact.__table__.insert().values(artifact_id="legacy", user_id="alice",
        filename="Sub/a.txt", storage_key="old", title="file", type="other",
        size_bytes=4, mime_type="text/plain"))
    db.commit()
    plan = plan_repairs(db, "alice")
    plan["repairs"][0]["new_directory"] = "elsewhere"
    with pytest.raises(ValueError, match="changed"):
        apply_repairs(db, plan)
    db.rollback()
    assert db.get(Artifact, "legacy").filename == "Sub/a.txt"

def test_scoped_projection_ignores_an_unrelated_missing_file(personal_environment):
    db, root = personal_environment
    for aid in ("good", "missing"):
        db.add(Artifact(artifact_id=aid, user_id="alice", filename=aid + ".txt",
            storage_key=aid, type="other", title=aid, size_bytes=4, mime_type="text/plain"))
    db.commit()
    get_storage().upload_bytes(b"good", "good")
    report = pull_myspace_updates(user_id="alice", paths=("good.txt",))
    assert report.failed == report.conflicted == 0
    assert (root / "good.txt").read_bytes() == b"good"


@pytest.mark.asyncio
async def test_failed_item_does_not_abort_registration_of_its_sibling(registry, monkeypatch):
    from tests.myspace.test_registry_watcher import _stub_present
    from core.myspace import mirror
    _stub_present(monkeypatch, mirror.VERDICT_NEW)
    seen = []
    def register(*, user_id, entry):
        if entry.rel == "bad.txt":
            raise RuntimeError("poison file")
        seen.append(entry.rel)
        return {"file_id": "good"}
    monkeypatch.setattr(mirror, "register_entry", register)
    registry._pending[("u1", "bad.txt")] = time.monotonic()
    registry._pending[("u1", "good.txt")] = time.monotonic()
    await registry.flush("u1", strict=False)
    assert seen == ["good.txt"]
    assert ("u1", "bad.txt") in registry._failures

def test_legacy_parent_file_conflict_is_reported_without_blocking_safe_repairs(personal_environment):
    from core.myspace.legacy_paths import plan_repairs, apply_repairs
    db, _ = personal_environment
    values = dict(user_id="alice", type="other", title="file", size_bytes=4, mime_type="text/plain")
    db.execute(Artifact.__table__.insert(), [
        dict(values, artifact_id="occupied", filename="Sub", storage_key="one"),
        dict(values, artifact_id="unsafe", filename="Sub/a.txt", storage_key="two"),
        dict(values, artifact_id="safe", filename="Other/a.txt", storage_key="three"),
    ])
    db.commit()
    plan = plan_repairs(db, "alice")
    assert [r["artifact_id"] for r in plan["repairs"]] == ["safe"]
    assert plan["skipped"] == [{"artifact_id": "unsafe", "error": "Ancestor is a live file"}]
    assert apply_repairs(db, plan) == 1
    db.commit()
    assert db.get(Artifact, "unsafe").filename == "Sub/a.txt"

def test_rollback_rejects_file_changed_after_validation(personal_environment, monkeypatch):
    from core.myspace.legacy_paths import plan_repairs, apply_repairs, restore_repairs
    from sqlalchemy import event
    db, _ = personal_environment
    db.execute(Artifact.__table__.insert().values(artifact_id="legacy", user_id="alice",
        filename="Sub/a.txt", storage_key="old", title="file", type="other",
        size_bytes=4, mime_type="text/plain"))
    db.commit()
    plan = plan_repairs(db, "alice")
    apply_repairs(db, plan)
    db.commit()
    changed = False
    def interleave(conn, cursor, statement, parameters, context, executemany):
        nonlocal changed
        if not changed and statement.startswith("UPDATE artifacts"):
            changed = True
            conn.exec_driver_sql("UPDATE artifacts SET storage_key='concurrent-edit' WHERE artifact_id='legacy'")
    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", interleave)
    try:
        with pytest.raises(ValueError, match="changed during rollback"):
            restore_repairs(db, plan)
        assert changed
        db.rollback()
    finally:
        event.remove(engine, "before_cursor_execute", interleave)
    assert db.get(Artifact, "legacy").filename == plan["repairs"][0]["new_filename"]
