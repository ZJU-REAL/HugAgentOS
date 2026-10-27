"""Upgrade existing desktop managers without changing component identities."""

import json
import pytest
from features.desktop.capabilities import registry
from core.db.engine import Base
from core.db.models import AdminSkill, AdminMcpServer, InstalledPlugin, ContentBlock
from features.desktop.local.plugins import legacy_migration as migration
from features.desktop.local import management_migration as skill_migration


@pytest.fixture
def legacy(index_db, caps_root, monkeypatch):
    Base.metadata.create_all(index_db.kw["bind"])
    monkeypatch.setattr("core.db.engine.SessionLocal", index_db)
    monkeypatch.setattr('core.capabilities.mcp.service.SessionLocal', index_db)
    monkeypatch.setattr('features.desktop.capabilities.device_catalog.active', lambda: False)
    monkeypatch.setattr('features.desktop.capabilities.skill_account.account_authorized_for', lambda _: False)
    return index_db


@pytest.mark.parametrize("projected", [False, True])
def test_old_local_manager_upgrades_and_remains_usable(legacy, projected):
    from core.capabilities.plugins.packaging.sources import make_skill_id as _make_skill_id, make_server_id as _make_server_id
    from core.config.settings import settings
    from extensions.mcp_servers._ports import PORTS

    sid = _make_skill_id("skill-manager", "skill-creator", "owner")
    mid = _make_server_id("skill-manager", "skill_manager", "owner")
    with legacy() as db:
        db.add(
            InstalledPlugin(
                install_id="skill-manager@owner",
                slug="skill-manager",
                name="Skill manager",
                version="1.0.0",
                source="builtin",
                owner_user_id="owner",
                component_ids={"skills": [sid], "mcp": [mid]},
            )
        )
        db.add(
            AdminSkill(
                skill_id=sid,
                display_name="Creator",
                description="Old",
                skill_content=f"---\nname: {sid}\ndescription: Old creator\n---\nregister_skill",
                source_plugin="skill-manager",
                owner_user_id="owner",
            )
        )
        db.add(
            AdminMcpServer(
                server_id=mid,
                display_name="Manager",
                transport="streamable_http",
                url=f"http://{settings.server.mcp_host}:{PORTS['skill_manager']}/mcp/",
                source_plugin="skill-manager",
                owner_user_id="owner",
            )
        )
        db.commit()
    if projected:
        from features.desktop.capabilities.plugins import publish_local_plugin
        publish_local_plugin({"slug": "skill-manager", "name": "Skill manager",
                              "install_id": "skill-manager@owner",
                              "components": {"skills": [sid], "mcp": [mid]}},
                             owner_user_id="owner")
    assert migration.migrate() == 1
    assert migration.migrate() == 0
    from features.desktop.capabilities.local_plugin_runtime import configs
    from features.desktop.capabilities import device_catalog

    assert "install_skill" in [x["name"] for x in configs("owner")[mid]["manifest_tools"]]
    entry = device_catalog.plugin_entries(user_id="owner")[0]
    assert entry["skills"] == [sid]
    assert entry["mcp"] == [mid]
    assert not configs("other")
    with legacy() as db:
        assert not db.query(InstalledPlugin).count()
        assert not db.query(AdminSkill).count()
        backup = db.query(ContentBlock).filter(ContentBlock.id.like("local-plugin-v1:%")).one()
        from core.infra.crypto import decrypt_secret

        restored = json.loads(decrypt_secret(backup.payload["encrypted"]))
        assert restored["skills"][0]["skill_content"].endswith("register_skill")
    from features.desktop.local.plugins import service as local_plugin_service

    current = local_plugin_service.get("owner", entry["install_id"])
    local_plugin_service.uninstall("owner", current["install_id"], current["revision"])
    assert migration.migrate() == 0
    assert not device_catalog.plugin_entries(user_id="owner")


