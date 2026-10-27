"""Personal file identity is the same at the HTTP and database boundaries."""
from tests.api.test_folder_upload import client  # noqa: F401


def test_same_folder_upload_is_renamed_and_edit_keeps_identity(client):
    first = client.post("/v1/file/upload", files={"file": ("report.txt", b"first")})
    assert first.status_code == 200, first.text
    names = [client.post("/v1/file/upload", files={"file": ("report.txt", b"again")}).json()["name"]
             for _ in range(2)]
    assert names == ["report (1).txt", "report (2).txt"]
    assert client.post("/v1/file/upload", files={"file": ("README", b"a")}).json()["name"] == "README"
    assert client.post("/v1/file/upload", files={"file": ("README", b"b")}).json()["name"] == "README (1)"
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


def test_concurrent_uploads_each_get_a_distinct_name(client):
    from concurrent.futures import ThreadPoolExecutor
    def upload(_):
        response = client.post("/v1/file/upload", files={"file": ("race.txt", b"data")})
        assert response.status_code == 200, response.text
        return response.json()["name"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sorted(pool.map(upload, range(4))) == [
            "race (1).txt", "race (2).txt", "race (3).txt", "race.txt"]


def test_retry_of_renamed_upload_returns_the_same_file(client):
    import uuid
    client.post("/v1/file/upload", files={"file": ("plan.md", b"old")})
    key = str(uuid.uuid4())
    sent = [client.post("/v1/file/upload", data={"upload_key": key},
                        files={"file": ("plan.md", b"new")}) for _ in range(2)]
    assert [r.status_code for r in sent] == [200, 200], sent[1].text
    assert sent[0].json() == sent[1].json()
    assert sent[0].json()["name"] == "plan (1).md"


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
    assert client.post("/v1/file/upload", files={"file": ("directory", b"data")}).json()["name"] == "directory (1)"


def test_chat_upload_lands_in_my_space_and_avoids_space_only_files(client):
    from core.files.spaces.access import personal_grant
    from core.files.storage import get_storage

    storage, grant = get_storage(), personal_grant("owner")
    # A file put straight into My Space has no artifact row; an upload must not overwrite it.
    storage.upload_bytes(b"space", grant.key("VPS.yml"))
    names = [client.post("/v1/file/upload", data={"chat_id": "chat"},
                         files={"file": ("VPS.yml", body)}).json()["name"] for body in (b"one", b"two")]
    assert names == ["VPS (1).yml", "VPS (2).yml"]
    assert [storage.download_bytes(grant.key(name)) for name in ("VPS.yml", *names)] == [b"space", b"one", b"two"]
