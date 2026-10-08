"""Retained modern packages reach ambient, explicit and progressive assembly."""
from pathlib import Path
import pytest
from core.capabilities import connectors, local_plugin_runtime, registry, skills, store
from core.config import catalog_resolver
from core.plugins.local import service
from core.services import desktop_cloud_bridge as bridge


@pytest.fixture
def installed(client, index_db, monkeypatch):
    root = Path(__file__).resolve().parents[2] / "plugin_bundles/marketplace/browser-automation"
    result = service.install("local-u1", str(root))
    monkeypatch.setattr(bridge, "_bridge_context", lambda: None)
    monkeypatch.setattr(bridge, "_local_server_ids", lambda: set())
    monkeypatch.setattr(bridge, "_mcp_json_local_declarations", lambda: {})
    monkeypatch.setattr(skills, "current_local_user_id", lambda: "local-u1")
    return result, index_db


def test_modern_owned_connector_reaches_default_and_explicit_bindings(installed):
    result, factory = installed
    sid = result["components"]["mcp"][0]
    from core.db.models import AdminMcpServer
    with factory() as db:
        assert db.query(AdminMcpServer).filter_by(server_id=sid).first() is None
        _, ambient = catalog_resolver._owned_enabled_ids(db, "local-u1", {})
        assert sid in ambient
        assert sid in bridge.apply_to_enabled_mcp_ids(ambient)
        output = []
        bridge.cloud_gateway_mcp_configs(ambient, resolution_out=output)
        assert sid in output[0].chosen
        _, explicit, _, missing = catalog_resolver.resolve_explicit_runtime_capabilities(
            db, "local-u1", mcp_ids=[sid]
        )
        assert explicit == [sid] and missing == []
        _, disabled = catalog_resolver._owned_enabled_ids(
            db, "local-u1", {"mcps": [{"id": sid, "enabled": False}]}
        )
        assert sid not in disabled
        assert sid not in bridge.apply_to_enabled_mcp_ids(disabled)
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
