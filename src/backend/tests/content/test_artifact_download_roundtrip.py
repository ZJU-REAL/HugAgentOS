"""A saved artifact resolves through its file reference and downloads from /files/{file_id}."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException
from starlette.responses import FileResponse

import core.files.artifacts.store as store
from api.routes.files import download_file
from core.files.artifacts.refs import extract_file_ref, resolve_artifact_storage_key

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture
def artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_TYPE", "local")
    monkeypatch.setattr(store, "_STORE_DIR", tmp_path / "artifacts")
    return store.save_artifact_bytes(content=b"demo", name="demo.docx", mime_type=DOCX, extension="docx")


def test_saved_artifact_is_retrievable(artifact):
    assert artifact["file_id"]
    loaded = store.get_artifact(artifact["file_id"])
    assert loaded is not None
    if loaded.get("path"):
        assert Path(loaded["path"]).read_bytes() == b"demo"
    else:
        assert loaded["storage_key"] == artifact["storage_key"]
    assert (
        resolve_artifact_storage_key(artifact["file_id"], f"artifacts/{artifact['file_id']}")
        == artifact["storage_key"]
    )


def test_file_ref_carries_the_storage_key(artifact):
    ref = extract_file_ref(
        {
            "file_id": artifact["file_id"],
            "name": artifact["name"],
            "mime_type": artifact["mime_type"],
            "size": artifact["size"],
            "url": f"/files/{artifact['file_id']}",
        }
    )

    assert ref is not None
    assert ref["storage_key"] == artifact["storage_key"]


def test_download_serves_the_artifact_file(artifact, db_session):
    loaded = store.get_artifact(artifact["file_id"])

    response = download_file(artifact["file_id"], BackgroundTasks(), mode="direct", inline=False,
                             user=None, db=db_session)

    assert isinstance(response, FileResponse)
    if loaded.get("path"):
        assert str(response.path) == str(loaded["path"])
    assert Path(str(response.path)).read_bytes() == b"demo"


def test_download_of_unknown_artifact_is_404(artifact, db_session):
    with pytest.raises(HTTPException) as error:
        download_file("missing-id", BackgroundTasks(), mode="direct", inline=False, user=None, db=db_session)
    assert error.value.status_code == 404
