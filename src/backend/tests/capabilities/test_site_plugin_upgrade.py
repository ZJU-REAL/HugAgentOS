"""Installed builtin site instructions must reach the cloud download snapshot."""

import io
import json
import zipfile
from pathlib import Path

import pytest
from core.plugins.management import projection as plugin_projection
from tests.capabilities.test_local_project_site_publish import local_project

BUNDLE_VERSION = json.loads(
    (
        Path(__file__).resolve().parents[2] / "plugin_bundles/marketplace/sites/plugin.json"
    ).read_text()
)["version"]


@pytest.mark.parametrize("owner", [None, "owner"])
def test_installed_sites_upgrade_preserves_settings_and_exports_new_skill(
    local_project, monkeypatch, owner
):
    _, factory = local_project
    from core.db.models import AdminMcpServer, AdminSkill, InstalledPlugin
    from core.plugins import management as plugin_service
    from core.plugins.local.site_upgrade import upgrade_builtin_sites
    from core.services.desktop_capability_protocol import skill_content_hash
    from core.services.marketplace_service import build_skill_zip

    monkeypatch.setattr(plugin_projection, "_refresh_after_change", lambda *_: None)
    monkeypatch.setattr(plugin_projection, "_project_plugin_to_store", lambda *a, **k: None)
    with factory() as db:
        plugin_service.install_plugin(db, "sites", owner_user_id=owner)
        row = db.query(InstalledPlugin).filter(InstalledPlugin.owner_user_id == owner).one()
        row.version = "1.0.0"
        skill = next(
            db.get(AdminSkill, key)
            for key in row.component_ids["skills"]
            if db.get(AdminSkill, key).display_name == "site-builder"
        )
        skill.skill_content = "---\nname: old-site-builder\ndescription: old\n---\n编辑无需 site_id"
        skill.is_enabled = False
        old_hash = skill_content_hash(skill.skill_content, skill.extra_files or {})
        mcp = db.get(AdminMcpServer, row.component_ids["mcp"][0])
        mcp.is_enabled = False
        mcp.url = "http://configured.example/mcp"
        mcp.headers = {"X-Test-Config": "preserved"}
        tool_name = mcp.tools_json[0]["name"]
        schema = {"type": "object", "properties": {"site_id": {"type": "string"}}}
        retired = {
            "site_kv_list",
            "site_kv_get",
            "site_kv_set",
            "site_kv_delete",
            "manage_legacy_site_data",
        }
        mcp.tools_json = [{**mcp.tools_json[0], "inputSchema": schema}] + [
            {"name": name, "description": "Retired shipped tool"} for name in retired
        ]
        db.commit()
        ids = dict(row.component_ids)
        assert upgrade_builtin_sites(db) == 1
        assert row.version == BUNDLE_VERSION
        assert row.component_ids == ids
        assert skill.is_enabled is False
        assert mcp.is_enabled is False
        assert mcp.url == "http://configured.example/mcp"
        assert mcp.headers == {"X-Test-Config": "preserved"}
        assert next(t for t in mcp.tools_json if t["name"] == tool_name)["inputSchema"] == schema
        assert "list_sites" in skill.skill_content
        assert "编辑必须显式传原 site_id" in skill.skill_content
        assert "均无需 site_id" not in str(mcp.tools_json)
        assert retired.isdisjoint(tool["name"] for tool in mcp.tools_json)
        assert skill_content_hash(skill.skill_content, skill.extra_files or {}) != old_hash
        exported = build_skill_zip(skill.skill_id, skill.skill_content, skill.extra_files or {})
        with zipfile.ZipFile(io.BytesIO(exported)) as archive:
            text = archive.read(
                next(n for n in archive.namelist() if n.endswith("SKILL.md"))
            ).decode()
            assert "list_sites" in text
        assert upgrade_builtin_sites(db) == 0
        plugin_service.uninstall_plugin(db, row.install_id, owner_user_id=owner)
        assert upgrade_builtin_sites(db) == 0


@pytest.mark.parametrize("source,version", [("imported_codex", "1.0.0"), ("builtin", "9.0.0")])
def test_sites_upgrade_skips_custom_and_newer_installs(local_project, monkeypatch, source, version):
    _, factory = local_project
    from core.db.models import InstalledPlugin
    from core.plugins import management as plugin_service
    from core.plugins.local.site_upgrade import upgrade_builtin_sites

    monkeypatch.setattr(plugin_projection, "_refresh_after_change", lambda *_: None)
    monkeypatch.setattr(plugin_projection, "_project_plugin_to_store", lambda *a, **k: None)
    with factory() as db:
        plugin_service.install_plugin(db, "sites", owner_user_id=None)
        row = db.query(InstalledPlugin).one()
        row.source, row.version = source, version
        db.commit()
        assert upgrade_builtin_sites(db) == 0
        assert row.version == version


