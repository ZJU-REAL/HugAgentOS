"""Manager commands and the desktop installation API share one source of truth."""
from pathlib import Path
import pytest
from core.services import local_skill_service as service


def test_install_update_uninstall_visible_to_device_api(client, tmp_path):
    folder = tmp_path / "graph-skill"
    folder.mkdir()
    (folder / "SKILL.md").write_text("---\nname: graph-skill\ndescription: Build graphs\n---\nVersion one")
    installed = service.install("local-u1", str(folder))
    iid = installed["install_id"]
    assert installed["source"] == "local"
    from core.capabilities import device_catalog
    assert any(x["id"] == installed["skill_id"] for x in device_catalog.catalog_overlay(user_id="local-u1")["skills"])
    assert not any(x["id"] == installed["skill_id"] for x in device_catalog.catalog_overlay(user_id="other")["skills"])
    listing = client.get("/v1/desktop/capabilities/installations?kind=skill").json()["data"]["items"]
    assert any(x["install_id"] == iid for x in listing)
    (folder / "SKILL.md").write_text("---\nname: graph-skill\ndescription: Build graphs\n---\nVersion two")
    updated = service.update("local-u1", iid, str(folder), installed["revision"])
    assert updated["revision"] != installed["revision"]
    with pytest.raises(Exception, match="revision"):
        service.update("local-u1", iid, str(folder), installed["revision"])
    with pytest.raises(Exception):
        service.get("other-user", iid)
    service.uninstall("local-u1", iid, updated["revision"])
    assert folder.exists()
    listing = client.get("/v1/desktop/capabilities/installations?kind=skill").json()["data"]["items"]
    assert not any(x["install_id"] == iid and x["state"] != "removed" for x in listing)


def test_plugin_install_has_components_and_appears_in_installed_page(client, tmp_path):
    import json
    from core.services import local_plugin_service as plugins
    folder = tmp_path / "graph-plugin"
    child = folder / "skills" / "graph-builder"
    child.mkdir(parents=True)
    (folder / "plugin.json").write_text(json.dumps({"name":"graph-plugin", "version":"1.0.0", "description":"Graph plugin"}))
    (child / "SKILL.md").write_text("---\nname: graph-builder\ndescription: Build graphs\n---\nUse this skill")
    installed = plugins.install("local-u1", str(folder))
    assert installed["source"] == "local"
    items = client.get("/v1/desktop/capabilities/installations?kind=plugin").json()["data"]["items"]
    assert any(x["install_id"] == installed["install_id"] for x in items)
    from core.capabilities import device_catalog
    assert any(x["install_id"] == installed["install_id"] for x in device_catalog.plugin_entries(user_id="local-u1"))
    assert not device_catalog.plugin_entries(user_id="other")
    detail = plugins.get("local-u1", installed["install_id"])
    assert len(detail["components"]["skills"]) == 1
    plugins.uninstall("local-u1", installed["install_id"], installed["revision"])
    assert plugins.list_plugins("local-u1") == []
