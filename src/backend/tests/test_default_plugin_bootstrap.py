"""CE first-boot defaults shared by browser/Compose deployments."""

import importlib
from types import SimpleNamespace

import pytest
from core.db.models import AdminMcpServer, ContentBlock, InstalledPlugin
from core.services import plugin_service


def test_ce_default_plugins_bootstrap_once_and_are_globally_available(db_session):
    assert plugin_service.ensure_default_plugins_bootstrapped(db_session) is True

    installs = (
        db_session.query(InstalledPlugin)
        .filter(InstalledPlugin.owner_user_id.is_(None))
        .order_by(InstalledPlugin.slug)
        .all()
    )
    assert [row.slug for row in installs] == ["automation", "sites", "skill-manager"]
    assert all(row.created_by == "system_bootstrap" for row in installs)

    plugin_mcps = {
        row.source_plugin: row
        for row in db_session.query(AdminMcpServer)
        .filter(AdminMcpServer.owner_user_id.is_(None))
        .filter(AdminMcpServer.source_plugin.is_not(None))
        .all()
    }
    assert set(plugin_mcps) == {"automation", "skill-manager", "sites"}
    assert all(row.is_enabled is True for row in plugin_mcps.values())

    visible_to_user = plugin_service.list_installed(
        db_session,
        owner_user_id="fresh_ce_user",
        include_global=True,
    )
    assert {item["slug"] for item in visible_to_user} == {
        "automation",
        "skill-manager",
        "sites",
    }
    assert all(item["enabled"] is True for item in visible_to_user)

    marker = db_session.get(ContentBlock, plugin_service.DEFAULT_BOOTSTRAP_MARKER_ID)
    assert marker.payload == {
        "version": 1,
        "plugins": ["automation", "skill-manager", "sites"],
    }
    assert plugin_service.ensure_default_plugins_bootstrapped(db_session) is False


def test_default_plugin_marker_preserves_later_user_uninstall(db_session):
    plugin_service.ensure_default_plugins_bootstrapped(db_session)
    plugin_service.uninstall_plugin(
        db_session,
        "sites@global",
        owner_user_id=None,
    )

    assert plugin_service.ensure_default_plugins_bootstrapped(db_session) is False
    assert db_session.get(InstalledPlugin, "sites@global") is None


@pytest.mark.asyncio
async def test_ce_compose_startup_runs_default_plugin_bootstrap(monkeypatch):
    app_module = importlib.import_module("api.app")
    engine_module = importlib.import_module("core.db.engine")
    calls = []

    class FakeSession:
        def close(self):
            calls.append("close")

    session = FakeSession()
    monkeypatch.setattr(
        app_module,
        "settings",
        SimpleNamespace(
            edition=SimpleNamespace(edition="ce"),
            deploy=SimpleNamespace(is_local=False),
        ),
    )
    monkeypatch.setattr(engine_module, "SessionLocal", lambda: session)
    monkeypatch.setattr(
        plugin_service,
        "ensure_default_plugins_bootstrapped",
        lambda db: calls.append(db) or True,
    )

    await app_module._startup_seed_default_plugins()

    assert calls == [session, "close"]