def test_site_upgrade_is_an_all_role_startup_step(local_project, monkeypatch):
    import asyncio
    import importlib

    from core.db.models import InstalledPlugin
    from core.plugins import management as plugin_service

    _, factory = local_project
    monkeypatch.setattr(plugin_projection, "_refresh_after_change", lambda *_: None)
    monkeypatch.setattr(plugin_projection, "_project_plugin_to_store", lambda *a, **k: None)
    with factory() as db:
        plugin_service.install_plugin(db, "sites", owner_user_id=None)
        row = db.query(InstalledPlugin).one()
        row.version = "1.0.0"
        db.commit()
    app = importlib.import_module("api.app")
    _step, _stop, gate, roles, _scope = next(
        s for s in app._startup_steps() if s[0] is app._startup_upgrade_sites_plugin
    )
    assert gate is True and roles == frozenset({app.SERVICE, app.EXECUTION_PLANE})
    asyncio.run(app._startup_upgrade_sites_plugin())
    with factory() as db:
        assert db.query(InstalledPlugin).one().version == BUNDLE_VERSION


def test_site_upgrade_refreshes_only_matching_local_projection(local_project, monkeypatch):
    from core.capabilities import plugins, registry, store
    from core.db.models import InstalledPlugin
    from core.plugins import management as plugin_service
    from core.plugins.local.site_upgrade import upgrade_builtin_sites

    root, factory = local_project
    monkeypatch.setenv("HUGAGENT_CAPS_ROOT", str(root.parent / "caps"))
    monkeypatch.setattr(registry, "SessionLocal", factory)
    monkeypatch.setattr(plugin_projection, "_refresh_after_change", lambda *_: None)
    with factory() as db:
        plugin_service.install_plugin(db, "sites", owner_user_id=None)
        row = db.query(InstalledPlugin).one()
        row.version = "1.0.0"
        db.commit()
        # The existing projection starts disabled and at the older bundle version.
        existing = registry.get("plugin:local:sites")
        comp = store.get("plugin", "local", "sites", existing.resolved_revision)
        definition = plugins.load_manifest(comp)
        definition["version"] = "1.0.0"
        plugins.publish_local_plugin(definition, owner_user_id=None)
        registry.set_enabled("plugin:local:sites", False)
        before = registry.get("plugin:local:sites")
        edges = registry.components_of(before.install_id)
        assert upgrade_builtin_sites(db) == 1
        after = registry.get(before.install_id)
        assert after.version == BUNDLE_VERSION
        assert after.enabled is False
        assert registry.components_of(after.install_id) == edges
        assert after.payload["db_install_id"] == row.install_id
        assert after.resolved_revision != before.resolved_revision
        manifest = plugins.load_manifest(
            store.get("plugin", "local", "sites", after.resolved_revision)
        )
        assert manifest["version"] == BUNDLE_VERSION
        # A same-slug private row must not overwrite the global projection.
        monkeypatch.setattr(plugin_projection, "_project_plugin_to_store", lambda *a, **k: None)
        plugin_service.install_plugin(db, "sites", owner_user_id="owner")
        private = db.query(InstalledPlugin).filter(InstalledPlugin.owner_user_id == "owner").one()
        private.version = "1.0.0"
        db.commit()
        assert upgrade_builtin_sites(db) == 1
        assert registry.get(after.install_id).payload["db_install_id"] == row.install_id


@pytest.mark.parametrize("owner", [None, "owner"])
def test_old_sites_install_gains_mcp_builder_without_enabling_disabled_skills(
    local_project, monkeypatch, owner
):
    from core.db.models import AdminMcpServer, AdminSkill, InstalledPlugin
    from core.plugins import management as plugin_service
    from core.plugins.local.site_upgrade import upgrade_builtin_sites

    _, factory = local_project
    monkeypatch.setattr(plugin_projection, "_refresh_after_change", lambda *_: None)
    monkeypatch.setattr(plugin_projection, "_project_plugin_to_store", lambda *a, **k: None)
    with factory() as db:
        plugin_service.install_plugin(db, "sites", owner_user_id=owner)
        row = db.query(InstalledPlugin).filter(InstalledPlugin.owner_user_id == owner).one()
        added = next(
            db.get(AdminSkill, key)
            for key in row.component_ids["skills"]
            if db.get(AdminSkill, key).display_name == "mcp-builder"
        )
        missing_id = added.skill_id
        old_ids = {
            **row.component_ids,
            "skills": [key for key in row.component_ids["skills"] if key != missing_id],
        }
        row.component_ids = old_ids
        row.version = "1.6.2"
        db.delete(added)
        for key in old_ids["skills"]:
            db.get(AdminSkill, key).is_enabled = False
        db.commit()
        assert upgrade_builtin_sites(db) == 1
        assert missing_id in row.component_ids["skills"]
        assert db.get(AdminSkill, missing_id).is_enabled is False
        assert "publish_mcp" in db.get(AdminSkill, missing_id).skill_content
        server = db.get(AdminMcpServer, row.component_ids["mcp"][0])
        assert "publish_mcp" in {tool["name"] for tool in server.tools_json}
        assert upgrade_builtin_sites(db) == 0
        plugin_service.uninstall_plugin(db, row.install_id, owner_user_id=owner)
        assert db.get(AdminSkill, missing_id) is None