def test_personal_skill_migrates_once_without_resurrection(legacy):
    with legacy() as db:
        db.add(
            AdminSkill(
                skill_id="legacy-skill",
                display_name="Old",
                description="Old",
                owner_user_id="owner",
                skill_content="---\nname: legacy-skill\ndescription: Legacy\n---\nBody",
            )
        )
        db.commit()
    assert skill_migration.migrate() == 1
    from features.desktop.local.skills import service as local_skill_service

    iid = registry.install_id("skill", "local", "legacy-skill")
    current = local_skill_service.get("owner", iid)
    local_skill_service.uninstall("owner", iid, current["revision"])
    assert skill_migration.migrate() == 0
    assert registry.get(iid).state == "removed"
    with legacy() as db:
        assert (
            db.query(ContentBlock).filter(ContentBlock.id.like("local-management-v1:%")).count()
            == 1
        )


def test_invalid_legacy_package_rolls_back_rows_and_registry(legacy):
    with legacy() as db:
        db.add(
            InstalledPlugin(
                install_id="broken@owner",
                slug="broken",
                name="Broken",
                source="imported_codex",
                owner_user_id="owner",
            )
        )
        db.add(
            AdminSkill(
                skill_id="broken-skill",
                display_name="Broken",
                description="Broken",
                skill_content="missing metadata",
                source_plugin="broken",
                owner_user_id="owner",
            )
        )
        db.commit()
    with pytest.raises(Exception):
        migration.migrate()
    with legacy() as db:
        assert db.get(InstalledPlugin, "broken@owner") is not None
        assert db.get(AdminSkill, "broken-skill") is not None
    assert not registry.list_installations(kind="plugin")


def test_local_plugin_migration_finishes_before_catalog_snapshots():
    from api import lifecycle
    steps = list(lifecycle._startup_steps())
    by_name = {step.__name__: (index, gate, roles, scope)
               for index, (step, _, gate, roles, scope) in enumerate(steps)}
    index, gate, roles, scope = by_name["_startup_migrate_local_capabilities"]
    assert gate and roles == lifecycle._ALL_ROLES and scope is lifecycle._SINGLETON
    assert by_name["_startup_seed_default_plugins"][0] < index
    assert index < by_name["_startup_backfill_tool_schemas"][0]


def test_global_default_projection_publishes_components_before_deleting_source(legacy):
    from core.config.settings import settings
    from extensions.mcp_servers._ports import PORTS
    from features.desktop.capabilities.plugins import publish_local_plugin
    from features.desktop.capabilities.local_plugin_runtime import configs

    mid, sid = "skill-manager-skill_manager", "skill-manager-skill-creator"
    with legacy() as db:
        db.add(InstalledPlugin(install_id="skill-manager@global", slug="skill-manager",
                               name="Skill manager", version="1.0.0", source="builtin",
                               component_ids={"skills": [sid], "mcp": [mid]}))
        db.add(AdminSkill(skill_id=sid, display_name="Creator", description="Old",
                          skill_content=f"---\nname: {sid}\ndescription: Old\n---\nBody",
                          source_plugin="skill-manager"))
        db.add(AdminMcpServer(server_id=mid, display_name="Manager", transport="streamable_http",
                              url=f"http://{settings.server.mcp_host}:{PORTS['skill_manager']}/mcp/",
                              source_plugin="skill-manager"))
        db.commit()
    publish_local_plugin({"slug": "skill-manager", "install_id": "skill-manager@global",
                          "components": {"skills": [sid], "mcp": [mid]}}, owner_user_id=None)
    assert migration.migrate() == 1
    inst = registry.get("plugin:local:skill-manager")
    assert inst.payload["shared_installation"] is True
    assert inst.payload["from_db"] is False
    assert registry.get("skill:local:" + sid).ready
    assert mid in configs("local-admin")
    assert migration.migrate() == 0
    with legacy() as db:
        assert not db.query(InstalledPlugin).count()
        assert not db.query(AdminMcpServer).count()
