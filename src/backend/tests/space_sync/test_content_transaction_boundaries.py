"""Object-store I/O runs outside writer transactions; changed cloud revisions win."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError

from core.db.models import Artifact
from core.infra.time import utc_now
from core.myspace import mirror
from core.space_sync.content import register
from core.storage import get_storage
from tests.edition_ee.test_team_workspace_access import team
from tests.space_sync.test_personal_recovery import personal_environment


def track_sessions(monkeypatch):
    from core.db import engine
    factory, sessions = engine.SessionLocal, []
    def tracked():
        session = factory()
        sessions.append(session)
        return session
    monkeypatch.setattr(engine, "SessionLocal", tracked)
    return factory, sessions


def test_upload_and_version_verification_hold_no_database_transaction(personal_environment, monkeypatch):
    db, root = personal_environment
    path = root / "a.txt"
    path.write_bytes(b"initial")
    result = register("alice", mirror.mirror_entry("alice", "a.txt"))
    art = db.get(Artifact, result["file_id"])
    # A metadata-only change forces the cloud-byte version verification branch.
    art.updated_at = utc_now()
    db.commit()
    factory, sessions = track_sessions(monkeypatch)
    storage = get_storage()
    upload, download = storage.upload_bytes, storage.download_bytes
    calls = []
    def unlocked(operation, *args):
        assert all(not session.in_transaction() for session in sessions)
        calls.append(operation)
        return (upload if operation == "upload" else download)(*args)
    monkeypatch.setattr(storage, "upload_bytes", lambda *args: unlocked("upload", *args))
    monkeypatch.setattr(storage, "download_bytes", lambda *args: unlocked("download", *args))
    path.write_bytes(b"changed")
    changed = register("alice", mirror.mirror_entry("alice", "a.txt"))
    assert calls == ["download", "upload"]
    assert download(changed["storage_key"]) == b"changed"


def test_cloud_replacement_during_upload_rejects_cas_and_keeps_both_cloud_and_local_bytes(
        personal_environment, monkeypatch):
    db, root = personal_environment
    path = root / "a.txt"
    path.write_bytes(b"initial")
    result = register("alice", mirror.mirror_entry("alice", "a.txt"))
    factory, sessions = track_sessions(monkeypatch)
    storage = get_storage()
    upload, deleted, staged = storage.upload_bytes, [], []
    upload(b"new-cloud", "concurrent-cloud")
    def concurrent_upload(data, key):
        assert all(not session.in_transaction() for session in sessions)
        url = upload(data, key)
        staged.append(key)
        with factory() as writer:
            row = writer.get(Artifact, result["file_id"])
            row.storage_key = "concurrent-cloud"
            row.updated_at = utc_now()
            writer.commit()
        return url
    delete = storage.delete
    def record_delete(key):
        deleted.append(key)
        delete(key)
    monkeypatch.setattr(storage, "upload_bytes", concurrent_upload)
    monkeypatch.setattr(storage, "delete", record_delete)
    path.write_bytes(b"local-unsaved")
    with pytest.raises(HTTPException) as error:
        register("alice", mirror.mirror_entry("alice", "a.txt"))
    assert error.value.status_code == 409
    db.expire_all()
    assert db.get(Artifact, result["file_id"]).storage_key == "concurrent-cloud"
    assert storage.download_bytes("concurrent-cloud") == b"new-cloud"
    assert path.read_bytes() == b"local-unsaved"
    assert deleted == staged


def test_cloud_conflict_before_upload_does_not_create_object(personal_environment, monkeypatch):
    db, root = personal_environment
    path = root / "a.txt"
    path.write_bytes(b"initial")
    result = register("alice", mirror.mirror_entry("alice", "a.txt"))
    art = db.get(Artifact, result["file_id"])
    get_storage().upload_bytes(b"cloud", "cloud")
    art.storage_key = "cloud"
    art.updated_at = utc_now()
    art.extra_data = {}
    db.commit()
    upload = Mock(side_effect=AssertionError("conflict must not upload"))
    monkeypatch.setattr(get_storage(), "upload_bytes", upload)
    path.write_bytes(b"local")
    with pytest.raises(HTTPException) as error:
        register("alice", mirror.mirror_entry("alice", "a.txt"))
    assert error.value.status_code == 409
    upload.assert_not_called()


def test_maintenance_lock_conflict_rolls_back_before_reporting_retry():
    from core.myspace.legacy_paths import _maintenance_transaction
    db = SimpleNamespace(rollback=Mock())
    class Busy(Exception):
        sqlstate = "55P03"
    @_maintenance_transaction
    def mutate(session, plan):
        raise OperationalError("SELECT FOR UPDATE NOWAIT", {}, Busy())
    with pytest.raises(ValueError, match="retry"):
        mutate(db, {})
    db.rollback.assert_called_once()


def test_artifact_lock_uses_postgresql_nowait(monkeypatch):
    from sqlalchemy import create_mock_engine
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.orm import Session
    from core.space_sync.content import _artifact_at
    engine = create_mock_engine("postgresql://", lambda *a, **kw: None)
    query = None
    with Session(bind=engine) as db:
        original = db.query
        def capture(*models):
            nonlocal query
            query = original(*models)
            return query
        monkeypatch.setattr(db, "query", capture)
        from sqlalchemy.orm import Query
        def inspect(statement):
            sql = str(statement.statement.compile(dialect=postgresql.dialect()))
            assert "FOR UPDATE NOWAIT" in sql
            return None
        monkeypatch.setattr(Query, "one_or_none", inspect)
        scope = SimpleNamespace(folders=lambda db: {None: None}, artifact_predicates=(Artifact.user_id == "alice",))
        assert _artifact_at(db, scope, "a.txt", lock=True) is None

@pytest.mark.asyncio
async def test_ask_without_owner_conversation_preserves_pending_write(personal_environment):
    from core.services.user_service import UserService
    from core.space_sync.personal_policy import Ask, approve
    db, root = personal_environment
    UserService(db).update_user_metadata("alice", {"tool_approval_mode": "ask"})
    path = root / "unsaved.txt"
    path.write_bytes(b"unsaved")
    with pytest.raises(RuntimeError, match="no active owner conversation"):
        await approve("alice", Ask("alice"), "/myspace/unsaved.txt", "edit", "changed")
    assert path.read_bytes() == b"unsaved"
    assert not db.query(Artifact).filter_by(user_id="alice", filename="unsaved.txt").all()

def test_equal_cloud_revision_is_adopted_without_reupload(personal_environment, monkeypatch):
    import hashlib
    from core.space_sync.index import get
    db, root = personal_environment
    path = root / "a.txt"
    path.write_bytes(b"initial")
    result = register("alice", mirror.mirror_entry("alice", "a.txt"))
    storage = get_storage()
    storage.upload_bytes(b"same", "new-cloud")
    art = db.get(Artifact, result["file_id"])
    art.storage_key = "new-cloud"
    art.size_bytes = 4
    art.updated_at = utc_now()
    art.extra_data = {"space_sync": {"sha256": hashlib.sha256(b"same").hexdigest()}}
    db.commit()
    monkeypatch.setattr(storage, "upload_bytes", Mock(side_effect=AssertionError("must adopt")))
    path.write_bytes(b"same")
    adopted = register("alice", mirror.mirror_entry("alice", "a.txt"))
    assert adopted["storage_key"] == "new-cloud"
    assert get("alice", "a.txt")["key"] == "new-cloud"

def test_maintenance_plan_takes_nowait_artifact_locks(personal_environment, monkeypatch):
    from sqlalchemy.orm import Query
    from core.myspace.legacy_paths import plan_repairs
    db, root = personal_environment
    locked = []
    original = Query.with_for_update
    def capture(query, **kwargs):
        locked.append(kwargs)
        return original(query, **kwargs)
    monkeypatch.setattr(Query, "with_for_update", capture)
    plan_repairs(db, "alice", lock=True)
    assert locked == [{"nowait": True}]
