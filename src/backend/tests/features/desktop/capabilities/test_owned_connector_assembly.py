"""Retained modern packages reach ambient, explicit and progressive assembly."""
from features.desktop.bridge import gateway as bridge_gateway
from features.desktop.capabilities import skill_account
from pathlib import Path
import pytest
from features.desktop.capabilities import connectors, local_plugin_runtime, registry, skills, store
from core.capabilities.catalog import resolver as catalog_resolver
from features.desktop.local.plugins import service as service
from features.desktop.bridge import client as bridge


@pytest.fixture
def installed(client, index_db, monkeypatch):
    root = Path(__file__).resolve().parents[4] / "extensions/plugins/marketplace/browser-automation"
    result = service.install("local-u1", str(root))
    monkeypatch.setattr(bridge_gateway, 'bridge_context', lambda: None)
    monkeypatch.setattr(bridge_gateway, 'local_server_ids', lambda: set())
    monkeypatch.setattr(bridge_gateway, 'mcp_json_local_declarations', lambda: {})
    monkeypatch.setattr(skill_account, 'current_local_user_id', lambda: "local-u1")
    return result, index_db


def test_modern_owned_connector_reaches_default_and_explicit_bindings(installed):
    result, factory = installed
    sid = result["components"]["mcp"][0]
    from core.db.models import AdminMcpServer
    with factory() as db:
        assert db.query(AdminMcpServer).filter_by(server_id=sid).first() is None
        _, ambient = catalog_resolver._owned_enabled_ids(db, "local-u1", {})
        assert sid in ambient
        assert sid in bridge_gateway.apply_to_enabled_mcp_ids(ambient)
        output = []
        bridge_gateway.cloud_gateway_mcp_configs(ambient, resolution_out=output)
        assert sid in output[0].chosen
        _, explicit, _, missing = catalog_resolver.resolve_explicit_runtime_capabilities(
            db, "local-u1", mcp_ids=[sid]
        )
        assert explicit == [sid] and missing == []
        _, disabled = catalog_resolver._owned_enabled_ids(
            db, "local-u1", {"mcps": [{"id": sid, "enabled": False}]}
        )
        assert sid not in disabled
        assert sid not in bridge_gateway.apply_to_enabled_mcp_ids(disabled)
    selected = connectors.resolve_bindings(connectors.db_candidates([], set(ambient)))
    assert selected.chosen[sid].install_id == "mcp:local:" + sid


@pytest.mark.parametrize("revocation", ["foreign", "disabled", "tampered"])
def test_modern_connector_cannot_revive_revoked_installation(installed, revocation):
    result, factory = installed
    sid = result["components"]["mcp"][0]
    row = registry.get(result["install_id"])
    user = "other" if revocation == "foreign" else "local-u1"
    if revocation == "disabled":
        registry.set_enabled(row.install_id, False)
    if revocation == "tampered":
        comp = store.get("plugin", "local", row.key, row.resolved_revision)
        (comp.path / "plugin.json").write_text("{}")
    assert sid not in local_plugin_runtime.configs(user)
    with factory() as db:
        assert sid not in catalog_resolver._owned_enabled_ids(db, user, {})[1]
        assert catalog_resolver.resolve_explicit_runtime_capabilities(
            db, user, mcp_ids=[sid]
        )[1] == []


def test_packaged_compose_host_reaches_the_local_plane(client, index_db, monkeypatch):
    from dataclasses import replace
    from core.config.settings import settings
    from extensions.mcp_servers._ports import PORTS
    monkeypatch.setattr("core.config.settings.settings",
                        replace(settings, server=replace(settings.server, mcp_host="127.0.0.1")))
    root = Path(__file__).resolve().parents[4] / "extensions/plugins/marketplace/automation"
    sid = service.install("local-u1", str(root))["components"]["mcp"][0]
    # The manifest names the compose service ``mcp``; a local plane listens on loopback.
    assert local_plugin_runtime.configs("local-u1")[sid]["url"] == f"http://127.0.0.1:{PORTS['automation_task']}/mcp"
