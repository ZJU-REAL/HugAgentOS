"""OAuth client registration: CIMD, administrator OAuth Apps, state callbacks, inspection."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import pytest
from core.capabilities.mcp import oauth, oauth_client, oauth_storage
from features.marketplace import mcp_administration, mcp_curated, mcp_listing
from features.marketplace.core_catalog import MarketplaceCatalogProvider
from features.marketplace.models import McpMarketOAuthClient
from tests.features.marketplace.mcp_marketplace_support import db  # noqa: F401 - fixture


class MemoryState:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def put(self, key, value, **kwargs):
        self.values[key] = value


def _public_base(monkeypatch, base: str) -> None:
    monkeypatch.setattr(
        oauth_client,
        "settings",
        SimpleNamespace(server=SimpleNamespace(mcp_oauth_public_base_url=base, is_prod=False)),
    )


def _oauth_method(listing: dict) -> dict:
    return next(m for m in listing["auth_config"]["methods"] if m["type"] == "oauth2")


@pytest.mark.parametrize("base", ["", "http://intranet.example.test/api"])
def test_cimd_is_unavailable_without_https_public_base(monkeypatch, base):
    _public_base(monkeypatch, base)
    assert oauth_client.client_metadata_url() is None
    assert oauth_client.client_metadata_document() is None
    assert oauth_client.cimd_unavailable_reason()


def test_cimd_document_names_its_own_url_and_fixed_callback(monkeypatch):
    _public_base(monkeypatch, "https://agent.example.test/api")
    document = oauth_client.client_metadata_document()
    assert document["client_id"] == (
        "https://agent.example.test/api/v1/mcp-market/oauth/client-metadata.json"
    )
    assert document["redirect_uris"] == [
        "https://agent.example.test/api/v1/mcp-market/oauth/callback"
    ]
    assert document["token_endpoint_auth_method"] == "none"


def test_cimd_route_serves_document_only_when_configured(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from features.marketplace.api import mcp_marketplace

    app = FastAPI()
    app.include_router(mcp_marketplace.router)
    from api.middleware.error_handler import setup_error_handlers

    setup_error_handlers(app)
    client = TestClient(app)
    _public_base(monkeypatch, "")
    assert client.get("/v1/mcp-market/oauth/client-metadata.json").status_code == 404
    _public_base(monkeypatch, "https://agent.example.test/api")
    response = client.get("/v1/mcp-market/oauth/client-metadata.json")
    assert response.status_code == 200
    assert response.json()["client_id"].endswith("/oauth/client-metadata.json")


def test_admin_oauth_app_on_curated_template_is_encrypted_and_hides_client_fields(db):
    mcp_curated.ensure_curated_market_items(db)
    before = mcp_listing.get_market_item(db, "github-official", owner_user_id="u1", viewer_user_id="u1")
    assert _oauth_method(before)["client_id_required"] is True

    result = mcp_administration.update_market_item(
        db,
        "github-official",
        oauth_registration="admin_app",
        oauth_client_id="Iv1.admin",
        oauth_client_secret="admin-secret-value",
        updated_by="admin",
    )
    assert "admin-secret-value" not in json.dumps(result, ensure_ascii=False)
    row = db.query(McpMarketOAuthClient).filter_by(slug="github-official").one()
    assert row.client_id == "Iv1.admin"
    assert row.client_secret and "admin-secret-value" not in row.client_secret

    listing = mcp_listing.get_market_item(db, "github-official", owner_user_id="u1", viewer_user_id="u1")
    method = _oauth_method(listing)
    assert method["client_registration"] == "admin_app"
    assert method["client_id_required"] is False
    assert method["client_secret_required"] is False

    # Re-seeding the curated template must not drop the administrator's choice.
    mcp_curated.ensure_curated_market_items(db)
    again = mcp_listing.get_market_item(db, "github-official", owner_user_id="u1", viewer_user_id="u1")
    assert _oauth_method(again)["client_registration"] == "admin_app"

    mcp_administration.update_market_item(db, "github-official", oauth_registration="manual")
    assert db.query(McpMarketOAuthClient).filter_by(slug="github-official").first() is None
    restored = mcp_listing.get_market_item(db, "github-official", owner_user_id="u1", viewer_user_id="u1")
    assert _oauth_method(restored)["client_id_required"] is True


def test_admin_oauth_app_keeps_secret_when_only_id_unchanged(db):
    mcp_curated.ensure_curated_market_items(db)
    mcp_administration.update_market_item(
        db, "github-official", oauth_registration="admin_app",
        oauth_client_id="Iv1.admin", oauth_client_secret="first-secret",
    )
    mcp_administration.update_market_item(db, "github-official", oauth_registration="admin_app")
    from features.marketplace import mcp_oauth_clients

    assert mcp_oauth_clients.admin_client(db, "github-official") == ("Iv1.admin", "first-secret")
    # A new client ID never inherits the previous client's secret.
    mcp_administration.update_market_item(
        db, "github-official", oauth_registration="admin_app", oauth_client_id="Iv1.other",
    )
    assert mcp_oauth_clients.admin_client(db, "github-official") == ("Iv1.other", "")


def test_registration_choice_is_written_to_non_curated_listing(db):
    mcp_curated.ensure_curated_market_items(db)
    from features.marketplace.models import McpMarketVersion

    version = db.query(McpMarketVersion).filter_by(slug="gitlab-official").one()
    version.approved_by = "admin"
    db.commit()
    mcp_administration.update_market_item(
        db, "gitlab-official", oauth_registration="dcr", oauth_scopes=["read_api", " mcp "],
    )
    listing = mcp_listing.get_market_item(db, "gitlab-official", owner_user_id="u1", viewer_user_id="u1")
    method = _oauth_method(listing)
    assert method["client_registration"] == "dcr"
    assert method["scopes"] == ["read_api", "mcp"]


async def _run_flow(db, monkeypatch, *, slug, client_id="", client_secret=""):
    state = MemoryState()
    monkeypatch.setattr(oauth, "get_ephemeral_state", lambda: state)
    monkeypatch.setattr(oauth, "validate_remote_mcp_url", AsyncMock())
    captured = {}

    async def probe(row, *, validate, oauth_provider, timeout_seconds):
        captured["provider"] = oauth_provider
        captured["client_info"] = await oauth_provider.context.storage.get_client_info()
        return True, ""

    monkeypatch.setattr(oauth, "probe_mcp_connectivity", probe)
    real = MarketplaceCatalogProvider()
    installed = AsyncMock(return_value={"installed": True})
    catalog = SimpleNamespace(
        mcp_version=real.mcp_version,
        normalize_auth_config=real.normalize_auth_config,
        installation_secrets=real.installation_secrets,
        mcp_oauth_install_policy=real.mcp_oauth_install_policy,
        mcp_oauth_admin_client=real.mcp_oauth_admin_client,
        install_market_item=installed,
    )
    monkeypatch.setattr(oauth, "marketplace_catalog", lambda: catalog)
    flow = oauth.OAuthInstallFlow(
        flow_id="flow-under-test", slug=slug, owner_user_id="u1", installed_by="u1",
        auth_method="oauth2", credentials={},
        callback_url="https://agent.example.test/api/v1/mcp-market/oauth/callback",
        client_id=client_id, client_secret=client_secret,
    )
    await oauth.run_flow(flow)
    return flow, captured, installed


@pytest.mark.asyncio
async def test_flow_offers_cimd_only_for_auto_registration_with_https_base(db, monkeypatch):
    mcp_curated.ensure_curated_market_items(db)
    _public_base(monkeypatch, "https://agent.example.test/api")
    flow, captured, installed = await _run_flow(db, monkeypatch, slug="gitlab-official")
    assert flow.status == "completed", flow.error
    assert captured["provider"].context.client_metadata_url == (
        "https://agent.example.test/api/v1/mcp-market/oauth/client-metadata.json"
    )
    assert captured["client_info"] is None
    # Curated templates are admin-published, so their unchanged endpoint keeps
    # the same network allowance token installs get.
    bundle = installed.await_args.kwargs["oauth_bundle"]
    assert bundle["allow_private_network"] is True


@pytest.mark.asyncio
async def test_flow_uses_admin_oauth_app_over_installer_client(db, monkeypatch):
    mcp_curated.ensure_curated_market_items(db)
    _public_base(monkeypatch, "https://agent.example.test/api")
    mcp_administration.update_market_item(
        db, "github-official", oauth_registration="admin_app",
        oauth_client_id="Iv1.admin", oauth_client_secret="admin-secret",
    )
    flow, captured, _ = await _run_flow(
        db, monkeypatch, slug="github-official", client_id="user-client", client_secret="user-secret",
    )
    assert flow.status == "completed", flow.error
    assert captured["client_info"].client_id == "Iv1.admin"
    assert captured["client_info"].client_secret == "admin-secret"
    assert captured["provider"].context.client_metadata_url is None


@pytest.mark.asyncio
async def test_start_does_not_require_client_fields_when_admin_app_exists(db, monkeypatch):
    mcp_curated.ensure_curated_market_items(db)
    mcp_administration.update_market_item(
        db, "github-official", oauth_registration="admin_app", oauth_client_id="Iv1.admin",
    )
    state = MemoryState()
    monkeypatch.setattr(oauth, "get_ephemeral_state", lambda: state)
    monkeypatch.setattr(oauth, "DISCOVERY_GRACE_SECONDS", 0.01)
    started = {}

    async def fake_run(flow):
        started["flow"] = flow

    monkeypatch.setattr(oauth, "run_flow", fake_run)
    result = await oauth.start_oauth_install(
        slug="github-official", owner_user_id="u1", installed_by="u1", auth_method="oauth2",
        credentials={}, api_base="https://agent.example.test/api",
        client_id="ignored", client_secret="ignored",
    )
    assert result["flow_id"]
    flow = started["flow"]
    assert flow.client_id == "" and flow.client_secret == ""
    assert flow.callback_url == "https://agent.example.test/api/v1/mcp-market/oauth/callback"
    oauth.process_state().flows.clear()


@pytest.mark.asyncio
async def test_callback_without_flow_id_resolves_through_state(monkeypatch):
    state = MemoryState()
    monkeypatch.setattr(oauth, "get_ephemeral_state", lambda: state)
    await state.put(oauth.state_key("random-state"), "mcpoauth_x")
    assert await oauth.resolve_callback_flow(None, "random-state") == "mcpoauth_x"
    assert await oauth.resolve_callback_flow("legacy", "random-state") == "legacy"
    from core.infra.exceptions import ResourceNotFoundError

    with pytest.raises(ResourceNotFoundError):
        await oauth.resolve_callback_flow(None, "unknown")


@pytest.mark.asyncio
async def test_refresh_provider_keeps_install_private_network_allowance(monkeypatch):
    calls = []

    async def validate(url, *, allow_private_network=False, require_https=True):
        calls.append(allow_private_network)

    monkeypatch.setattr(oauth_storage, "validate_remote_mcp_url", validate)
    for allowed in (True, False):
        provider = oauth_storage.build_oauth_provider(
            "http://10.0.0.5/mcp", {"allow_private_network": allowed}
        )
        await provider.mcp_request_hook(httpx2.Request("GET", "http://10.0.0.5/token"))
    assert calls == [True, False]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "as_metadata,base,expected",
    [
        ({"registration_endpoint": "https://as.test/register"}, "", ("auto", True, False)),
        ({"client_id_metadata_document_supported": True}, "https://agent.test/api", ("auto", False, True)),
        ({"client_id_metadata_document_supported": True}, "", ("admin_app", False, True)),
        ({}, "", ("admin_app", False, False)),
    ],
)
async def test_inspect_reports_registration_capabilities(monkeypatch, as_metadata, base, expected):
    _public_base(monkeypatch, base)
    monkeypatch.setattr(oauth_client, "validate_remote_mcp_url", AsyncMock())

    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if request.method == "POST" and path == "/mcp":
            return httpx2.Response(
                401,
                headers={"WWW-Authenticate": 'Bearer resource_metadata="https://mcp.test/.well-known/oauth-protected-resource/mcp"'},
            )
        if path == "/.well-known/oauth-protected-resource/mcp":
            return httpx2.Response(200, json={"resource": "https://mcp.test/mcp", "authorization_servers": ["https://as.test"]})
        if request.url.host == "as.test" and path == "/.well-known/oauth-authorization-server":
            return httpx2.Response(200, json={
                "issuer": "https://as.test",
                "authorization_endpoint": "https://as.test/authorize",
                "token_endpoint": "https://as.test/token",
                "code_challenge_methods_supported": ["S256"],
                **as_metadata,
            })
        return httpx2.Response(404)

    real_client = httpx2.AsyncClient
    monkeypatch.setattr(
        oauth_client.httpx2,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx2.MockTransport(handler), **kwargs),
    )
    result = await oauth_client.inspect_authorization("https://mcp.test/mcp", allow_private_network=True)
    assert result["error"] == ""
    assert result["authorization_server"] == "https://as.test"
    assert result["pkce_s256"] is True
    assert (result["recommended_registration"], result["dcr"], result["cimd"]) == expected


@pytest.mark.asyncio
async def test_inspect_explains_missing_oauth_metadata(monkeypatch):
    _public_base(monkeypatch, "")
    monkeypatch.setattr(oauth_client, "validate_remote_mcp_url", AsyncMock())
    real_client = httpx2.AsyncClient
    monkeypatch.setattr(
        oauth_client.httpx2,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx2.MockTransport(lambda r: httpx2.Response(404)), **kwargs),
    )
    result = await oauth_client.inspect_authorization("https://mcp.test/mcp", allow_private_network=False)
    assert "未找到 OAuth 授权服务器元数据" in result["error"]
