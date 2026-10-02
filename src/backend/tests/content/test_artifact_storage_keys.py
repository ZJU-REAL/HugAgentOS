import pytest

from core.content.artifact_refs import require_artifact_storage_key


def test_new_artifact_without_an_authoritative_key_is_rejected():
    with pytest.raises(ValueError, match="storage key"):
        require_artifact_storage_key("unregistered-file")

from types import SimpleNamespace
from core.content.artifact_key_repair import verified_legacy_key


class Objects:
    def __init__(self, entries):
        self.entries = entries

    def exists(self, key):
        return key in self.entries

    def download_bytes(self, key):
        return self.entries[key]


def row(key="artifacts/old", size=7):
    return SimpleNamespace(artifact_id="old", filename="报告.docx", storage_key=key, size_bytes=size)


def test_historical_placeholder_repairs_only_to_verified_matching_object():
    storage = Objects({"artifacts/old.docx": b"content"})
    assert verified_legacy_key(row(), storage) == "artifacts/old.docx"


def test_real_extensionless_object_and_unmatched_size_are_preserved():
    assert verified_legacy_key(row(), Objects({"artifacts/old": b"content"})) is None
    assert verified_legacy_key(row(), Objects({"artifacts/old.docx": b"other"})) is None

from core.content.artifact_key_repair import repair_legacy_key
from core.db.models import Artifact


def test_repair_is_audited_idempotent_and_keeps_content_version(db_session):
    art = Artifact(artifact_id="old", user_id="u", filename="report.docx", type="report",
                   title="report", mime_type="application/octet-stream", size_bytes=7,
                   storage_key="artifacts/old")
    db_session.add(art)
    db_session.commit()
    version = art.updated_at
    storage = Objects({"artifacts/old.docx": b"content"})
    assert repair_legacy_key(db_session, art, storage)
    assert art.storage_key == "artifacts/old.docx"
    assert art.updated_at == version
    assert art.extra_data["storage_key_repair"]["old_key"] == "artifacts/old"
    assert not repair_legacy_key(db_session, art, storage)

from core.content.artifact_key_repair import rollback_legacy_key


def test_audited_repair_can_be_rolled_back_but_not_after_content_changes(db_session):
    art = Artifact(artifact_id="old", user_id="u", filename="report.docx", type="report",
                   title="report", mime_type="application/octet-stream", size_bytes=7,
                   storage_key="artifacts/old")
    db_session.add(art)
    db_session.commit()
    storage = Objects({"artifacts/old.docx": b"content"})
    assert repair_legacy_key(db_session, art, storage)
    assert rollback_legacy_key(db_session, art)
    assert art.storage_key == "artifacts/old"
    assert repair_legacy_key(db_session, art, storage)
    art.storage_key = "new/immutable/version"
    db_session.commit()
    assert not rollback_legacy_key(db_session, art)


def test_explicit_historical_placeholder_is_resolved_to_verified_suffix(monkeypatch):
    monkeypatch.setattr("core.storage.get_storage", lambda: Objects({"artifacts/old.docx": b"content"}))
    assert require_artifact_storage_key("old", "artifacts/old", filename="report.docx", size=7) == "artifacts/old.docx"


def test_stale_repair_snapshot_cannot_overwrite_a_concurrent_rename(db_session):
    from sqlalchemy.orm import Session
    art = Artifact(artifact_id="old", user_id="u", filename="report.docx", type="report",
                   title="report", mime_type="application/octet-stream", size_bytes=7,
                   storage_key="artifacts/old")
    db_session.add(art)
    db_session.commit()
    _ = art.filename  # Retain the original snapshot in this session.
    with Session(db_session.get_bind()) as other:
        concurrent = other.get(Artifact, "old")
        concurrent.filename = "renamed.docx"
        other.commit()
    assert not repair_legacy_key(db_session, art, Objects({"artifacts/old.docx": b"content"}))
    assert (art.filename, art.storage_key) == ("renamed.docx", "artifacts/old")
