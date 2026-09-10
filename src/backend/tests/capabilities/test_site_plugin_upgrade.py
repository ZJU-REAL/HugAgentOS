"""Installed builtin site instructions must reach the cloud download snapshot."""

import io
import zipfile
import pytest
from tests.capabilities.test_local_project_site_publish import local_project


@pytest.mark.parametrize("owner", [None, "owner"])
def test_installed_sites_upgrade_preserves_settings_and_exports_new_skill(
    local_project, monkeypatch, owner
):
    _, factory = local_project
    from core.services import plugin_service
    from core.services.site_plugin_upgrade import upgrade_builtin_sites
    from core.db.models import InstalledPlugin, AdminSkill, AdminMcpServer
    from core.services.desktop_capability_protocol import skill_content_hash
    from core.services.marketplace_service import build_skill_zip

    monkeypatch.setattr(plugin_service, "_refresh_after_change", lambda *_: None)
    monkeypatch.setattr(plugin_service, "_project_plugin_to_store", lambda *a, **k: None)
    with factory() as db:
        plugin_service.install_plugin(db, "sites", owner_user_id=owner)
        row = db.query(InstalledPlugin).filter(InstalledPlugin.owner_user_id == owner).one()
        row.version = "1.0.0"
        skill = db.get(AdminSkill, row.component_ids["skills"][0])
        skill.skill_content = "---\nname: old-site-builder\ndescription: old\n---\n编辑无需 site_id"
        skill.is_enabled = False
        old_hash = skill_content_hash(skill.skill_content, skill.extra_files or {})
        mcp = db.get(AdminMcpServer, row.component_ids["mcp"][0])
        mcp.is_enabled = False
        mcp.url = "http://configured.example/mcp"
        mcp.headers = {"X-Test-Config": "preserved"}
        db.commit()
        ids = dict(row.component_ids)
        assert upgrade_builtin_sites(db) == 1
        assert row.version == "1.2.0"
        assert row.component_ids == ids
        assert skill.is_enabled is False
        assert mcp.is_enabled is False
        assert mcp.url == "http://configured.example/mcp"
        assert mcp.headers == {"X-Test-Config": "preserved"}
        assert "list_project_sites" in skill.skill_content
        assert "编辑必须显式传原 site_id" in skill.skill_content
        assert "均无需 site_id" not in str(mcp.tools_json)
        assert skill_content_hash(skill.skill_content, skill.extra_files or {}) != old_hash
        exported = build_skill_zip(skill.skill_id, skill.skill_content, skill.extra_files or {})
        with zipfile.ZipFile(io.BytesIO(exported)) as archive:
            text = archive.read(
                next(n for n in archive.namelist() if n.endswith("SKILL.md"))
            ).decode()
            assert "list_project_sites" in text
        assert upgrade_builtin_sites(db) == 0
        plugin_service.uninstall_plugin(db, row.install_id, owner_user_id=owner)
        assert upgrade_builtin_sites(db) == 0


@pytest.mark.parametrize("source,version", [("imported_codex", "1.0.0"), ("builtin", "9.0.0")])
def test_sites_upgrade_skips_custom_and_newer_installs(local_project, monkeypatch, source, version):
    _, factory = local_project
    from core.services.site_plugin_upgrade import upgrade_builtin_sites
    from core.services import plugin_service
    from core.db.models import InstalledPlugin

    monkeypatch.setattr(plugin_service, "_refresh_after_change", lambda *_: None)
    monkeypatch.setattr(plugin_service, "_project_plugin_to_store", lambda *a, **k: None)
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
    from core.services import plugin_service
    from core.db.models import InstalledPlugin

    _, factory = local_project
    monkeypatch.setattr(plugin_service, "_refresh_after_change", lambda *_: None)
    monkeypatch.setattr(plugin_service, "_project_plugin_to_store", lambda *a, **k: None)
    with factory() as db:
        plugin_service.install_plugin(db, "sites", owner_user_id=None)
        row = db.query(InstalledPlugin).one()
        row.version = "1.0.0"
        db.commit()
    app = importlib.import_module("api.app")
    _, gate, roles = next(
        s for s in app._startup_steps() if s[0] is app._startup_upgrade_sites_plugin
    )
    assert gate is True and roles == frozenset({app.SERVICE, app.EXECUTION_PLANE})
    asyncio.run(app._startup_upgrade_sites_plugin())
    with factory() as db:
        assert db.query(InstalledPlugin).one().version == "1.2.0"


def test_site_upgrade_refreshes_only_matching_local_projection(local_project, monkeypatch):
    from core.services import plugin_service
    from core.services.site_plugin_upgrade import upgrade_builtin_sites
    from core.capabilities import registry, plugins, store
    from core.db.models import InstalledPlugin

    root, factory = local_project
    monkeypatch.setenv("HUGAGENT_CAPS_ROOT", str(root.parent / "caps"))
    monkeypatch.setattr(registry, "SessionLocal", factory)
    monkeypatch.setattr(plugin_service, "_refresh_after_change", lambda *_: None)
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
        assert after.version == "1.2.0"
        assert after.enabled is False
        assert registry.components_of(after.install_id) == edges
        assert after.payload["db_install_id"] == row.install_id
        assert after.resolved_revision != before.resolved_revision
        manifest = plugins.load_manifest(
            store.get("plugin", "local", "sites", after.resolved_revision)
        )
        assert manifest["version"] == "1.2.0"
        # A same-slug private row must not overwrite the global projection.
        monkeypatch.setattr(plugin_service, "_project_plugin_to_store", lambda *a, **k: None)
        plugin_service.install_plugin(db, "sites", owner_user_id="owner")
        private = db.query(InstalledPlugin).filter(InstalledPlugin.owner_user_id == "owner").one()
        private.version = "1.0.0"
        db.commit()
        assert upgrade_builtin_sites(db) == 1
        assert registry.get(after.install_id).payload["db_install_id"] == row.install_id
