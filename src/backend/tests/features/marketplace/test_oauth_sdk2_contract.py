"""OAuth SDK 2 callback integrity and interactive discovery budgets."""
import asyncio
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock

import pytest
from mcp.shared.auth import OAuthMetadata

from core.capabilities.mcp import oauth, management
from features.marketplace.api.mcp_marketplace import oauth_callback
from tests.db_support import share_session


class MemoryState:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def put(self, key, value, **kwargs):
        self.values[key] = value


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_worker", [False, True])
@pytest.mark.parametrize("issuer,accepted", [
    ("https://issuer.example.test/", True),
    ("https://attacker.example.test/", False),
    (None, False),
])
async def test_callback_issuer_reaches_sdk_validation(monkeypatch, cross_worker, issuer, accepted):
    """Exercise the actual route, flow callbacks and SDK authorization grant."""
    state = MemoryState()
    monkeypatch.setattr(oauth, "get_ephemeral_state", lambda: state)
    monkeypatch.setattr(oauth, "encrypt_secret", lambda value: "encrypted:" + value)
    monkeypatch.setattr(oauth, "decrypt_secret", lambda value: value.removeprefix("encrypted:"))
    share_session(monkeypatch, SimpleNamespace(rollback=lambda: None, close=lambda: None))
    monkeypatch.setattr(oauth, "validate_remote_mcp_url", AsyncMock())
    monkeypatch.setattr(oauth, "market_dependency_materialize_mcp_http_connection", lambda url, headers: (url, headers))
    version = SimpleNamespace(auth_config={}, auth_schema=[], url="https://mcp.example.test/mcp", transport="streamable_http")
    monkeypatch.setattr(oauth, "get_version", lambda db, slug: (SimpleNamespace(display_name="OAuth test", description=""), version))
    installed = AsyncMock(return_value={"installed": True})
    catalog = SimpleNamespace(
        normalize_auth_config=lambda *args: {"methods": [{"id": "oauth", "scopes": []}]},
        installation_secrets=lambda *args, **kwargs: {},
        install_market_item=installed,
        mcp_oauth_install_policy=lambda *args: {"admin_client": None, "allow_private_network": False},
    )
    monkeypatch.setattr(oauth, "marketplace_catalog", lambda: catalog)
    flow = oauth.OAuthInstallFlow(
        flow_id="issuer-test", slug="test", owner_user_id="owner", installed_by="owner",
        auth_method="oauth", credentials={}, callback_url="https://app.example.test/callback", client_id="client",
    )
    monkeypatch.setattr(oauth, "process_state", lambda: SimpleNamespace(flows=flows))
    flows = {flow.flow_id: flow}

    async def probe(row, *, validate, oauth_provider, timeout_seconds):
        assert oauth_provider.context.client_metadata.application_type == "web"
        assert timeout_seconds == 300.0
        oauth_provider.context.client_info = await oauth_provider.context.storage.get_client_info()
        oauth_provider.context.oauth_metadata = OAuthMetadata(
            issuer="https://issuer.example.test/",
            authorization_endpoint="https://issuer.example.test/authorize",
            token_endpoint="https://issuer.example.test/token",
            authorization_response_iss_parameter_supported=True,
        )

        async def deliver_callback():
            await flow.ready.wait()
            authorization_state = parse_qs(urlparse(flow.authorization_url).query)["state"][0]
            if cross_worker:
                flows.clear()
            await oauth_callback(flow_id=flow.flow_id, code="authorization-code", state=authorization_state,
                                 iss=issuer, error="", error_description="")

        callback = asyncio.create_task(deliver_callback())
        try:
            code, verifier = await oauth_provider._perform_authorization_code_grant()
            assert code == "authorization-code"
            assert verifier
            return True, ""
        finally:
            await callback

    monkeypatch.setattr(oauth, "probe_mcp_connectivity", probe)
    await oauth.run_flow(flow)
    assert flow.status == ("completed" if accepted else "failed")
    assert installed.await_count == int(accepted)
    persisted = json.loads(await state.get(oauth.callback_key(flow.flow_id)))
    assert persisted["iss"] == issuer
    assert persisted["code"] == "encrypted:authorization-code"
    if not accepted:
        assert "iss" in flow.error


@pytest.mark.asyncio
async def test_connectivity_probe_passes_interactive_budget_and_releases_resource(monkeypatch):
    from core.capabilities.mcp.service import McpServerConfigService
    from core.harness.registry import mcp_clients
    cfg = {"transport": "streamable_http", "url": "https://mcp.example.test/mcp", "transport_timeout": 17}
    monkeypatch.setattr(McpServerConfigService, "get_instance", lambda: SimpleNamespace(_row_to_config=lambda row: dict(cfg)))
    resource = SimpleNamespace(descriptors=[], close=AsyncMock())
    discover = AsyncMock(return_value=resource)
    monkeypatch.setattr(mcp_clients, "discover_tools", discover)
    provider = object()
    row = SimpleNamespace(server_id="oauth", transport="streamable_http")
    assert await management.probe_mcp_connectivity(row, oauth_provider=provider, timeout_seconds=300) == (True, "")
    passed = discover.await_args.args[1]
    assert passed["discovery_timeout"] == 300
    assert passed["transport_timeout"] == 17
    assert passed["oauth_provider"] is provider
    resource.close.assert_awaited_once()
