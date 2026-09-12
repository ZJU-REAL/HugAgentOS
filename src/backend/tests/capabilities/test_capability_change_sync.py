"""Changes are previewed and committed through the authenticated device API."""

import os
from pathlib import Path
from tests.capabilities.test_desktop_capabilities_api import client


def test_preview_includes_query_output_and_marks_file_conflict(client, monkeypatch):
    from core.capabilities import change_sync
    from core.capabilities.change_merge import revision

    folder = Path(os.environ["HUGAGENT_CAPS_ROOT"]) / "skills" / "news"
    folder.mkdir(parents=True)
    md = "---\nname: news\ndescription: News\n---\nLocal"
    folder.joinpath("SKILL.md").write_text(md)
    folder.joinpath("query.json").write_text("{}")
    client.get("/v1/desktop/capabilities/installations")
    cloud = {"SKILL.md": md.replace("Local", "Cloud")}
    monkeypatch.setattr(
        change_sync,
        "cloud_request",
        lambda *a, **kw: {
            "files": cloud,
            "revision": revision(cloud),
            "exists": True,
            "can_edit": True,
        },
    )
    response = client.post(
        "/v1/desktop/capabilities/changes/preview", json={"install_id": "skill:local:news"}
    )
    assert response.status_code == 200
    rows = {row["path"]: row for row in response.json()["data"]["changes"]}
    assert rows["SKILL.md"]["conflict"]
    assert "query.json" in rows


def test_commit_requires_resolution_and_rechecks_local_files(client, monkeypatch):
    from core.capabilities import change_sync
    from core.capabilities.change_merge import revision

    folder = Path(os.environ["HUGAGENT_CAPS_ROOT"]) / "skills" / "news"
    folder.mkdir(parents=True)
    md = "---\nname: news\ndescription: News\n---\nLocal"
    folder.joinpath("SKILL.md").write_text(md)
    client.get("/v1/desktop/capabilities/installations")
    cloud = {"SKILL.md": md.replace("Local", "Cloud")}
    calls = []

    def request(state, method, kind, key, body=None):
        calls.append(method)
        return {"files": cloud, "revision": revision(cloud), "exists": True, "can_edit": True}

    monkeypatch.setattr(change_sync, "cloud_request", request)
    preview = client.post(
        "/v1/desktop/capabilities/changes/preview", json={"install_id": "skill:local:news"}
    ).json()["data"]
    response = client.post(
        "/v1/desktop/capabilities/changes/commit",
        json={"preview_id": preview["preview_id"], "choices": {}},
    )
    assert response.status_code == 409
    folder.joinpath("query.json").write_text("changed during confirmation")
    response = client.post(
        "/v1/desktop/capabilities/changes/commit",
        json={"preview_id": preview["preview_id"], "choices": {"SKILL.md": {"side": "local"}}},
    )
    assert response.status_code == 409
    assert calls == ["GET"]


