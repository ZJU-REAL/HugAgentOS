import pytest
from core.db.models import Artifact
from core.space_sync.personal import MySpaceRegistry
from tests.edition_ee.test_team_workspace_access import team  # noqa: F401
from tests.space_sync.test_personal_recovery import personal_environment  # noqa: F401


@pytest.mark.asyncio
async def test_metadata_read_does_not_require_missing_remote_bytes(personal_environment):
    db, root = personal_environment
    db.add(Artifact(artifact_id="missing", user_id="alice", filename="missing.docx",
                    storage_key="artifacts/missing", size_bytes=7, type="report", title="missing", mime_type="application/octet-stream"))
    db.commit()
    registry = MySpaceRegistry()
    await registry.start()
    try:
        await registry.flush("alice", metadata_only=True)
        assert not (root / "missing.docx").exists()
    finally:
        await registry.stop()

from core.myspace.projection import pull_myspace_updates
from core.storage import get_storage


def test_projection_repairs_legacy_key_and_keeps_working_when_another_file_is_missing(personal_environment):
    db, root = personal_environment
    storage = get_storage()
    storage.upload_bytes(b"content", "artifacts/legacy.docx")
    for identity in ("legacy", "missing"):
        db.add(Artifact(artifact_id=identity, user_id="alice", filename=identity + ".docx",
                        storage_key="artifacts/" + identity, size_bytes=7, type="report",
                        title=identity, mime_type="application/octet-stream"))
    db.commit()
    version = db.get(Artifact, "legacy").updated_at
    report = pull_myspace_updates(user_id="alice")
    assert (report.materialized, report.failed) == (1, 1)
    assert (root / "legacy.docx").read_bytes() == b"content"
    db.expire_all()
    repaired = db.get(Artifact, "legacy")
    assert repaired.storage_key == "artifacts/legacy.docx"
    assert repaired.updated_at == version
    (root / "legacy.docx").write_bytes(b"local unsaved changes")
    pull_myspace_updates(user_id="alice")
    assert (root / "legacy.docx").read_bytes() == b"local unsaved changes"

import asyncio
from core.db.repository import ArtifactRepository


@pytest.mark.asyncio
async def test_metadata_read_drains_local_writes_despite_missing_cloud_file(personal_environment):
    db, root = personal_environment
    db.add(Artifact(artifact_id="missing", user_id="alice", filename="missing.docx",
                    storage_key="artifacts/missing", size_bytes=7, type="report", title="missing",
                    mime_type="application/octet-stream"))
    db.commit()
    registry = MySpaceRegistry()
    await registry.start()
    try:
        (root / "local.txt").write_bytes(b"local new file")
        await asyncio.sleep(0.1)
        await registry.flush("alice", metadata_only=True)
        db.expire_all()
        rows, _ = ArtifactRepository(db).list_by_user_with_chat("alice", personal_only=True)
        assert "local.txt" in [artifact["artifact"].filename for artifact in rows]
        with pytest.raises(RuntimeError, match="同步未完成"):
            await registry.flush("alice")
    finally:
        await registry.stop()


def test_repair_cli_dry_run_preserves_database_then_apply_is_audited(personal_environment, monkeypatch, capsys):
    import runpy
    import sys
    db, _root = personal_environment
    get_storage().upload_bytes(b"content", "artifacts/legacy.docx")
    db.add(Artifact(artifact_id="legacy", user_id="alice", filename="legacy.docx",
                    storage_key="artifacts/legacy", size_bytes=7, type="report", title="legacy",
                    mime_type="application/octet-stream"))
    db.commit()
    monkeypatch.setattr(sys, "argv", ["repair", "--user-id", "alice"])
    runpy.run_path("scripts/repair_artifact_storage_keys.py", run_name="__main__")
    assert "artifacts/legacy.docx" in capsys.readouterr().out
    db.expire_all()
    assert db.get(Artifact, "legacy").storage_key == "artifacts/legacy"
    monkeypatch.setattr(sys, "argv", ["repair", "--user-id", "alice", "--apply"])
    runpy.run_path("scripts/repair_artifact_storage_keys.py", run_name="__main__")
    db.expire_all()
    art = db.get(Artifact, "legacy")
    assert art.storage_key == "artifacts/legacy.docx"
    assert art.extra_data["storage_key_repair"]["old_key"] == "artifacts/legacy"
