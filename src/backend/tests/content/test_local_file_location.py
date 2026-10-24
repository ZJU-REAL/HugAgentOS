"""Native file actions resolve authorized, persistent paths through HTTP."""

from dataclasses import replace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.routes import files
from api.routes.v1 import projects
from core.auth.backend import UserContext, get_current_user
from core.config import settings as settings_module
from core.db.engine import Base, get_db
from core.db.models import Artifact, Project, UserShadow
from tests.db_support import share_session


@pytest.fixture
def local_files(tmp_path, monkeypatch):
    from core.files.storage import factory

    monkeypatch.setattr(factory.process_state(), 'storage_instance', None)
    monkeypatch.setenv("STORAGE_TYPE", "local")
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
    settings = settings_module.settings
    monkeypatch.setattr(
        settings_module,
        "settings",
        replace(settings, deploy=replace(settings.deploy, profile="local")),
    )
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(UserShadow(user_id="owner", username="Owner"))
    db.add(UserShadow(user_id="other", username="Other"))
    report = tmp_path / "artifacts" / "年度 报告.pdf"
    report.parent.mkdir()
    report.write_bytes(b"%PDF-test")
    db.add(
        Artifact(
            artifact_id="report",
            user_id="owner",
            type="document",
            title="Report",
            filename=report.name,
            size_bytes=9,
            mime_type="application/pdf",
            storage_key="artifacts/" + report.name,
        )
    )
    db.add(
        Project(
            project_id="local-project",
            name="Local",
            kind="local",
            owner_user_id="owner",
            extra_data={"local": {"path": str(report.parent)}},
        )
    )
    db.commit()
    app = FastAPI()
    app.include_router(files.router)
    app.include_router(projects.router)
    app.dependency_overrides[get_db] = lambda: db
    share_session(monkeypatch, db)

    def authenticate(request: Request):
        user_id = request.headers.get("x-test-user")
        if not user_id:
            raise HTTPException(status_code=401, detail="Login required")
        return UserContext(user_id=user_id, user_center_id=user_id, username=user_id)

    app.dependency_overrides[get_current_user] = authenticate
    with TestClient(app) as client:
        yield client, report
    db.close()
    engine.dispose()


def test_local_file_location_returns_original_and_containing_folder(local_files):
    client, report = local_files
    response = client.get("/files/report/local-path", headers={"x-test-user": "owner"})
    assert response.status_code == 200
    assert response.json() == {"path": str(report), "folder_path": str(report.parent)}
    assert sorted(report.parent.iterdir()) == [report]


def test_local_file_location_requires_auth_and_ownership(local_files):
    client, _ = local_files
    assert client.get("/files/report/local-path").status_code == 401
    assert (
        client.get("/files/report/local-path", headers={"x-test-user": "other"}).status_code == 403
    )


def test_local_file_location_denies_cloud_and_missing_files(local_files, monkeypatch):
    client, report = local_files
    report.unlink()
    assert (
        client.get("/files/report/local-path", headers={"x-test-user": "owner"}).status_code == 404
    )
    settings = settings_module.settings
    monkeypatch.setattr(
        settings_module, "settings", replace(settings, deploy=replace(settings.deploy, profile=""))
    )
    response = client.get("/files/report/local-path", headers={"x-test-user": "owner"})
    assert response.status_code == 404
    assert str(report) not in response.text


def test_local_file_location_does_not_follow_storage_symlink_outside_root(local_files, tmp_path):
    client, report = local_files
    outside = tmp_path.parent / (tmp_path.name + "-outside.pdf")
    outside.write_bytes(b"private")
    try:
        report.unlink()
        report.symlink_to(outside)
        assert (
            client.get("/files/report/local-path", headers={"x-test-user": "owner"}).status_code
            == 404
        )
    finally:
        outside.unlink()


def test_project_file_location_reuses_project_access_and_path_containment(local_files):
    client, report = local_files
    url = "/v1/projects/local-project/local-files/location"
    response = client.get(url, params={"path": report.name}, headers={"x-test-user": "owner"})
    assert response.status_code == 200
    assert response.json()["data"] == {"path": str(report), "folder_path": str(report.parent)}
    assert client.get(url, params={"path": report.name}).status_code == 401
    assert (
        client.get(url, params={"path": report.name}, headers={"x-test-user": "other"}).status_code
        == 404
    )
    assert (
        client.get(
            url, params={"path": "../../outside"}, headers={"x-test-user": "owner"}
        ).status_code
        == 403
    )


def test_project_office_preview_returns_pdf_from_original_file(local_files):
    import shutil
    import zipfile

    if not shutil.which("libreoffice"):
        pytest.skip("LibreOffice required for native Office conversion")
    client, report = local_files
    doc = report.with_suffix(".docx")
    with zipfile.ZipFile(doc, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
        )
        z.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        )
        z.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Local report preview</w:t></w:r></w:p></w:body></w:document>',
        )
    response = client.get(
        "/v1/projects/local-project/local-files/raw/preview",
        params={"path": doc.name, "format": "pdf"},
        headers={"x-test-user": "owner"},
    )
    assert response.status_code == 200, response.text[:300]
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")
    assert doc.is_file(), "Preview must leave the original file intact"
