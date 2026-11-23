from __future__ import annotations
from core.capabilities.mcp import oauth_client
from tests.features.marketplace.mcp_marketplace_support import (
    AdminMcpServer,
    BadRequestError,
    Base,
    McpMarketInstallation,
    McpMarketItem,
    McpMarketSubmission,
    McpMarketVersion,
    McpServerConfigService,
    OAuthBundleStorage,
    OAuthToken,
    Path,
    ResourceNotFoundError,
    SimpleNamespace,
    _IS_CE_TREE,
    _admin_private_market,
    _approved_market,
    _patch_install_side_effects,
    _patch_probe,
    _private_server,
    _tools,
    admin_mcp_routes,
    asyncio,
    create_engine,
    db,
    decrypt_mcp_headers,
    encrypt_legacy_mcp_headers,
    encrypt_mcp_headers,
    httpx,
    importlib,
    json,
    listing,
    make_client,
    management,
    mask_mcp_headers,
    materialize_mcp_http_connection,
    mcp_administration,
    mcp_listing,
    mcp_curated,
    mcp_installation,
    mcp_oauth_bundle_storage_key,
    mcp_publishing,
    mcp_query_secret_storage_key,
    mcp_revalidation,
    mcp_submissions,
    mcp_versions,
    me_capability_routes,
    oauth_service,
    pytest,
    sessionmaker,
    socket,
    tool_snapshot_hash,
    validate_remote_mcp_url,
)

@pytest.mark.asyncio
async def test_http_probe_releases_its_discovery_resource(db, monkeypatch):
    from mcp.types import Tool
    from core.harness.registry import mcp_clients
    calls = []
    class Resource:
        descriptors = [Tool(name="search", description="Search", input_schema={"type":"object"})]
        async def close(self):
            calls.append("closed")
    async def discover(name, cfg):
        calls.append(name)
        return Resource()
    monkeypatch.setattr(mcp_clients, "discover_tools", discover)
    row = AdminMcpServer(server_id="probe", display_name="Probe", transport="streamable_http", url="https://mcp.example.test/mcp", headers={}, is_enabled=True)
    ok, error = await management.probe_mcp_connectivity(row)
    assert ok and not error
    assert calls == ["probe", "closed"]
    assert row.tools_json[0]["name"] == "search"


@pytest.mark.asyncio
async def test_http_probe_surfaces_401_instead_of_cancel_scope_noise(db, monkeypatch):
    import httpx2
    request = httpx2.Request("POST", "https://mcp.example.test/mcp")
    response = httpx2.Response(401, request=request)
    unauthorized = httpx2.HTTPStatusError("401 Unauthorized", request=request, response=response)
    async def discover(name, cfg):
        raise BaseExceptionGroup("MCP request failed", [unauthorized, asyncio.CancelledError("Cancelled via cancel scope")])
    from core.harness.registry import mcp_clients
    monkeypatch.setattr(mcp_clients, "discover_tools", discover)
    row = AdminMcpServer(
        server_id="unauthorized_probe",
        display_name="Unauthorized Probe",
        transport="streamable_http",
        url="https://mcp.example.test/mcp",
        headers={},
        is_enabled=True,
    )

    ok, error = await management.probe_mcp_connectivity(row)

    assert ok is False
    assert error == "远端 MCP 返回 401 Unauthorized，请检查鉴权设置和 Token"
    assert "cancel scope" not in error.lower()


