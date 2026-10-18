"""Personal file identity is the same at the HTTP and database boundaries."""
from tests.api.test_folder_upload import client  # noqa: F401


def test_same_folder_rejects_new_name_but_allows_edit(client):
    first = client.post("/v1/file/upload", files={"file": ("report.txt", b"first")})
    assert first.status_code == 200, first.text
    duplicate = client.post("/v1/file/upload", files={"file": ("report.txt", b"second")})
    assert duplicate.status_code == 409, duplicate.text
    replaced = client.put(
        "/v1/file/" + first.json()["file_id"],
        files={"file": ("report.txt", b"updated")},
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["file_id"] == first.json()["file_id"]


def test_names_are_scoped_and_move_copy_cannot_create_duplicates(client):
    first = client.post("/v1/file/upload", files={"file": ("report.txt", b"first")}).json()
    assert client.post("/v1/myspace/folders/copy-artifact",
                       json={"artifact_id": first["file_id"], "folder_id": None}).status_code == 200
    folder = client.post("/v1/myspace/folders", json={"name": "Nested"}).json()["data"]["folder_id"]
    other = client.post("/v1/file/upload", data={"folder_id": folder},
                        files={"file": ("report.txt", b"other")})
    assert other.status_code == 200, other.text
    moved = client.post("/v1/myspace/folders/move-artifact",
                        json={"artifact_id": other.json()["file_id"], "folder_id": None})
    assert moved.status_code == 409, moved.text
    copied = client.post("/v1/myspace/folders/copy-artifact",
                         json={"artifact_id": first["file_id"], "folder_id": folder})
    assert copied.status_code == 409, copied.text


def test_concurrent_uploads_have_exactly_one_winner(client):
    from concurrent.futures import ThreadPoolExecutor
    def upload(_):
        return client.post("/v1/file/upload", files={"file": ("race.txt", b"data")}).status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sorted(pool.map(upload, range(4))) == [200, 409, 409, 409]


def test_direct_database_insert_cannot_bypass_uniqueness(client):
    import pytest
    from fastapi import HTTPException
    from core.db.engine import get_db
    from core.db.models import Artifact
    from sqlalchemy import insert

    client.post("/v1/file/upload", files={"file": ("report.txt", b"first")})
    with next(client.app.dependency_overrides[get_db]()) as db:
        with pytest.raises(HTTPException) as error:
            db.execute(insert(Artifact.__table__).values(
                artifact_id="raw-dupe", user_id="owner", filename="report.txt", title="report.txt",
                type="document", mime_type="text/plain", size_bytes=1, storage_key="raw-dupe"))
        assert error.value.status_code == 409
        db.rollback()


def test_file_and_folder_share_one_namespace(client):
    assert client.post("/v1/file/upload", files={"file": ("occupied", b"data")}).status_code == 200
    assert client.post("/v1/myspace/folders", json={"name": "occupied"}).status_code == 409
    assert client.post("/v1/myspace/folders", json={"name": "directory"}).status_code == 200
    assert client.post("/v1/file/upload", files={"file": ("directory", b"data")}).status_code == 409
