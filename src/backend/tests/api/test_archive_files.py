"""Archive browsing through authenticated HTTP, with real ZIP and local storage."""

import io
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from api.routes.files import router
from core.auth.backend import UserContext, get_current_user
from core.db.engine import Base, get_db
from core.db.models import Artifact
from core.files.storage.local import LocalStorageBackend
from tests.db_support import share_session


@pytest.fixture
def archive_client(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = Session(engine)
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "storage"))
    storage = LocalStorageBackend()
    monkeypatch.setattr('core.files.storage.get_storage', lambda: storage)
    monkeypatch.setattr("api.routes.file_archive.get_storage", lambda: storage)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    share_session(monkeypatch, db)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="owner", user_center_id="owner", username="Owner"
    )
    client = TestClient(app)

    def upload(data, name="sample.zip", owner="owner", deleted_at=None):
        key = f"archives/{len(db.query(Artifact).all())}/{name}"
        storage.upload_bytes(data, key)
        artifact = Artifact(
            artifact_id=f"archive-{len(db.query(Artifact).all())}",
            user_id=owner,
            filename=name,
            title=name,
            type="document",
            size_bytes=len(data),
            mime_type="application/octet-stream",
            storage_key=key,
            deleted_at=deleted_at,
        )
        db.add(artifact)
        db.commit()
        return f"/files/{artifact.artifact_id}/archive"

    yield client, upload
    db.close()
    engine.dispose()


def test_owner_can_list_zip_and_read_a_unicode_member(archive_client):
    client, upload = archive_client
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("资料/", b"")
        archive.writestr("资料/说明.txt", "hello 压缩包".encode())
    url = upload(data.getvalue())
    response = client.get(url)
    assert response.status_code == 200
    listing = response.json()["data"]
    assert listing["format"] == "zip"
    assert [entry["path"] for entry in listing["entries"]] == ["资料", "资料/说明.txt"]
    member = client.get(url + "/member", params={"path": "资料/说明.txt", "inline": "true"})
    assert member.status_code == 200
    assert member.text == "hello 压缩包"
    assert (
        "说明.txt" in member.headers["content-disposition"]
        or "%E8%AF%B4" in member.headers["content-disposition"]
    )