def test_ce_metadata_contains_mcp_market_auth_without_ee_dependency():
    project_root = Path(__file__).resolve().parents[5]
    module_path = project_root / "ce/overlay/src/backend/core/db/personal_schema.py"
    if not module_path.exists():
        module_path = project_root / "src/backend/core/db/personal_schema.py"
    spec = importlib.util.spec_from_file_location("ce_edition_tables_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    metadata = module.ce_metadata()
    assert "mcp_market_items" in metadata.tables
    assert "auth_config" in metadata.tables["mcp_market_versions"].columns
    assert "auth_config" in metadata.tables["mcp_market_submissions"].columns


def test_header_credentials_are_encrypted_and_masked():
    encrypted = encrypt_mcp_headers({"Authorization": "Bearer top-secret"})
    assert encrypted["Authorization"].startswith("enc:v1:")
    assert "top-secret" not in encrypted["Authorization"]
    assert decrypt_mcp_headers(encrypted) == {"Authorization": "Bearer top-secret"}
    assert mask_mcp_headers(encrypted) == {"Authorization": "***"}


def test_runtime_query_credentials_are_encrypted_and_never_sent_as_headers():
    storage_key = mcp_query_secret_storage_key("key")
    encrypted = encrypt_mcp_headers({storage_key: "amap-secret"})
    assert "amap-secret" not in str(encrypted)
    assert mask_mcp_headers(encrypted) == {}

    runtime_url, runtime_headers = materialize_mcp_http_connection(
        "https://mcp.amap.com/mcp?locale=zh-CN",
        decrypt_mcp_headers(encrypted),
    )
    assert runtime_url == "https://mcp.amap.com/mcp?locale=zh-CN&key=amap-secret"
    assert runtime_headers == {}

    row = AdminMcpServer(
        server_id="amap_private",
        display_name="Amap",
        transport="streamable_http",
        url="https://mcp.amap.com/mcp?locale=zh-CN",
        headers=encrypted,
        is_enabled=True,
    )
    runtime_config = McpServerConfigService.get_instance()._row_to_config(row)
    assert runtime_config["url"] == runtime_url
    assert "headers" not in runtime_config


@pytest.mark.asyncio
async def test_oauth_bundle_is_provider_neutral_and_never_sent_as_header():
    storage = OAuthBundleStorage(
        {
            "client_metadata": {
                "redirect_uris": ["https://app.example.test/api/v1/mcp-market/oauth/callback"],
                "client_name": "HugAgentOS MCP Client",
            },
            "client_info": {
                "redirect_uris": ["https://app.example.test/api/v1/mcp-market/oauth/callback"],
                "client_id": "client-id",
                "token_endpoint_auth_method": "none",
            },
        }
    )
    await storage.set_tokens(
        OAuthToken(
            access_token="oauth-access-secret",
            refresh_token="oauth-refresh-secret",
            expires_in=3600,
        )
    )
    bundle = storage.bundle
    encrypted = encrypt_mcp_headers(
        {mcp_oauth_bundle_storage_key(): json.dumps(bundle, separators=(",", ":"))}
    )
    assert "oauth-access-secret" not in str(encrypted)
    assert mask_mcp_headers(encrypted) == {}

    row = AdminMcpServer(
        server_id="oauth_private",
        display_name="OAuth MCP",
        transport="streamable_http",
        url="https://mcp.example.test/mcp",
        headers=encrypted,
        is_enabled=True,
    )
    runtime_config = McpServerConfigService.get_instance()._row_to_config(row)
    assert "oauth_provider" in runtime_config
    assert "headers" not in runtime_config
    client = make_client(row.server_id, runtime_config)
    assert client.mode == "auto"
    assert client.server.args[1]["oauth_provider"] is runtime_config["oauth_provider"]


@pytest.mark.asyncio
async def test_oauth_callback_and_status_are_shared_without_plaintext_code(monkeypatch):
    import fakeredis.aioredis

    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("core.infra.ephemeral.get_redis", lambda **_: fake_redis)
    monkeypatch.setattr("core.infra.ephemeral.redis_configured", lambda: True)
    flow = oauth_service.OAuthInstallFlow(
        flow_id="mcpoauth_test",
        slug="gitlab-official",
        owner_user_id="u1",
        installed_by="u1",
        auth_method="oauth2",
        credentials={},
        callback_url="https://app.example.test/api/v1/mcp-market/oauth/callback",
        status="waiting_for_user",
    )
    await oauth_service.store_flow_status(flow)
    oauth_service.process_state().flows.pop(flow.flow_id, None)  # simulate callback landing on another worker
    await oauth_service.complete_callback(
        flow.flow_id,
        code="short-lived-authorization-code",
        state="oauth-state",
    )
    callback_raw = await fake_redis.get(oauth_service.callback_key(flow.flow_id))
    assert "short-lived-authorization-code" not in callback_raw
    status = await oauth_service.get_flow_status(flow.flow_id, owner_user_id="u1")
    assert status["status"] == "processing_callback"
    await fake_redis.aclose()


@pytest.mark.asyncio
async def test_oauth_start_hands_back_before_discovery_finishes(db, monkeypatch):
    """Slow authorization-URL discovery must keep the flow pollable, never fail the install."""
    import fakeredis.aioredis

    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("core.infra.ephemeral.get_redis", lambda **_: fake_redis)
    monkeypatch.setattr("core.infra.ephemeral.redis_configured", lambda: True)
    monkeypatch.setattr("core.capabilities.mcp.oauth.DISCOVERY_GRACE_SECONDS", 0.05)
    mcp_curated.ensure_curated_market_items(db)

    async def slow_discovery(flow):
        await asyncio.sleep(0.3)
        flow.authorization_url = "https://provider.example.test/authorize?code_challenge=x"
        flow.status = "waiting_for_user"
        await oauth_service.store_flow_status(flow)
        flow.ready.set()

    monkeypatch.setattr(oauth_service, "run_flow", slow_discovery)
    try:
        started = await oauth_service.start_oauth_install(
            slug="gitlab-official",
            owner_user_id="u1",
            installed_by="u1",
            auth_method="oauth2",
            credentials={},
            api_base="https://app.example.test/api",
        )
        assert started["status"] == "starting"
        assert started["authorization_url"] == ""

        await asyncio.sleep(0.4)
        status = await oauth_service.get_flow_status(started["flow_id"], owner_user_id="u1")
        assert status["status"] == "waiting_for_user"
        assert status["authorization_url"].startswith("https://provider.example.test/authorize")
    finally:
        oauth_service.process_state().flows.clear()
        await fake_redis.aclose()


@pytest.mark.asyncio
async def test_oauth_cancel_marks_flow_failed_and_wakes_worker(monkeypatch):
    import fakeredis.aioredis

    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("core.infra.ephemeral.get_redis", lambda **_: fake_redis)
    monkeypatch.setattr("core.infra.ephemeral.redis_configured", lambda: True)
    flow = oauth_service.OAuthInstallFlow(
        flow_id="mcpoauth_cancel",
        slug="gitlab-official",
        owner_user_id="u1",
        installed_by="u1",
        auth_method="oauth2",
        credentials={},
        callback_url="https://app.example.test/api/v1/mcp-market/oauth/callback",
        status="waiting_for_user",
    )
    oauth_service.process_state().flows[flow.flow_id] = flow
    try:
        await oauth_service.store_flow_status(flow)
        status = await oauth_service.cancel_flow(flow.flow_id, owner_user_id="u1")
        assert status["status"] == "failed"
        assert status["error"] == "OAuth 登录已取消"
        assert flow.callback_received.is_set()
        with pytest.raises(ResourceNotFoundError):
            await oauth_service.cancel_flow(flow.flow_id, owner_user_id="u2")
    finally:
        oauth_service.process_state().flows.pop(flow.flow_id, None)
        await fake_redis.aclose()


def test_oauth_callback_uses_browser_origin_not_untrusted_host(monkeypatch):
    monkeypatch.setattr(
        oauth_client,
        "settings",
        SimpleNamespace(server=SimpleNamespace(mcp_oauth_public_base_url="", is_prod=False)),
    )
    request = SimpleNamespace(
        headers={"origin": "https://app.example.test", "host": "attacker.invalid"},
        url=SimpleNamespace(netloc="attacker.invalid"),
    )
    assert oauth_client.callback_url(oauth_client.public_api_base(request)) == (
        "https://app.example.test/api/v1/mcp-market/oauth/callback"
    )


def test_oauth_callback_allows_configured_http_in_production(monkeypatch):
    monkeypatch.setattr(
        oauth_client,
        "settings",
        SimpleNamespace(
            server=SimpleNamespace(
                mcp_oauth_public_base_url="http://app.example.test/api",
                is_prod=True,
            )
        ),
    )
    request = SimpleNamespace(headers={}, url=SimpleNamespace(netloc="ignored.example.test"))

    assert oauth_client.callback_url(oauth_client.public_api_base(request)) == (
        "http://app.example.test/api/v1/mcp-market/oauth/callback"
    )


def test_legacy_plaintext_headers_are_encrypted_once(db):
    row = _private_server(db)
    row.headers = {"Authorization": "Bearer legacy"}
    db.commit()
    assert encrypt_legacy_mcp_headers(db) == 1
    db.refresh(row)
    assert decrypt_mcp_headers(row.headers) == {"Authorization": "Bearer legacy"}
    assert "legacy" not in str(row.headers)
    assert encrypt_legacy_mcp_headers(db) == 0


@pytest.mark.asyncio
async def test_remote_url_security_blocks_private_and_accepts_public_dns(monkeypatch):
    with pytest.raises(BadRequestError):
        await validate_remote_mcp_url("https://127.0.0.1/mcp")
    with pytest.raises(BadRequestError):
        await validate_remote_mcp_url("http://example.com/mcp")

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )
    await validate_remote_mcp_url("https://example.com/mcp")


