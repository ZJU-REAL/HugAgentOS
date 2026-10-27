"""Self-service OAuth MCPs: connect privately, submit without secrets, switch listings to OAuth."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from core.capabilities.mcp import oauth
from core.capabilities.mcp.management import (
    decrypt_mcp_headers,
    encrypt_mcp_headers,
    mcp_oauth_bundle_storage_key,
)
from core.db.models import AdminMcpServer
from features.marketplace import mcp_administration, mcp_curated, mcp_listing, mcp_submissions
from features.marketplace.models import McpMarketSubmission
from tests.features.marketplace.mcp_marketplace_support import (  # noqa: F401 - fixture
    _approved_market,
    _patch_probe,
    _tools,
    db,
)


class MemoryState:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def put(self, key, value, **kwargs):
        self.values[key] = value


def _target(**overrides):
    return {
        "display_name": "My OAuth MCP",
        "description": "",
        "user_intro": None,
        "transport": "streamable_http",
        "url": "https://mcp.example.test/mcp",
        "headers": {"X-Tenant": "acme"},
        "icon": None,
        "scopes": ["read"],
        **overrides,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("private_allowed", [False, True])
@pytest.mark.parametrize("client_id,expected", [("", "auto"), ("my-app", "manual")])
async def test_private_flow_saves_owner_row_with_bundle(db, monkeypatch, private_allowed, client_id, expected):
    monkeypatch.setattr(oauth, "get_ephemeral_state", lambda: MemoryState())
    validate = AsyncMock()
    monkeypatch.setattr(oauth, "validate_remote_mcp_url", validate)
    monkeypatch.setattr(
        oauth, "settings",
        SimpleNamespace(server=SimpleNamespace(mcp_self_service_allow_private_network=private_allowed)),
    )
    captured = {}

    async def probe(row, *, validate, oauth_provider, timeout_seconds):
        captured["client_info"] = await oauth_provider.context.storage.get_client_info()
        captured["scope"] = oauth_provider.context.client_metadata.scope
        return True, ""

    monkeypatch.setattr(oauth, "probe_mcp_connectivity", probe)
    flow = oauth.OAuthInstallFlow(
        flow_id="private-flow", slug="", owner_user_id="u1", installed_by="u1",
        auth_method="oauth2", credentials={},
        callback_url="https://agent.example.test/api/v1/mcp-market/oauth/callback",
        client_id=client_id, client_secret="s3cret" if client_id else "",
        private_target=_target(),
    )
    await oauth.run_flow(flow)

    assert flow.status == "completed", flow.error
    assert validate.await_args.kwargs["allow_private_network"] is private_allowed
    assert captured["scope"] == "read"
    assert (captured["client_info"].client_id if captured["client_info"] else "") == client_id
    row = db.query(AdminMcpServer).filter_by(server_id=flow.result["server_id"]).one()
    assert row.owner_user_id == "u1" and row.is_enabled
    headers = decrypt_mcp_headers(row.headers)
    assert headers["X-Tenant"] == "acme"
    bundle = json.loads(headers[mcp_oauth_bundle_storage_key()])
    assert bundle["client_registration"] == expected
    assert bundle["allow_private_network"] is private_allowed


def _oauth_source(db, *, registration="auto", secret_supplied=False):
    bundle = {
        "tokens": {"access_token": "access-secret", "token_type": "Bearer", "refresh_token": "refresh-secret"},
        "client_info": {"client_id": "client-1", "client_secret": "client-secret-value", "redirect_uris": ["https://x/cb"]},
        "client_metadata": {"redirect_uris": ["https://x/cb"], "scope": "read write"},
        "client_registration": registration,
        "client_secret_supplied": secret_supplied,
    }
    row = AdminMcpServer(
        server_id="umcp_oauth_source",
        display_name="Acme OAuth",
        description="",
        transport="streamable_http",
        url="https://mcp.example.test/mcp",
        headers=encrypt_mcp_headers({mcp_oauth_bundle_storage_key(): json.dumps(bundle)}),
        tools_json=_tools("search_documents"),
        is_enabled=True,
        owner_user_id="u1",
    )
    db.add(row)
    db.commit()
    return row


async def _submit(source):
    return await mcp_submissions.submit_to_marketplace(
        source.server_id, owner_user_id="u1", submitter_name="Alice",
        category="信息检索", version="1.0.0",
    )


@pytest.mark.asyncio
async def test_oauth_submission_lists_oauth_method_without_any_secret(db, monkeypatch):
    source = _oauth_source(db)
    _patch_probe(monkeypatch)
    result = await _submit(source)
    submission = db.query(McpMarketSubmission).filter_by(submission_id=result["submission_id"]).one()
    assert submission.auth_schema == []
    method = submission.auth_config["methods"][0]
    assert (submission.auth_config["default_method"], method["type"]) == ("oauth2", "oauth2")
    assert method["client_registration"] == "auto"
    assert method["scopes"] == ["read", "write"]
    assert submission.auth_config["credential_mode"] == "installer"
    stored = json.dumps(
        {"auth_config": submission.auth_config, "auth_schema": submission.auth_schema, "url": submission.url, "result": result},
        ensure_ascii=False,
    )
    for secret in ("access-secret", "refresh-secret", "client-secret-value", "client-1"):
        assert secret not in stored

    await mcp_submissions.review_submission(result["submission_id"], approve=True, reviewed_by="admin")
    listing = mcp_listing.get_market_item(db, submission.slug, owner_user_id="u2", viewer_user_id="u2")
    assert listing["auth_config"]["methods"][0]["type"] == "oauth2"
    assert listing["requires_user_credentials"] is True


@pytest.mark.asyncio
async def test_submission_from_own_oauth_app_requires_installers_bring_one(db, monkeypatch):
    source = _oauth_source(db, registration="manual", secret_supplied=True)
    _patch_probe(monkeypatch)
    result = await _submit(source)
    method = db.query(McpMarketSubmission).filter_by(submission_id=result["submission_id"]).one().auth_config["methods"][0]
    assert method["client_registration"] == "manual"
    assert method["client_id_required"] is True and method["client_secret_required"] is True


def test_admin_switches_community_token_listing_to_oauth_and_back(db):
    _approved_market(db)
    result = mcp_administration.update_market_item(
        db, "acme-search", requires_auth=True, auth_type="oauth2",
        credential_mode="admin", oauth_registration="admin_app",
        oauth_client_id="shared-app", oauth_client_secret="shared-secret",
    )
    methods = result["auth_config"]["methods"]
    assert [m["type"] for m in methods] == ["oauth2"]
    assert methods[0]["client_registration"] == "admin_app"
    assert result["auth_schema"] == []
    assert result["auth_config"]["credential_mode"] == "installer"
    assert "shared-secret" not in json.dumps(result, ensure_ascii=False)

    back = mcp_administration.update_market_item(db, "acme-search", requires_auth=True, auth_type="token")
    assert [m["type"] for m in back["auth_config"]["methods"]] == ["token"]
    assert back["auth_schema"][0]["key"] == "Authorization"


def test_curated_template_auth_type_is_fixed(db):
    from core.infra.exceptions import BadRequestError

    mcp_curated.ensure_curated_market_items(db)
    with pytest.raises(BadRequestError):
        mcp_administration.update_market_item(db, "amap-maps", requires_auth=True, auth_type="oauth2")


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [False, True])
async def test_submitting_intranet_mcp_follows_self_service_private_switch(db, monkeypatch, allowed):
    from core.infra.exceptions import BadRequestError

    source = _oauth_source(db)
    source.url = "http://192.168.10.20/mcp"
    db.commit()
    monkeypatch.setattr(
        "features.marketplace.mcp_validation.self_service_private_network_allowed", lambda: allowed
    )
    monkeypatch.setattr(mcp_submissions, "probe_mcp_connectivity", AsyncMock(return_value=(True, "")))
    if allowed:
        assert (await _submit(source))["status"] == "pending"
    else:
        with pytest.raises(BadRequestError):
            await _submit(source)
