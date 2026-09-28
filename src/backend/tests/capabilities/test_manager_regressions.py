"""Lifecycle regressions found by independent implementation review."""
import io
import json
import zipfile
from pathlib import Path
from contextlib import nullcontext
from types import SimpleNamespace
import pytest
from core.capabilities import registry, store
from core.services import local_skill_service as skills, local_plugin_service as plugins


def skill(root, body="First"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text("---\nname: example\ndescription: Example skill\n---\n" + body)
    return str(root)


def test_plugin_dependencies_and_sibling_paths_survive_import(client, tmp_path):
    root = tmp_path / "plugin"
    for name in ("first", "second"):
        child = root / "skills" / name
        child.mkdir(parents=True)
        (child / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Test\nmcp_servers: lookup\n---\nRead ../second/SKILL.md and ${{SKILL_DIR}}/scripts/run.py")
    (root / "plugin.json").write_text(json.dumps({"name": "dependencies", "version": "1", "mcpServers": {"lookup": {"url": "https://example.test/mcp"}}}))
    installed = plugins.install("local-u1", str(root))
    children = installed["components"]["skills"]
    sid = installed["components"]["mcp"][0]
    first = registry.get(registry.install_id("skill", "local", children[0]))
    content = (store.get("skill", "local", first.key, first.resolved_revision).path / "SKILL.md").read_text()
    assert sid in content
    assert "../" + children[1] + "/SKILL.md" in content
    assert "${SKILL_DIR}" not in content
    with pytest.raises(ValueError, match="plugin component"):
        skills.uninstall("local-u1", first.install_id, first.resolved_revision)
    from core.capabilities.invocation import cloud_plugin_selection
    selected = cloud_plugin_selection(installed["install_id"], user_id="local-u1")
    assert selected["skills"] == children
    assert selected["mcp"] == [sid]


def test_upload_repeat_and_lost_response_have_stable_identity(client, tmp_path, monkeypatch):
    from core.services import local_skill_upload, desktop_cloud_bridge as bridge
    from core.capabilities import skills as capability_skills, change_sync
    installed = skills.install("local-u1", skill(tmp_path / "skill"))
    monkeypatch.setattr(bridge, "require_current_account", lambda _: None)
    monkeypatch.setattr(bridge, "account_scope", lambda _: nullcontext())
    monkeypatch.setattr(capability_skills, "account_authorized_for", lambda uid: uid == "local-u1")
    monkeypatch.setattr(capability_skills, "current_account_profile", lambda: "test-cloud")
    remote = {"exists": False, "revision": "empty", "model_version": None}
    receipts, posts = {}, []
    def request(state, method, kind, key, body=None):
        if method == "GET":
            return dict(remote)
        posts.append(body["request_id"])
        if body["request_id"] not in receipts:
            receipts[body["request_id"]] = {"applied": True, "revision": "cloud-r1"}
            remote.update(exists=True, revision="cloud-r1")
            raise TimeoutError("response lost after commit")
        return receipts[body["request_id"]]
    monkeypatch.setattr(change_sync, "cloud_request", request)
    with pytest.raises(TimeoutError):
        local_skill_upload.upload("local-u1", installed["install_id"], installed["revision"])
    result = local_skill_upload.upload("local-u1", installed["install_id"], installed["revision"])
    again = local_skill_upload.upload("local-u1", installed["install_id"], installed["revision"])
    assert result == again
    assert len(posts) == 2 and posts[0] == posts[1]
    assert skills.get("local-u1", installed["install_id"])["revision"] == installed["revision"]
    remote["revision"] = "changed-elsewhere"
    with pytest.raises(ValueError, match="cloud_revision_conflict"):
        local_skill_upload.upload("local-u1", installed["install_id"], installed["revision"])


def test_ui_editor_and_tool_share_registration(client, tmp_path):
    from core.services import local_skill_editor as editor
    installed = skills.install("local-u1", skill(tmp_path / "skill"))
    key = installed["skill_id"]
    draft = editor.detail("local-u1", key)
    editor.save("local-u1", SimpleNamespace(name=key, display_name="Edited", description="Edited skill",
        expected_revision=draft["revision"], instructions="New instructions", tags=["graph"], mcp_server_ids=[], user_intro="Intro", icon=None))
    editor.file_change("local-u1", key, "references/a.txt", b"extra")
    assert editor.detail("local-u1", key)["tags"] == ["graph"]
    assert editor.file_get("local-u1", key, "references/a.txt")["content"] == "extra"
    assert skills.get("local-u1", installed["install_id"])["revision"] != draft["revision"]
    assert zipfile.is_zipfile(io.BytesIO(editor.export("local-u1", key)))
    editor.uninstall("local-u1", key)
    assert skills.list_skills("local-u1") == []


def test_rejected_archive_does_not_publish_and_plugin_update_removes_old_children(client, tmp_path):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../escaped.txt", "bad")
    with pytest.raises(Exception):
        skills.install("local-u1", str(bad))
    assert not skills.list_skills("local-u1")
    root = tmp_path / "plugin"
    skill(root / "skills" / "old")
    (root / "plugin.json").write_text(json.dumps({"name": "replace-components"}))
    installed = plugins.install("local-u1", str(root))
    child = registry.install_id("skill", "local", installed["components"]["skills"][0])
    (root / "skills" / "old" / "SKILL.md").unlink()
    skill(root / "skills" / "new")
    updated = plugins.update("local-u1", installed["install_id"], str(root), installed["revision"])
    assert registry.get(child).state == "removed"
    with pytest.raises(ValueError, match="revision_conflict"):
        plugins.uninstall("local-u1", installed["install_id"], installed["revision"])
    plugins.uninstall("local-u1", installed["install_id"], updated["revision"])


def test_stale_form_cannot_overwrite_or_recreate_removed_skill(client, tmp_path):
    from fastapi import HTTPException
    from core.services import local_skill_editor as editor
    installed = skills.install("local-u1", skill(tmp_path / "skill"))
    body = SimpleNamespace(name=installed["skill_id"], display_name="Stale", description="Old",
        expected_revision=installed["revision"], instructions="Old", tags=[], mcp_server_ids=[], user_intro="", icon=None)
    updated = skills.update("local-u1", installed["install_id"], skill(tmp_path / "skill", "Changed"), installed["revision"])
    with pytest.raises(HTTPException) as error:
        editor.save("local-u1", body)
    assert error.value.status_code == 409
    skills.uninstall("local-u1", installed["install_id"], updated["revision"])
    with pytest.raises(HTTPException) as error:
        editor.save("local-u1", body)
    assert error.value.status_code == 409
    assert skills.list_skills("local-u1") == []


def test_plugin_card_reports_missing_runtime(client, tmp_path):
    from core.capabilities.device_plugin_catalog import plugin_entries
    root = tmp_path / "missing-runtime"
    root.mkdir()
    (root / "plugin.json").write_text(json.dumps({"name": "missing-runtime", "mcpServers": {
        "missing": {"command": "never-installed-command-928417", "args": []}}}))
    installed = plugins.install("local-u1", str(root))
    card = next(x for x in plugin_entries("local-u1") if x["install_id"] == installed["install_id"])
    assert card["callable"] is False
    assert card["readiness"]["ready"] is False