@pytest.mark.asyncio
async def test_remote_url_security_allows_public_http_when_requested(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))],
    )

    await validate_remote_mcp_url("http://example.com/mcp", require_https=False)

    with pytest.raises(BadRequestError, match="本机、内网或保留地址"):
        await validate_remote_mcp_url("http://127.0.0.1/mcp", require_https=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["127.0.0.1", "10.10.0.8", "172.18.0.12", "192.168.1.9"])
async def test_remote_url_security_allows_trusted_private_networks_when_enabled(address):
    await validate_remote_mcp_url(
        f"http://{address}/mcp",
        allow_private_network=True,
        require_https=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["0.0.0.0", "169.254.169.254", "224.0.0.1"])
async def test_remote_url_security_keeps_high_risk_ranges_blocked_when_private_enabled(address):
    with pytest.raises(BadRequestError, match="本机、内网或保留地址"):
        await validate_remote_mcp_url(
            f"http://{address}/mcp",
            allow_private_network=True,
            require_https=False,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_private_network", [False, True])
async def test_self_service_mcp_uses_private_network_deployment_setting(
    db, monkeypatch, allow_private_network
):
    validation = {}

    async def _validate(url, **kwargs):
        validation.update(url=url, **kwargs)

    async def _probe(row, **_options):
        row.tools_json = _tools("search")
        return True, ""

    monkeypatch.setattr(me_capability_routes, "_require_flag", lambda *args: None)
    monkeypatch.setattr(me_capability_routes, "validate_remote_mcp_url", _validate)
    monkeypatch.setattr(me_capability_routes, "probe_mcp_connectivity", _probe)
    monkeypatch.setattr(
        me_capability_routes,
        "settings",
        SimpleNamespace(
            server=SimpleNamespace(
                mcp_self_service_allow_private_network=allow_private_network,
            )
        ),
    )

    await me_capability_routes.create_my_mcp_server(
        me_capability_routes.CreateUserMcpRequest(
            display_name="Private MCP",
            url="http://10.10.0.8/mcp",
        ),
        user=SimpleNamespace(user_id="u1"),
    )

    assert validation == {
        "url": "http://10.10.0.8/mcp",
        "allow_private_network": allow_private_network,
        "require_https": False,
    }
