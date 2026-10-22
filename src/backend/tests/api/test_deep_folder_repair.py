import pytest
from types import SimpleNamespace
from core.auth.backend import get_current_user
from tests.api.test_folder_upload import client

@pytest.fixture(autouse=True)
def normal_user(client):
    client.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        user_id="owner", username="owner", public_chat_visitor=False)



def test_deep_batch_upload_move_and_breadcrumb(client):
    path = "/".join("level" + str(i) for i in range(32))
    result = client.post("/v1/myspace/folders/batch", json={"paths": [path]})
    assert result.status_code == 200, result.text
    fid = result.json()["data"]["folders"][path]
    response = client.post("/v1/file/upload", data={"folder_id": fid},
                           files={"file": ("deep.txt", b"content")})
    assert response.status_code == 200, response.text
    breadcrumb = client.get("/v1/myspace/folders/breadcrumb", params={"folder_id": fid})
    assert len(breadcrumb.json()["data"]["breadcrumb"]) == 32
    leaf = client.post("/v1/myspace/folders", json={"name": "Moved"}).json()["data"]["folder_id"]
    moved = client.patch("/v1/myspace/folders/" + leaf, json={"parent_folder_id": fid})
    assert moved.status_code == 200, moved.text
    assert len(client.get("/v1/myspace/folders/breadcrumb",
               params={"folder_id": leaf}).json()["data"]["breadcrumb"]) == 33
    cycle = client.patch("/v1/myspace/folders/" + fid, json={"parent_folder_id": leaf})
    assert cycle.status_code == 400


def test_path_in_filename_is_rejected_before_storage(client, monkeypatch):
    from core.files.storage import get_storage
    def fail(*a, **k):
        raise AssertionError("invalid filename reached object storage")
    monkeypatch.setattr(get_storage(), "upload_bytes", fail)
    response = client.post("/v1/file/upload", files={"file": ("Sub/a.txt", b"data")})
    assert response.status_code == 400
