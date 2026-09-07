"""An independent local runtime alias must not make its copied file uneditable."""

from core.capabilities import registry
from tests.capabilities.test_desktop_capabilities_api import client, PROFILE, _Cloud


def test_edit_alias_copy_preserves_source_name_and_rejects_frontmatter_rename(client, monkeypatch):
    text = "---\nname: original\ndescription: Synthetic skill\n---\nOriginal body\n"
    monkeypatch.setattr("httpx.get", _Cloud({"original": {"SKILL.md": text}}).get)
    assert client.post("/v1/desktop/capabilities/sync").status_code == 200
    source = registry.install_id("skill", PROFILE, "original")
    prepared = client.post("/v1/desktop/capabilities/preparations", json={"install_ids": [source]})
    assert prepared.json()["data"]["results"][0]["ok"]
    before = registry.get(source).resolved_revision
    response = client.post(
        "/v1/desktop/capabilities/installations/" + source + "/local-copy",
        json={"runtime_name": "my-alias"},
    )
    assert response.status_code == 200
    copied = response.json()["data"]
    assert copied["installation"]["runtime_name"] == "my-alias"
    path = "/v1/desktop/capabilities/installations/" + copied["install_id"] + "/files/SKILL.md"
    opened = client.get(path).json()["data"]
    assert opened["content"] == text
    saved = client.put(
        path, json={"content": text + "Updated body\n", "expected_revision": opened["revision"]}
    )
    assert saved.status_code == 200
    current = saved.json()["data"]
    assert current["content"].startswith("---\nname: original\n")
    assert current["installation"]["runtime_name"] == "my-alias"
    denied = client.put(
        path,
        json={
            "content": text.replace("name: original", "name: changed"),
            "expected_revision": current["revision"],
        },
    )
    assert denied.status_code == 400
    assert client.get(path).json()["data"]["content"] == current["content"]
    assert registry.get(source).resolved_revision == before

    from core.capabilities import runtime
    from core.db.models import ContentBlock
    from tests.capabilities.test_desktop_capabilities_api import USER

    with registry._session() as db:
        ContentBlock.__table__.create(db.get_bind(), checkfirst=True)
    run = runtime.prepare("alias-copy-runtime", USER, skill_ids=["my-alias"])
    loader = runtime.frozen_loader(run)
    assert "my-alias" in loader.load_all_metadata()
    spec = loader.load_skill_full("my-alias")
    assert spec is not None and spec.id == "my-alias"
    assert "Original body" in str(spec.instructions)
    from pathlib import Path

    assert (Path(loader.get_skill_dir("my-alias")) / "SKILL.md").read_text() == current["content"]