def test_successful_upload_is_idempotent_and_next_preview_has_baseline(client, monkeypatch):
    from core.capabilities import change_sync
    from core.capabilities.change_merge import revision

    folder = Path(os.environ["HUGAGENT_CAPS_ROOT"]) / "skills" / "news"
    folder.mkdir(parents=True)
    folder.joinpath("SKILL.md").write_text("---\nname: news\ndescription: News\n---\nLocal")
    folder.joinpath("query.json").write_text("{}")
    client.get("/v1/desktop/capabilities/installations")
    remote = {"files": {}, "exists": False, "can_edit": True}
    calls = []

    def request(state, method, kind, key, body=None):
        if method == "POST":
            calls.append(body)
            remote.update(files=body["files"], exists=True, key=key)
        return {**remote, "revision": revision(remote["files"])}

    monkeypatch.setattr(change_sync, "cloud_request", request)
    preview = client.post(
        "/v1/desktop/capabilities/changes/preview", json={"install_id": "skill:local:news"}
    ).json()["data"]
    body = {"preview_id": preview["preview_id"], "choices": {}}
    saved = client.post("/v1/desktop/capabilities/changes/commit", json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["local_applied"]
    assert (
        client.post("/v1/desktop/capabilities/changes/commit", json=body).json()["data"]
        == saved.json()["data"]
    )
    assert len(calls) == 1
    assert "query.json" in remote["files"]
    folder.joinpath("query.json").write_text("updated")
    remote["files"] = {**remote["files"], "cloud.txt": "remote addition"}
    preview2 = client.post(
        "/v1/desktop/capabilities/changes/preview", json={"install_id": "skill:local:news"}
    ).json()["data"]
    assert not any(row["conflict"] for row in preview2["changes"])


def test_download_captures_baseline_and_cloud_choice_uses_fresh_directory(client, monkeypatch):
    from tests.capabilities.test_desktop_capabilities_api import _iid, USER
    from core.capabilities import change_sync
    from core.capabilities.change_merge import revision

    client.post("/v1/desktop/capabilities/sync")
    iid = _iid("market-x")
    _, _, original, path = change_sync.source(USER, iid)
    path.joinpath("a.py").write_text("local edit")
    remote = {**original, "cloud.txt": "cloud addition"}

    def request(state, method, kind, key, body=None):
        return {
            "key": key,
            "files": body["files"] if body else remote,
            "revision": revision(body["files"] if body else remote),
            "exists": True,
            "can_edit": True,
        }

    monkeypatch.setattr(change_sync, "cloud_request", request)
    preview = client.post(
        "/v1/desktop/capabilities/changes/preview", json={"install_id": iid}
    ).json()["data"]
    assert not any(row["conflict"] for row in preview["changes"])
    saved = client.post(
        "/v1/desktop/capabilities/changes/commit",
        json={
            "preview_id": preview["preview_id"],
            "choices": {"a.py": {"side": "cloud"}, "cloud.txt": {"side": "local"}},
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["local_applied"]
    assert change_sync.source(USER, iid)[2] == original
    assert change_sync.source(USER, iid)[3] != path
    assert path.joinpath("a.py").read_text() == "local edit"


def test_account_switch_after_http_never_applies_local(client, monkeypatch):
    import pytest
    from core.capabilities import change_sync
    from core.capabilities.change_merge import revision
    from core.capabilities.errors import CloudUnavailable
    from core.services import desktop_cloud_bridge as bridge
    from tests.capabilities.test_desktop_capabilities_api import USER

    folder = Path(os.environ["HUGAGENT_CAPS_ROOT"]) / "skills" / "news"
    folder.mkdir(parents=True)
    folder.joinpath("SKILL.md").write_text("---\nname: news\ndescription: News\n---\nLocal")
    client.get("/v1/desktop/capabilities/installations")
    state = bridge.get_state()
    monkeypatch.setattr(
        change_sync,
        "cloud_request",
        lambda *a, **k: {"files": {}, "revision": revision({}), "exists": False, "can_edit": True},
    )
    preview = change_sync.preview(USER, "skill:local:news", state)

    def switched(state, method, kind, key, body=None):
        monkeypatch.setattr(bridge, "get_state", lambda: {})
        return {"key": key, "files": body["files"], "revision": revision(body["files"])}

    monkeypatch.setattr(change_sync, "cloud_request", switched)
    before = folder.joinpath("SKILL.md").read_text()
    with pytest.raises(CloudUnavailable):
        change_sync.commit(USER, preview["preview_id"], {}, state)
    assert folder.joinpath("SKILL.md").read_text() == before


def test_local_connector_preserves_unknown_config_fields(client):
    from core.capabilities import change_sync, mcp_json
    from core.capabilities.paths import mcp_json_path
    from tests.capabilities.test_desktop_capabilities_api import USER
    import json

    mcp_json.upsert_local_server("custom", {"transport": "stdio", "command": "example"})
    path = mcp_json_path()
    raw = json.loads(path.read_text())
    raw["local"]["servers"]["custom"]["custom_log"] = {"path": "query.json"}
    path.write_text(json.dumps(raw))
    files = change_sync.source(USER, "mcp:local-json:custom")[2]
    assert json.loads(files["connector.json"])["custom_log"] == {"path": "query.json"}


def test_inaccessible_cloud_name_can_be_forked_without_reading_foreign_files(client, monkeypatch):
    from fastapi import HTTPException
    from core.capabilities import change_sync
    from core.capabilities.change_merge import revision

    folder = Path(os.environ["HUGAGENT_CAPS_ROOT"]) / "skills" / "news"
    folder.mkdir(parents=True)
    folder.joinpath("SKILL.md").write_text("---\nname: news\ndescription: News\n---\nLocal")
    client.get("/v1/desktop/capabilities/installations")
    posted = []

    def request(state, method, kind, key, body=None):
        if method == "GET":
            raise HTTPException(404)
        posted.append((key, body))
        return {"key": key, "files": body["files"], "revision": revision(body["files"])}

    monkeypatch.setattr(change_sync, "cloud_request", request)
    preview = client.post(
        "/v1/desktop/capabilities/changes/preview", json={"install_id": "skill:local:news"}
    ).json()["data"]
    assert not preview["can_edit"]
    assert all(row["cloud"] is None for row in preview["changes"])
    response = client.post(
        "/v1/desktop/capabilities/changes/commit",
        json={"preview_id": preview["preview_id"], "choices": {}, "fork_key": "my-news"},
    )
    assert response.status_code == 200, response.text
    assert posted[0][0] == "my-news"
    assert posted[0][1]["create_only"]
