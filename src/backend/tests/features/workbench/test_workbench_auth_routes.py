"""Authenticated external launch and target-server exchange, through HTTP."""

from urllib.parse import parse_qs, urlsplit

from tests.db_support import share_factory
from tests.features.workbench.test_workbench_external_routes import AUTH, BASE, client, create  # noqa: F401

KEY = "external:wb_reports"
CONFIG = BASE + "/admin/external-auth/" + KEY
EXCHANGE = BASE + "/external-auth/exchange"
SECRET = "target-server-secret-with-at-least-32-characters"
CALLBACK = "https://reports.example.org/callback"


def configure(client, **changes):
    body = {
        "revision": 0,
        "mode": "exchange",
        "audience": "reports",
        "callback": CALLBACK,
        "initiationUrl": "https://reports.example.org/login/workbench",
        "clientSecret": SECRET,
        **changes,
    }
    return client.put(CONFIG, headers=AUTH, json=body)


def launch(client):
    response = client.post(BASE + "/applications/" + KEY + "/launch", json={})
    assert response.status_code == 200, response.text
    start_url = urlsplit(response.json()["data"]["target"]["url"])
    assert start_url._replace(fragment="").geturl() == "https://reports.example.org/login/workbench"
    intent = parse_qs(start_url.fragment)["intent"][0]
    state = "controlled-target-browser-state-1234567890"
    pending = client.post(
        BASE + "/external-auth/challenges",
        headers={"Authorization": "Bearer " + SECRET},
        json={
            "applicationKey": KEY,
            "audience": "reports",
            "callback": CALLBACK,
            "state": state,
            "intent": intent,
        },
    )
    assert pending.status_code == 200, pending.text
    values = parse_qs(urlsplit(pending.json()["data"]["continueUrl"]).fragment)
    authorized = client.post(
        BASE + "/external-auth/authorize",
        json={"challenge": values["challenge"][0], "state": state},
    )
    assert authorized.status_code == 200, authorized.text
    query = parse_qs(urlsplit(authorized.json()["data"]["url"]).fragment)
    return {
        "ticket": query["ticket"][0],
        "state": query["state"][0],
        "applicationKey": KEY,
        "audience": "reports",
        "callback": CALLBACK,
    }


def redeem(client, body, secret=SECRET):
    return client.post(EXCHANGE, json=body, headers={"Authorization": "Bearer " + secret})


def test_target_can_redeem_once_and_config_never_returns_secret(client):
    assert create(client).status_code == 200
    saved = configure(client)
    assert saved.status_code == 200, saved.text
    assert SECRET not in saved.text
    assert SECRET not in client.get(CONFIG, headers=AUTH).text
    body = launch(client)
    response = redeem(client, body)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["subject"] == "wb-user"
    assert response.headers["cache-control"] == "no-store"
    assert redeem(client, body).status_code == 401


def test_exchange_launch_rejects_cross_origin_json_and_errors_are_private(client):
    assert create(client).status_code == 200
    assert configure(client).status_code == 200
    response = client.post(
        BASE + "/applications/" + KEY + "/launch",
        headers={"Origin": "https://evil.example.org"},
        json={},
    )
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"


import pytest


@pytest.mark.parametrize(
    "field,value",
    [
        ("audience", "other"),
        ("callback", "https://reports.example.org/other"),
        ("state", "x" * 43),
        ("applicationKey", "external:other"),
    ],
)
def test_wrong_binding_never_returns_identity(client, field, value):
    assert create(client).status_code == 200
    assert configure(client).status_code == 200
    body = launch(client)
    original = dict(body)
    body[field] = value
    response = redeem(client, body)
    assert response.status_code in (401, 404)
    assert "wb-user" not in response.text
    assert response.headers["cache-control"] == "no-store"
    if field != "applicationKey":
        assert redeem(client, original).status_code == 401


def test_unauthenticated_target_cannot_consume_ticket(client):
    assert create(client).status_code == 200
    assert configure(client).status_code == 200
    body = launch(client)
    assert redeem(client, body, "wrong-target").status_code == 401
    assert redeem(client, body).status_code == 200


def test_default_thirty_second_expiry(client, monkeypatch):
    from features.workbench import external_auth

    assert create(client).status_code == 200
    assert configure(client).status_code == 200
    now = external_auth.time.time()
    body = launch(client)
    monkeypatch.setattr(external_auth.time, "time", lambda: now + 31)
    assert redeem(client, body).status_code == 401


def test_revocation_and_auth_rotation_reject_outstanding_tickets(client, db_session):
    from core.db.models import UserShadow

    assert create(client).status_code == 200
    assert configure(client).status_code == 200
    body = launch(client)
    user = db_session.get(UserShadow, "wb-user")
    user.extra_data = {"allowed_apps": []}
    db_session.commit()
    assert redeem(client, body).status_code == 404
    user.extra_data = {}
    db_session.commit()
    body = launch(client)
    assert configure(client, revision=1).status_code == 200
    assert redeem(client, body).status_code == 401


