"""End-to-end route coverage for the plugin UI contribution endpoints.

Installs the real industry plugin through the service layer, then drives the
three endpoints the frontend depends on and checks the properties that matter:

- contributions appear only while the plugin is installed **and enabled**;
- the payload never carries the upstream URL or the auth header;
- the data proxy refuses a source the manifest did not declare;
- module assets are served from the plugin package and cannot be escaped.
"""

from __future__ import annotations

import json

import pytest
from core.db.models import UserShadow

OWNER = "u-ui-routes"
SLUG = "industry-knowledge-center"


@pytest.fixture()
def client(db_session):
    from api.app import app
    from core.auth.backend import UserContext, get_current_user
    from core.db.engine import get_db
    from fastapi.testclient import TestClient

    db_session.add(
        UserShadow(user_id=OWNER, username="Tester", extra_data={"can_import_plugin": True})
    )
    db_session.commit()

    def _override_db():
        yield db_session

    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=OWNER, user_center_id="c1", username="Tester", email="t@e.com"
    )
    app.dependency_overrides[get_db] = _override_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _install(db_session):
    from core.services import plugin_service as ps

    return ps.install_plugin(db_session, SLUG, owner_user_id=OWNER)


def _contributions(client):
    resp = client.get("/v1/plugins/ui-contributions")
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["items"]


def test_contributions_appear_after_install(client, db_session):
    assert _contributions(client) == []

    _install(db_session)
    items = _contributions(client)

    assert [item["slug"] for item in items] == [SLUG]
    contributes = items[0]["contributes"]
    assert contributes["tool_views"], "no tool views surfaced"
    assert contributes["canvas_views"], "no canvas views surfaced"
    assert contributes["modules"], "no modules surfaced"


def test_contributions_never_carry_credentials(client, db_session):
    _install(db_session)
    blob = json.dumps(_contributions(client), ensure_ascii=False)

    assert "auth_token" not in blob
    assert "Authorization" not in blob
    assert "{config." not in blob
    # The source is still addressable by id, with its parameter contract.
    source = _contributions(client)[0]["contributes"]["data_sources"][0]
    assert source["id"] == "node_companies"
    assert "chainId" in source["params_schema"]
    assert "url" not in source


def test_contributions_disappear_when_the_plugin_is_disabled(client, db_session):
    """Turning the plugin off withdraws its interface — the whole point of the
    contribution model, so it is exercised through the real toggle endpoint."""
    result = _install(db_session)
    install_id = result["install_id"]
    assert _contributions(client)

    resp = client.patch(f"/v1/plugins/installed/{install_id}/enable", json={"enabled": False})
    assert resp.status_code == 200, resp.text

    assert _contributions(client) == [], "a disabled plugin still contributed UI"

    # …and comes back when re-enabled.
    resp = client.patch(f"/v1/plugins/installed/{install_id}/enable", json={"enabled": True})
    assert resp.status_code == 200, resp.text
    assert _contributions(client), "re-enabling did not restore the contributions"


def test_uninstall_withdraws_the_contributions(client, db_session):
    install_id = _install(db_session)["install_id"]
    assert _contributions(client)

    resp = client.delete(f"/v1/plugins/installed/{install_id}")
    assert resp.status_code == 200, resp.text

    assert _contributions(client) == []
    # The module assets go with it.
    assert client.get(f"/v1/plugins/{SLUG}/web/chain-overview/index.html").status_code == 404


def test_data_proxy_rejects_an_undeclared_source(client, db_session):
    _install(db_session)
    resp = client.post(f"/v1/plugins/{SLUG}/data/not_declared", json={})
    assert resp.status_code == 404, resp.text


def test_data_proxy_requires_the_plugin_to_be_installed(client):
    resp = client.post(f"/v1/plugins/{SLUG}/data/node_companies", json={})
    assert resp.status_code == 404, resp.text


def test_module_asset_is_served_with_a_locked_down_csp(client, db_session):
    _install(db_session)
    resp = client.get(f"/v1/plugins/{SLUG}/web/chain-overview/index.html")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/html")
    csp = resp.headers["content-security-policy"]
    assert "connect-src 'none'" in csp, "module could reach the network on its own"
    assert "default-src 'self'" in csp
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "module:ready" in resp.text, "served asset is not the plugin's module"


def test_module_asset_path_cannot_escape_the_package(client, db_session):
    _install(db_session)
    for attempt in ("../plugin.json", "..%2Fplugin.json", "chain-overview/../../plugin.json"):
        resp = client.get(f"/v1/plugins/{SLUG}/web/{attempt}")
        assert resp.status_code in (403, 404), f"{attempt} -> {resp.status_code}"
        assert "ai_chain_information_mcp" not in resp.text


def test_module_asset_requires_the_plugin_to_be_installed(client):
    resp = client.get(f"/v1/plugins/{SLUG}/web/chain-overview/index.html")
    assert resp.status_code == 404


def test_startup_refresh_backfills_pre_upgrade_installs(client, db_session):
    """The exact incident this pins: a plugin installed *before* the UI-contract
    upgrade has ``ui_contributions IS NULL`` (the migration adds the column, the
    manifest is only read at install time), so the chain-analysis canvas never
    auto-opened. The startup refresh must backfill such rows from the bundle.
    """
    from core.db.models import InstalledPlugin
    from core.services.plugin_service import refresh_builtin_ui_contributions

    install_id = _install(db_session)["install_id"]
    # Simulate the pre-upgrade row.
    row = db_session.get(InstalledPlugin, install_id)
    row.ui_contributions = None
    db_session.commit()
    assert _contributions(client) == [], "sanity: NULL column must yield no contributions"

    updated = refresh_builtin_ui_contributions(db_session)

    assert updated >= 1
    items = _contributions(client)
    assert [item["slug"] for item in items] == [SLUG]
    assert items[0]["contributes"]["canvas_views"], "canvas declaration missing after backfill"
    # Idempotent: a second run with nothing stale touches nothing.
    assert refresh_builtin_ui_contributions(db_session) == 0
