"""Admin embed policy and authenticated launch: real HTTP/persistence boundary."""

from copy import deepcopy

import pytest
from core.db.models import UserShadow
from tests.features.workbench.test_workbench_external_routes import APP, AUTH, ADMIN, BASE, client  # noqa: F401

EMBED = {
    "origin": "https://reports.example.org",
    "ready": "integrated",
    "allowForms": False,
    "originStorage": True,
}


def embedded():
    app = deepcopy(APP)
    app["target"]["mode"] = "embedded"
    return app


def test_admin_embed_config_is_required_and_launch_projects_minimal_policy(client):
    body = {"declaration": embedded(), "enabled": True}
    assert client.post(ADMIN, headers=AUTH, json=body).status_code == 422
    response = client.post(ADMIN, headers=AUTH, json={**body, "embedding": EMBED})
    assert response.status_code == 200, response.text
    launch = client.post(BASE + "/applications/external:wb_reports/launch", json={})
    assert launch.status_code == 200, launch.text
    value = launch.json()["data"]
    assert value["embedding"] == EMBED
    assert value["target"]["mode"] == "embedded"
    assert launch.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "config",
    [
        {**EMBED, "origin": "https://*.example.org"},
        {**EMBED, "origin": "https://reports.example.org/path"},
        {**EMBED, "origin": "https://other.example.org"},
        {**EMBED, "origin": "http://testserver"},
        {**EMBED, "originStorage": False},
        {**EMBED, "sandbox": "allow-top-navigation"},
        {**EMBED, "allowForms": "yes"},
    ],
)
def test_invalid_embed_policy_rejected_without_echoing_input(client, config):
    response = client.post(
        ADMIN, headers=AUTH, json={"declaration": embedded(), "enabled": True, "embedding": config}
    )
    assert response.status_code == 422
    assert "https://other.example.org" not in response.text


def test_embedding_does_not_bypass_live_permissions_or_revision(client, db_session):
    saved = client.post(
        ADMIN, headers=AUTH, json={"declaration": embedded(), "enabled": True, "embedding": EMBED}
    ).json()["data"]
    row = db_session.get(UserShadow, "wb-user")
    row.extra_data = {"allowed_apps": []}
    db_session.commit()
    assert (
        client.post(BASE + "/applications/external:wb_reports/launch", json={}).status_code == 404
    )
    row.extra_data = {"allowed_apps": ["wb_reports"]}
    db_session.commit()
    response = client.put(
        ADMIN + "/external:wb_reports",
        headers=AUTH,
        json={
            "declaration": embedded(),
            "enabled": False,
            "embedding": EMBED,
            "revision": saved["revision"],
        },
    )
    assert response.status_code == 200
    assert (
        client.post(BASE + "/applications/external:wb_reports/launch", json={}).status_code == 409
    )


def test_browser_mode_rejects_unused_embed_grants(client):
    assert (
        client.post(
            ADMIN, headers=AUTH, json={"declaration": APP, "enabled": True, "embedding": EMBED}
        ).status_code
        == 422
    )


def test_other_host_port_still_shares_host_cookie_scope(client):
    declaration = embedded()
    declaration["target"]["url"] = "http://testserver:4000/page"
    response = client.post(
        ADMIN,
        headers=AUTH,
        json={
            "declaration": declaration,
            "enabled": True,
            "embedding": {**EMBED, "origin": "http://testserver:4000"},
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "embedding_host_origin"