def test_concurrent_target_exchange_has_exactly_one_winner(client, db_session, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from api.app import app
    from core.db.engine import get_db
    from sqlalchemy.orm import sessionmaker

    assert create(client).status_code == 200
    assert configure(client).status_code == 200
    body = launch(client)
    factory = sessionmaker(bind=db_session.get_bind())

    def database():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    share_factory(monkeypatch, factory)
    barrier = Barrier(6)

    def exchange_once():
        barrier.wait()
        return redeem(client, body).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sorted(pool.map(lambda _: exchange_once(), range(6))) == [
            200,
            401,
            401,
            401,
            401,
            401,
        ]


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.org/callback",
        "https://reports.example.org/callback#state",
        "https://reports.example.org/callback?token=secret",
        "//reports.example.org/callback",
    ],
)
def test_auth_callback_rejects_open_redirect_and_embedded_credentials(client, url):
    assert create(client).status_code == 200
    result = configure(client, callback=url)
    assert result.status_code == 422
    assert "secret" not in result.text


def test_shared_identity_launch_uses_targets_own_standard_login(client):
    assert create(client).status_code == 200
    response = configure(
        client,
        mode="shared_identity",
        callback="",
        audience="",
        clientSecret=None,
        initiationUrl="",
        loginUrl="https://reports.example.org/oidc/login",
        identityProvider="corporate-idp",
    )
    assert response.status_code == 200, response.text
    result = client.post(BASE + "/applications/" + KEY + "/launch", json={})
    assert result.json()["data"]["target"]["url"] == "https://reports.example.org/oidc/login"


def test_auth_config_cas_and_secret_redaction_in_validation_and_logs(client, caplog):
    assert create(client).status_code == 200
    assert configure(client).status_code == 200
    assert configure(client).status_code == 409
    body = launch(client)
    assert redeem(client, body).status_code == 200
    invalid = configure(client, clientSecret=SECRET, unknown=SECRET)
    assert invalid.status_code == 422
    combined = invalid.text + caplog.text
    assert SECRET not in combined
    assert body["ticket"] not in combined
    assert body["state"] not in combined


def test_legacy_app_uses_explicit_target_adapter_without_host_token(client, db_session):
    from core.db.models import ContentBlock

    db_session.add(
        ContentBlock(
            id="app_config",
            payload={
                "apps": [
                    {
                        "id": "old",
                        "name": "Legacy",
                        "enabled": True,
                        "url": "https://reports.example.org/home",
                    }
                ]
            },
        )
    )
    db_session.commit()
    legacy = "legacy:old"
    result = client.put(
        BASE + "/admin/external-auth/" + legacy,
        headers=AUTH,
        json={
            "revision": 0,
            "mode": "exchange",
            "audience": "old",
            "callback": CALLBACK,
            "initiationUrl": "https://reports.example.org/login/workbench",
            "clientSecret": SECRET,
        },
    )
    assert result.status_code == 200, result.text
    result = client.post(BASE + "/applications/" + legacy + "/launch", json={})
    intent = parse_qs(urlsplit(result.json()["data"]["target"]["url"]).fragment)["intent"][0]
    pending = client.post(
        BASE + "/external-auth/challenges",
        headers={"Authorization": "Bearer " + SECRET},
        json={
            "applicationKey": legacy,
            "audience": "old",
            "callback": CALLBACK,
            "state": "controlled-legacy-browser-state-1234567890",
            "intent": intent,
        },
    )
    values = parse_qs(urlsplit(pending.json()["data"]["continueUrl"]).fragment)
    authorized = client.post(
        BASE + "/external-auth/authorize",
        json={"challenge": values["challenge"][0], "state": values["state"][0]},
    )
    url = urlsplit(authorized.json()["data"]["url"])
    assert url.query == ""
    values = parse_qs(url.fragment)
    result = redeem(
        client,
        {
            "applicationKey": legacy,
            "audience": "old",
            "callback": CALLBACK,
            "ticket": values["ticket"][0],
            "state": values["state"][0],
        },
    )
    assert result.status_code == 200


def test_host_logout_invalidates_unredeemed_session_ticket(client, monkeypatch):
    import asyncio
    from dataclasses import replace
    from types import SimpleNamespace

    from core.auth import session
    from core.config.settings import settings

    monkeypatch.setattr(
        session, "settings", SimpleNamespace(session=replace(settings.session, store_type="memory"))
    )
    token = asyncio.run(
        session.create_session(
            {"user_id": "wb-user", "user_center_id": "wb-user", "username": "Reader"}
        )
    )
    client.cookies.set(settings.session.cookie_name, token)
    assert create(client).status_code == 200
    assert configure(client).status_code == 200
    body = launch(client)
    result = client.post("/v1/auth/logout", json={})
    assert result.status_code == 200
    assert redeem(client, body).status_code == 401


def test_unconfigured_legacy_login_is_explicitly_unavailable(client, db_session):
    from core.db.models import ContentBlock

    db_session.add(
        ContentBlock(
            id="app_config",
            payload={
                "apps": [
                    {
                        "id": "old",
                        "name": "Legacy",
                        "enabled": True,
                        "url": "https://reports.example.org/home",
                    }
                ]
            },
        )
    )
    db_session.commit()
    response = client.post(BASE + "/applications/legacy:old/launch", json={})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "auth_configuration_required"
    assert (
        client.get(BASE + "/applications/legacy:old").json()["data"]["availability"]["reason"]
        == "auth_configuration_required"
    )
    # A reviewed independent target is enabled explicitly, without reusable tokens.
    configured = client.put(
        BASE + "/admin/external-auth/legacy:old",
        headers=AUTH,
        json={"revision": 0, "mode": "independent"},
    )
    assert configured.status_code == 200
    assert client.post(BASE + "/applications/legacy:old/launch", json={}).status_code == 200


def test_authorization_requires_authenticated_target_challenge(client):
    assert create(client).status_code == 200
    assert configure(client).status_code == 200
    body = {
        "applicationKey": KEY,
        "audience": "reports",
        "callback": CALLBACK,
        "state": "x" * 43,
        "intent": "x" * 43,
    }
    body["intent"] = "x" * 43
    result = client.post(BASE + "/external-auth/challenges", json=body)
    assert result.status_code == 401
    assert (
        client.post(
            BASE + "/external-auth/authorize", json={"challenge": "x" * 43, "state": "x" * 43}
        ).status_code
        == 401
    )


def test_approved_distinct_frontend_origin_works_but_wildcard_does_not(client, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace

    from features.workbench.api import workbench_auth

    original = workbench_auth.settings
    monkeypatch.setattr(
        workbench_auth,
        "settings",
        SimpleNamespace(
            server=replace(original.server, cors_origins="https://frontend.example.org,*"),
            session=original.session,
        ),
    )
    assert create(client).status_code == 200
    response = client.post(
        BASE + "/applications/" + KEY + "/launch",
        headers={"Origin": "https://frontend.example.org", "Sec-Fetch-Site": "cross-site"},
        json={},
    )
    assert response.status_code == 200
    assert (
        client.post(
            BASE + "/applications/" + KEY + "/launch",
            headers={"Origin": "https://evil.example.org"},
            json={},
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "domain,url",
    [
        (None, "http://testserver:9000/app"),
        (".example.org", "https://reports.example.org/home"),
    ],
)
def test_external_target_cannot_receive_host_session_cookie(client, monkeypatch, domain, url):
    from dataclasses import replace
    from types import SimpleNamespace

    from features.workbench.api import workbench

    original = workbench.settings
    monkeypatch.setattr(
        workbench,
        "settings",
        SimpleNamespace(session=replace(original.session, cookie_domain=domain)),
    )
    app = {
        "schemaVersion": "1.0.0",
        "id": "cookie_target",
        "title": "Target",
        "target": {"kind": "external", "mode": "browser", "url": url},
    }
    assert create(client, app).status_code == 200
    result = client.post(BASE + "/applications/external:cookie_target/launch", json={})
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "host_cookie_scope_overlap"


def test_authorize_cannot_change_launch_user_or_original_session(client, db_session):
    from api.app import app
    from core.auth.backend import UserContext, get_current_user
    from core.db.models import UserShadow

    assert create(client).status_code == 200
    assert configure(client).status_code == 200
    start = client.post(BASE + "/applications/" + KEY + "/launch", json={}).json()["data"]
    values = parse_qs(urlsplit(start["target"]["url"]).fragment)
    state = "target-browser-account-switch-state-1234567890"
    pending = client.post(
        BASE + "/external-auth/challenges",
        headers={"Authorization": "Bearer " + SECRET},
        json={
            "applicationKey": KEY,
            "audience": "reports",
            "callback": CALLBACK,
            "state": state,
            "intent": values["intent"][0],
        },
    )
    assert pending.status_code == 200, pending.text
    challenge = parse_qs(urlsplit(pending.json()["data"]["continueUrl"]).fragment)["challenge"][0]
    db_session.add(UserShadow(user_id="wb-other", username="Other", extra_data={}))
    db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="wb-other", user_center_id="wb-other", username="Other"
    )
    result = client.post(
        BASE + "/external-auth/authorize", json={"challenge": challenge, "state": state}
    )
    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "launch_identity_mismatch"
