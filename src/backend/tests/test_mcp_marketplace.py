"""End-to-end service tests for the reviewed MCP marketplace lifecycle."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import socket
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from api.routes.v1 import admin_mcp_servers as admin_mcp_routes
from api.routes.v1 import me_capabilities as me_capability_routes
from core.db.engine import Base
from core.db.models import (
    AdminMcpServer,
    McpMarketInstallation,
    McpMarketItem,
    McpMarketSubmission,
    McpMarketVersion,
)
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.llm.mcp_pool import make_client
from core.services import mcp_management_service as management
from core.services import mcp_marketplace_service as market
from core.services import mcp_oauth_service as oauth_service
from core.services.mcp_management_service import (
    assess_mcp_risk,
    decrypt_mcp_headers,
    encrypt_legacy_mcp_headers,
    encrypt_mcp_headers,
    mask_mcp_headers,
    materialize_mcp_http_connection,
    mcp_oauth_bundle_storage_key,
    mcp_query_secret_storage_key,
    tool_snapshot_hash,
    validate_remote_mcp_url,
)
from core.services.mcp_oauth_service import OAuthBundleStorage
from core.services.mcp_service import McpServerConfigService
from mcp.shared.auth import OAuthToken
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

_IS_CE_TREE = not (Path(__file__).resolve().parents[1] / "edition_ee").exists()


@pytest.fixture
def db():
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


def _tools(*names: str):
    return [
        {
            "name": name,
            "description": f"Tool {name}",
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
        }
        for name in names
    ]


@pytest.mark.asyncio
async def test_http_probe_uses_stateless_client_without_explicit_close(db, monkeypatch):
    calls = {"connected": 0, "closed": 0, "is_stateful": None}

    class FakeClient:
        async def connect(self):
            calls["connected"] += 1

        async def list_tools(self):
            return [SimpleNamespace(name="search", description="Search", inputSchema={})]

        async def close(self):
            calls["closed"] += 1

    def _make_client(name, cfg, *, is_stateful=True, **kwargs):
        calls["is_stateful"] = is_stateful
        return FakeClient()

    from core.llm import mcp_pool

    monkeypatch.setattr(mcp_pool, "make_client", _make_client)
    row = AdminMcpServer(
        server_id="stateless_probe",
        display_name="Stateless Probe",
        transport="streamable_http",
        url="https://mcp.example.test/mcp",
        headers={},
        is_enabled=True,
    )

    ok, error = await management.probe_mcp_connectivity(row)

    assert ok is True
    assert error == ""
    assert calls == {"connected": 0, "closed": 0, "is_stateful": False}
    assert row.tools_json[0]["name"] == "search"


@pytest.mark.asyncio
async def test_http_probe_surfaces_401_instead_of_cancel_scope_noise(db, monkeypatch):
    request = httpx.Request("POST", "https://mcp.example.test/mcp")
    response = httpx.Response(401, request=request)
    unauthorized = httpx.HTTPStatusError(
        "401 Unauthorized",
        request=request,
        response=response,
    )

    class FakeClient:
        async def list_tools(self):
            raise BaseExceptionGroup(
                "MCP request failed",
                [
                    unauthorized,
                    asyncio.CancelledError("Cancelled via cancel scope deadbeef"),
                ],
            )

    def _make_client(name, cfg, *, is_stateful=True, **kwargs):
        assert is_stateful is False
        return FakeClient()

    from core.llm import mcp_pool

    monkeypatch.setattr(mcp_pool, "make_client", _make_client)
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


def _private_server(db, *, owner: str = "u1", server_id: str = "umcp_source"):
    row = AdminMcpServer(
        server_id=server_id,
        display_name="Acme Search",
        description="Search Acme documents",
        transport="streamable_http",
        url="https://mcp.example.test/mcp",
        headers=encrypt_mcp_headers({"Authorization": "Bearer publisher-secret"}),
        tools_json=_tools("search_documents"),
        is_stable=False,
        is_enabled=True,
        owner_user_id=owner,
    )
    db.add(row)
    db.commit()
    return row


def _approved_market(db, *, tools=None, risk_tool: str | None = None):
    snapshot = tools or _tools(risk_tool or "search_documents")
    risk, report = assess_mcp_risk(snapshot)
    item = McpMarketItem(
        slug="acme-search",
        display_name="Acme Search",
        description="Search Acme documents",
        category="信息检索",
        tags=["search"],
        publisher_name="Acme",
        source="community",
        latest_version_id="mcpver_1",
        status="active",
    )
    version = McpMarketVersion(
        version_id="mcpver_1",
        slug="acme-search",
        version="1.0.0",
        transport="streamable_http",
        url="https://mcp.example.test/mcp",
        auth_schema=[
            {
                "key": "Authorization",
                "label": "Authorization",
                "target": "header",
                "required": True,
                "secret": True,
            }
        ],
        tools_json=snapshot,
        tool_hash=tool_snapshot_hash(snapshot),
        risk_level=risk,
        risk_report=report,
        source_server_id="umcp_source",
    )
    db.add_all([item, version])
    db.commit()
    return item, version


def _patch_probe(monkeypatch, tools=None):
    async def _validate(*args, **kwargs):
        return None

    async def _probe(row, db=None):
        if tools is not None:
            row.tools_json = tools
        return True, ""

    monkeypatch.setattr(market, "validate_remote_mcp_url", _validate)
    monkeypatch.setattr(market, "probe_mcp_connectivity", _probe)
    monkeypatch.setattr(market, "refresh_mcp_caches", lambda: None)


def test_ce_metadata_contains_mcp_market_auth_without_ee_dependency():
    project_root = Path(__file__).resolve().parents[3]
    module_path = project_root / "ce/overlay/src/backend/core/db/edition_tables.py"
    if not module_path.exists():
        module_path = project_root / "src/backend/core/db/edition_tables.py"
    spec = importlib.util.spec_from_file_location("ce_edition_tables_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    metadata = module._ce_metadata()
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
        is_stable=False,
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
        is_stable=False,
        is_enabled=True,
    )
    runtime_config = McpServerConfigService.get_instance()._row_to_config(row)
    assert "oauth_provider" in runtime_config
    assert "headers" not in runtime_config
    client = make_client(row.server_id, runtime_config, is_stateful=False)
    assert client.oauth_provider is runtime_config["oauth_provider"]


@pytest.mark.asyncio
async def test_oauth_callback_and_status_are_shared_without_plaintext_code(monkeypatch):
    import fakeredis.aioredis

    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(oauth_service, "get_redis", lambda: fake_redis)
    flow = oauth_service.OAuthInstallFlow(
        flow_id="mcpoauth_test",
        slug="gitlab-official",
        owner_user_id="u1",
        installed_by="u1",
        auth_method="oauth2",
        credentials={},
        confirm_high_risk=True,
        callback_url="https://app.example.test/api/v1/mcp-market/oauth/callback",
        status="waiting_for_user",
    )
    await oauth_service._store_flow_status(flow)
    oauth_service._flows.pop(flow.flow_id, None)  # simulate callback landing on another worker
    await oauth_service.complete_callback(
        flow.flow_id,
        code="short-lived-authorization-code",
        state="oauth-state",
    )
    callback_raw = await fake_redis.get(oauth_service._callback_key(flow.flow_id))
    assert "short-lived-authorization-code" not in callback_raw
    status = await oauth_service.get_flow_status(flow.flow_id, owner_user_id="u1")
    assert status["status"] == "processing_callback"
    await fake_redis.aclose()


@pytest.mark.asyncio
async def test_oauth_cancel_marks_flow_failed_and_wakes_worker(monkeypatch):
    import fakeredis.aioredis

    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(oauth_service, "get_redis", lambda: fake_redis)
    flow = oauth_service.OAuthInstallFlow(
        flow_id="mcpoauth_cancel",
        slug="gitlab-official",
        owner_user_id="u1",
        installed_by="u1",
        auth_method="oauth2",
        credentials={},
        confirm_high_risk=True,
        callback_url="https://app.example.test/api/v1/mcp-market/oauth/callback",
        status="waiting_for_user",
    )
    oauth_service._flows[flow.flow_id] = flow
    try:
        await oauth_service._store_flow_status(flow)
        status = await oauth_service.cancel_flow(flow.flow_id, owner_user_id="u1")
        assert status["status"] == "failed"
        assert status["error"] == "OAuth 登录已取消"
        assert flow.callback_received.is_set()
        with pytest.raises(ResourceNotFoundError):
            await oauth_service.cancel_flow(flow.flow_id, owner_user_id="u2")
    finally:
        oauth_service._flows.pop(flow.flow_id, None)
        await fake_redis.aclose()


def test_oauth_callback_uses_browser_origin_not_untrusted_host(monkeypatch):
    monkeypatch.setattr(
        oauth_service,
        "settings",
        SimpleNamespace(server=SimpleNamespace(mcp_oauth_public_base_url="", is_prod=False)),
    )
    request = SimpleNamespace(
        headers={"origin": "https://app.example.test", "host": "attacker.invalid"},
        url=SimpleNamespace(netloc="attacker.invalid"),
    )
    assert oauth_service.public_oauth_callback_url(request) == (
        "https://app.example.test/api/v1/mcp-market/oauth/callback"
    )


def test_oauth_callback_allows_configured_http_in_production(monkeypatch):
    monkeypatch.setattr(
        oauth_service,
        "settings",
        SimpleNamespace(
            server=SimpleNamespace(
                mcp_oauth_public_base_url="http://app.example.test/api",
                is_prod=True,
            )
        ),
    )
    request = SimpleNamespace(headers={}, url=SimpleNamespace(netloc="ignored.example.test"))

    assert oauth_service.public_oauth_callback_url(request) == (
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

    async def _probe(row, _db):
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
        db=db,
    )

    assert validation == {
        "url": "http://10.10.0.8/mcp",
        "allow_private_network": allow_private_network,
        "require_https": False,
    }


@pytest.mark.asyncio
async def test_remote_url_security_rechecks_tun_fake_ip_dns(monkeypatch):
    with pytest.raises(BadRequestError):
        await validate_remote_mcp_url("https://198.18.0.1/mcp")

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.42", 443))],
    )

    async def _public_dns(_hostname: str):
        return {"8.8.8.8", "2001:4860:4860::8888"}

    monkeypatch.setattr(management, "_resolve_public_dns_via_doh", _public_dns)
    await validate_remote_mcp_url("https://example.com/mcp")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answers",
    [
        [
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                6,
                "",
                ("fc00::2", 443, 0, 0),
            )
        ],
        [
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                6,
                "",
                ("fdfe:dcba:9876::1b", 443, 0, 0),
            )
        ],
        [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.43", 443)),
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                6,
                "",
                ("fdfe:dcba:9876::1b", 443, 0, 0),
            ),
        ],
    ],
    ids=["sing-box-ipv6", "tun-ipv6", "dual-stack"],
)
async def test_remote_url_security_rechecks_ipv6_tun_fake_ip_dns(monkeypatch, answers):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: answers)

    async def _public_dns(_hostname: str):
        return {"140.82.112.4", "2606:50c0:8000::154"}

    monkeypatch.setattr(management, "_resolve_public_dns_via_doh", _public_dns)
    await validate_remote_mcp_url("https://github.com/mcp")


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["fd00::1", "fdfe:dcba:9877::1"])
async def test_remote_url_security_does_not_treat_arbitrary_ula_as_fake_ip(monkeypatch, address):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 443, 0, 0))
        ],
    )
    public_dns_called = False

    async def _public_dns(_hostname: str):
        nonlocal public_dns_called
        public_dns_called = True
        return {"8.8.8.8"}

    monkeypatch.setattr(management, "_resolve_public_dns_via_doh", _public_dns)
    with pytest.raises(BadRequestError, match="本机、内网或保留地址"):
        await validate_remote_mcp_url("https://internal.example.com/mcp")
    assert public_dns_called is False


@pytest.mark.asyncio
async def test_remote_url_security_rejects_fake_ip_when_public_dns_is_private(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                6,
                "",
                ("fdfe:dcba:9876::1b", 443, 0, 0),
            )
        ],
    )

    async def _public_dns(_hostname: str):
        return {"10.0.0.1"}

    monkeypatch.setattr(management, "_resolve_public_dns_via_doh", _public_dns)
    with pytest.raises(BadRequestError, match="公共 DNS 安全复核失败"):
        await validate_remote_mcp_url("https://example.com/mcp")


def test_tool_hash_is_order_independent_and_risk_is_explained():
    first = _tools("search", "delete_record")
    second = list(reversed(first))
    assert tool_snapshot_hash(first) == tool_snapshot_hash(second)
    level, report = assess_mcp_risk(first)
    assert level == "high"
    assert report["high_risk_tools"] == ["delete_record"]
    assert report["requires_confirmation"] is True


@pytest.mark.asyncio
async def test_submit_snapshots_without_credentials_then_approve(db, monkeypatch):
    source = _private_server(db)
    _patch_probe(monkeypatch)

    result = await market.submit_to_marketplace(
        db,
        source.server_id,
        owner_user_id="u1",
        submitter_name="Alice",
        category="信息检索",
        version="1.0.0",
        note="Tested",
    )
    submission = (
        db.query(McpMarketSubmission).filter_by(submission_id=result["submission_id"]).one()
    )
    assert submission.status == "pending"
    assert submission.auth_schema == [
        {
            "key": "Authorization",
            "label": "Authorization",
            "target": "header",
            "required": True,
            "secret": True,
        }
    ]
    assert "publisher-secret" not in str(submission.auth_schema)
    assert submission.tools_json == _tools("search_documents")

    reviewed = await market.review_submission(
        db,
        submission.submission_id,
        approve=True,
        reviewed_by="admin",
    )
    assert reviewed["status"] == "approved"
    item = db.query(McpMarketItem).filter_by(slug=submission.slug).one()
    version = db.query(McpMarketVersion).filter_by(version_id=item.latest_version_id).one()
    assert item.source == "community"
    assert version.tool_hash == tool_snapshot_hash(_tools("search_documents"))


@pytest.mark.asyncio
async def test_public_http_marketplace_submit_review_and_install(db, monkeypatch):
    source = _private_server(db)
    source.url = "http://8.8.8.8:8082/mcp"
    db.commit()

    async def _probe(row, db=None):
        return True, ""

    monkeypatch.setattr(market, "probe_mcp_connectivity", _probe)
    monkeypatch.setattr(market, "refresh_mcp_caches", lambda: None)

    submitted = await market.submit_to_marketplace(
        db,
        source.server_id,
        owner_user_id="u1",
        submitter_name="Alice",
        category="信息检索",
        version="1.0.0",
    )
    reviewed = await market.review_submission(
        db,
        submitted["submission_id"],
        approve=True,
        reviewed_by="admin",
    )
    installed = await market.install_market_item(
        db,
        reviewed["slug"],
        owner_user_id="u2",
        installed_by="u2",
        credentials={"Authorization": "Bearer user-two"},
    )

    row = db.query(AdminMcpServer).filter_by(server_id=installed["server_id"]).one()
    assert row.url == "http://8.8.8.8:8082/mcp"
    assert row.owner_user_id == "u2"


@pytest.mark.asyncio
async def test_market_installed_private_server_cannot_submit_again(db):
    item, version = _approved_market(db)
    source = _private_server(db)
    db.add(
        McpMarketInstallation(
            install_id="mcpins_no_resubmit",
            slug=item.slug,
            version_id=version.version_id,
            server_id=source.server_id,
            owner_user_id="u1",
            status="active",
        )
    )
    db.commit()

    from api.routes.v1.catalog import _load_owned_capability_items

    _skills, mcps = _load_owned_capability_items(db, "u1")
    catalog_item = next(row for row in mcps if row["id"] == source.server_id)
    assert catalog_item["marketplace_installed"] is True

    with pytest.raises(BadRequestError, match="不能再次申请上架"):
        await market.submit_to_marketplace(
            db,
            source.server_id,
            owner_user_id="u1",
            submitter_name="Alice",
            category="信息检索",
            version="1.0.0",
        )


@pytest.mark.asyncio
async def test_submission_strips_query_secret_into_install_schema(db, monkeypatch):
    source = _private_server(db)
    source.url = "https://mcp.example.test/mcp?key=publisher-secret&locale=zh-CN"
    db.commit()
    _patch_probe(monkeypatch)

    submitted = await market.submit_to_marketplace(
        db,
        source.server_id,
        owner_user_id="u1",
        submitter_name="Alice",
        category="信息检索",
        version="1.0.0",
    )
    row = db.query(McpMarketSubmission).filter_by(submission_id=submitted["submission_id"]).one()
    assert row.url == "https://mcp.example.test/mcp?locale=zh-CN"
    assert row.auth_schema[-1] == {
        "key": "QUERY_KEY",
        "label": "key",
        "target": "query",
        "name": "key",
        "required": True,
        "secret": True,
    }
    assert "publisher-secret" not in str(row.url)
    assert "publisher-secret" not in str(row.auth_schema)


@pytest.mark.asyncio
async def test_admin_publish_links_existing_global_instance_without_duplication(db, monkeypatch):
    source = _private_server(db, owner="temporary", server_id="global_remote")
    source.owner_user_id = None
    db.commit()
    _patch_probe(monkeypatch)

    published = await market.publish_admin_server(
        db,
        source.server_id,
        category="信息检索",
        version="1.0.0",
    )
    assert published["installed"] is True
    installation = db.query(McpMarketInstallation).one()
    assert installation.server_id == source.server_id
    assert installation.owner_user_id is None
    assert db.query(AdminMcpServer).count() == 1
    listed = market.list_market_items(
        db,
        owner_user_id=None,
        viewer_user_id=None,
        include_disabled=True,
    )
    assert listed["items"][0]["installed"] is True


@pytest.mark.asyncio
async def test_marketplace_first_admin_source_is_reused_on_global_install(db, monkeypatch):
    source = _private_server(db, owner="temporary", server_id="market_source")
    source.owner_user_id = None
    db.commit()
    _patch_probe(monkeypatch)

    published = await market.publish_admin_server(
        db,
        source.server_id,
        category="信息检索",
        version="1.0.0",
        link_as_installation=False,
    )
    db.refresh(source)
    assert published["installed"] is False
    assert source.is_enabled is False
    assert source.extra_config["market_source_only"] is True
    assert db.query(McpMarketInstallation).count() == 0
    assert published["requires_auth"] is True
    assert published["credentials_managed_by_admin"] is True
    assert published["requires_user_credentials"] is False
    assert "publisher-secret" not in json.dumps(published)

    installed = await market.install_market_item(
        db,
        published["slug"],
        owner_user_id=None,
        installed_by="admin",
        credentials={},
    )
    db.refresh(source)
    assert installed["server_id"] == source.server_id
    assert source.is_enabled is True
    assert "market_source_only" not in source.extra_config
    assert decrypt_mcp_headers(source.headers)["Authorization"] == "Bearer publisher-secret"
    assert db.query(AdminMcpServer).count() == 1
    assert db.query(McpMarketInstallation).one().server_id == source.server_id


@pytest.mark.asyncio
async def test_user_install_reuses_admin_managed_credentials_without_exposing_them(db, monkeypatch):
    source = _private_server(db, owner="temporary", server_id="shared_market_source")
    source.owner_user_id = None
    db.commit()
    _patch_probe(monkeypatch)

    published = await market.publish_admin_server(
        db,
        source.server_id,
        category="信息检索",
        version="1.0.0",
        link_as_installation=False,
    )
    listed = market.get_market_item(
        db,
        published["slug"],
        owner_user_id="u2",
        viewer_user_id="u2",
    )
    assert listed["credentials_managed_by_admin"] is True
    assert listed["requires_user_credentials"] is False
    assert "publisher-secret" not in json.dumps(listed)

    installed = await market.install_market_item(
        db,
        published["slug"],
        owner_user_id="u2",
        installed_by="u2",
        credentials={},
    )
    private_server = db.query(AdminMcpServer).filter_by(server_id=installed["server_id"]).one()
    assert private_server.owner_user_id == "u2"
    assert decrypt_mcp_headers(private_server.headers)["Authorization"] == (
        "Bearer publisher-secret"
    )
    assert "publisher-secret" not in str(private_server.headers)
    db.refresh(source)
    assert source.is_enabled is False
    assert source.extra_config["market_source_only"] is True


@pytest.mark.asyncio
async def test_user_install_reuses_admin_managed_query_token(db, monkeypatch):
    source = _private_server(db, owner="temporary", server_id="query_market_source")
    source.owner_user_id = None
    source.url = "https://mcp.example.test/mcp?key=publisher-query-secret&locale=zh-CN"
    db.commit()
    _patch_probe(monkeypatch)

    published = await market.publish_admin_server(
        db,
        source.server_id,
        category="信息检索",
        version="1.0.0",
        link_as_installation=False,
    )
    assert published["credentials_managed_by_admin"] is True
    assert published["url_origin"] == "https://mcp.example.test"
    assert "publisher-query-secret" not in json.dumps(published)

    installed = await market.install_market_item(
        db,
        published["slug"],
        owner_user_id="u2",
        installed_by="u2",
        credentials={},
    )
    private_server = db.query(AdminMcpServer).filter_by(server_id=installed["server_id"]).one()
    assert private_server.url == "https://mcp.example.test/mcp?locale=zh-CN"
    assert "publisher-query-secret" not in str(private_server.headers)
    runtime_url, runtime_headers = materialize_mcp_http_connection(
        private_server.url,
        decrypt_mcp_headers(private_server.headers),
    )
    assert runtime_url == ("https://mcp.example.test/mcp?locale=zh-CN&key=publisher-query-secret")
    assert runtime_headers == {"Authorization": "Bearer publisher-secret"}


@pytest.mark.asyncio
async def test_removed_admin_credential_falls_back_to_installer_input(db, monkeypatch):
    source = _private_server(db, owner="temporary", server_id="unmanaged_market_source")
    source.owner_user_id = None
    db.commit()
    _patch_probe(monkeypatch)

    published = await market.publish_admin_server(
        db,
        source.server_id,
        category="信息检索",
        version="1.0.0",
        link_as_installation=False,
    )
    source.headers = {}
    db.commit()
    listed = market.get_market_item(
        db,
        published["slug"],
        owner_user_id="u2",
        viewer_user_id="u2",
    )
    assert listed["credentials_managed_by_admin"] is False
    assert listed["requires_user_credentials"] is True

    with pytest.raises(BadRequestError, match="请填写安装凭据"):
        await market.install_market_item(
            db,
            published["slug"],
            owner_user_id="u2",
            installed_by="u2",
            credentials={},
        )


@pytest.mark.asyncio
async def test_market_edit_can_require_installer_credentials_without_reusing_source(
    db, monkeypatch
):
    source = _private_server(db, owner="temporary", server_id="installer_auth_source")
    source.owner_user_id = None
    db.commit()
    _patch_probe(monkeypatch)

    published = await market.publish_admin_server(
        db,
        source.server_id,
        category="信息检索",
        version="1.0.0",
        link_as_installation=False,
    )
    updated = market.update_market_item(
        db,
        published["slug"],
        requires_auth=True,
        credential_mode="installer",
    )
    assert updated["requires_auth"] is True
    assert updated["credentials_managed_by_admin"] is False
    assert updated["requires_user_credentials"] is True
    assert updated["auth_config"]["credential_mode"] == "installer"
    assert decrypt_mcp_headers(source.headers)["Authorization"] == "Bearer publisher-secret"

    with pytest.raises(BadRequestError, match="请填写安装凭据"):
        await market.install_market_item(
            db,
            published["slug"],
            owner_user_id="u2",
            installed_by="u2",
            credentials={},
        )


@pytest.mark.asyncio
async def test_market_edit_sets_admin_token_and_keeps_it_out_of_responses(db, monkeypatch):
    source = _private_server(db, owner="temporary", server_id="edited_admin_auth_source")
    source.owner_user_id = None
    db.commit()
    _patch_probe(monkeypatch)

    published = await market.publish_admin_server(
        db,
        source.server_id,
        category="信息检索",
        version="1.0.0",
        link_as_installation=False,
    )
    updated = market.update_market_item(
        db,
        published["slug"],
        requires_auth=True,
        credential_mode="admin",
        managed_credentials={"Authorization": "Bearer centrally-managed"},
    )
    assert updated["credentials_managed_by_admin"] is True
    assert updated["requires_user_credentials"] is False
    assert updated["auth_config"]["credential_mode"] == "admin"
    assert "centrally-managed" not in json.dumps(updated)
    db.refresh(source)
    assert decrypt_mcp_headers(source.headers)["Authorization"] == ("Bearer centrally-managed")
    assert "centrally-managed" not in str(source.headers)

    installed = await market.install_market_item(
        db,
        published["slug"],
        owner_user_id="u2",
        installed_by="u2",
        credentials={},
    )
    private_server = db.query(AdminMcpServer).filter_by(server_id=installed["server_id"]).one()
    assert decrypt_mcp_headers(private_server.headers)["Authorization"] == (
        "Bearer centrally-managed"
    )


@pytest.mark.asyncio
async def test_create_market_mcp_with_blank_token_requires_each_installer(db, monkeypatch):
    async def _validate(*args, **kwargs):
        return None

    async def _fail_probe(*args, **kwargs):
        return False, "401 Unauthorized"

    monkeypatch.setattr(market, "validate_remote_mcp_url", _validate)
    monkeypatch.setattr(market, "probe_mcp_connectivity", _fail_probe)
    monkeypatch.setattr(market, "refresh_mcp_caches", lambda: None)
    monkeypatch.setattr(admin_mcp_routes, "_probe_connectivity", _fail_probe)
    monkeypatch.setattr(admin_mcp_routes, "_sync_catalog_from_db", lambda db: None)
    monkeypatch.setattr(admin_mcp_routes, "_refresh_caches", lambda: None)

    request = admin_mcp_routes.McpServerCreateRequest(
        server_id="blank_token_mcp",
        display_name="Blank Token MCP",
        description="Discovers tools after the user authenticates",
        transport="streamable_http",
        url="https://mcp.example.test/mcp",
        publish_to_marketplace=True,
        market_requires_auth=True,
        market_auth_type="token",
        market_auth_header="Authorization",
        market_auth_prefix="Bearer ",
        market_admin_token="",
    )
    await admin_mcp_routes.create_mcp_server(request, db)

    listed = market.list_market_items(
        db,
        owner_user_id="u2",
        viewer_user_id="u2",
    )[
        "items"
    ][0]
    assert listed["requires_auth"] is True
    assert listed["requires_user_credentials"] is True
    assert listed["credentials_managed_by_admin"] is False
    assert listed["supports_admin_credentials"] is True
    assert listed["auth_config"]["credential_mode"] == "installer"
    assert listed["risk_report"]["discovery_mode"] == "per_install"
    assert listed["tool_count"] == 0

    async def _authenticated_probe(row, db=None):
        assert decrypt_mcp_headers(row.headers)["Authorization"] == "Bearer user-secret"
        row.tools_json = _tools("search_documents")
        return True, ""

    monkeypatch.setattr(market, "probe_mcp_connectivity", _authenticated_probe)
    installed = await market.install_market_item(
        db,
        listed["slug"],
        owner_user_id="u2",
        installed_by="u2",
        credentials={"Authorization": "user-secret"},
    )
    private_server = db.query(AdminMcpServer).filter_by(server_id=installed["server_id"]).one()
    assert decrypt_mcp_headers(private_server.headers)["Authorization"] == "Bearer user-secret"
    assert private_server.tools_json == _tools("search_documents")


@pytest.mark.asyncio
async def test_create_market_mcp_with_admin_token_makes_user_install_tokenless(db, monkeypatch):
    discovered_tools = _tools("search_documents")

    async def _validate(*args, **kwargs):
        return None

    async def _probe(row, db=None):
        row.tools_json = discovered_tools
        return True, ""

    monkeypatch.setattr(market, "validate_remote_mcp_url", _validate)
    monkeypatch.setattr(market, "probe_mcp_connectivity", _probe)
    monkeypatch.setattr(market, "refresh_mcp_caches", lambda: None)
    monkeypatch.setattr(admin_mcp_routes, "_probe_connectivity", _probe)
    monkeypatch.setattr(admin_mcp_routes, "_sync_catalog_from_db", lambda db: None)
    monkeypatch.setattr(admin_mcp_routes, "_refresh_caches", lambda: None)

    request = admin_mcp_routes.McpServerCreateRequest(
        server_id="admin_token_mcp",
        display_name="Admin Token MCP",
        description="Uses one administrator token",
        transport="streamable_http",
        url="https://mcp.example.test/mcp",
        publish_to_marketplace=True,
        market_requires_auth=True,
        market_auth_type="token",
        market_auth_header="Authorization",
        market_auth_prefix="Bearer ",
        market_admin_token="central-secret",
    )
    await admin_mcp_routes.create_mcp_server(request, db)

    source = db.query(AdminMcpServer).filter_by(server_id="admin_token_mcp").one()
    assert decrypt_mcp_headers(source.headers)["Authorization"] == "Bearer central-secret"
    assert "central-secret" not in str(source.headers)
    listed = market.list_market_items(
        db,
        owner_user_id="u2",
        viewer_user_id="u2",
    )[
        "items"
    ][0]
    assert listed["credentials_managed_by_admin"] is True
    assert listed["requires_user_credentials"] is False
    assert "central-secret" not in json.dumps(listed)


@pytest.mark.asyncio
async def test_create_market_mcp_with_oauth_defers_authorization_to_installer(db, monkeypatch):
    async def _validate(*args, **kwargs):
        return None

    async def _fail_probe(*args, **kwargs):
        return False, "401 Unauthorized"

    monkeypatch.setattr(market, "validate_remote_mcp_url", _validate)
    monkeypatch.setattr(market, "probe_mcp_connectivity", _fail_probe)
    monkeypatch.setattr(market, "refresh_mcp_caches", lambda: None)
    monkeypatch.setattr(admin_mcp_routes, "_probe_connectivity", _fail_probe)
    monkeypatch.setattr(admin_mcp_routes, "_sync_catalog_from_db", lambda db: None)
    monkeypatch.setattr(admin_mcp_routes, "_refresh_caches", lambda: None)

    request = admin_mcp_routes.McpServerCreateRequest(
        server_id="oauth_market_mcp",
        display_name="OAuth Market MCP",
        transport="streamable_http",
        url="https://mcp.example.test/mcp",
        publish_to_marketplace=True,
        market_requires_auth=True,
        market_auth_type="oauth2",
    )
    await admin_mcp_routes.create_mcp_server(request, db)

    listed = market.list_market_items(
        db,
        owner_user_id="u2",
        viewer_user_id="u2",
    )[
        "items"
    ][0]
    assert listed["requires_auth"] is True
    assert listed["requires_user_credentials"] is True
    assert listed["auth_config"]["default_method"] == "oauth2"
    assert listed["auth_config"]["methods"][0]["type"] == "oauth2"
    assert listed["risk_report"]["discovery_mode"] == "per_install"


@pytest.mark.asyncio
async def test_move_existing_published_server_removes_global_installation(db, monkeypatch):
    source = _private_server(db, owner="temporary", server_id="published_global")
    source.owner_user_id = None
    db.commit()
    _patch_probe(monkeypatch)

    await market.publish_admin_server(
        db,
        source.server_id,
        category="信息检索",
        version="1.0.0",
    )
    assert db.query(McpMarketInstallation).count() == 1

    moved = await market.publish_admin_server(
        db,
        source.server_id,
        category="信息检索",
        version="1.0.1",
        link_as_installation=False,
    )

    db.refresh(source)
    assert moved["installed"] is False
    assert source.is_enabled is False
    assert source.extra_config["market_source_only"] is True
    assert db.query(McpMarketInstallation).count() == 0


def test_edit_market_metadata_propagates_to_existing_installations(db, monkeypatch):
    item, version = _approved_market(db)
    installed_server = AdminMcpServer(
        server_id="installed_acme",
        display_name=item.display_name,
        description=item.description,
        transport=version.transport,
        url=version.url,
        is_stable=False,
        is_enabled=True,
        owner_user_id="u2",
    )
    db.add(installed_server)
    db.flush()
    db.add(
        McpMarketInstallation(
            install_id="mcpins_edit",
            slug=item.slug,
            version_id=version.version_id,
            server_id=installed_server.server_id,
            owner_user_id="u2",
            status="active",
        )
    )
    db.commit()
    monkeypatch.setattr(market, "refresh_mcp_caches", lambda: None)

    result = market.update_market_item(
        db,
        item.slug,
        display_name="Acme Knowledge Search",
        description="Search the updated Acme knowledge base",
        user_intro="## 用途\n检索 Acme 知识库",
        category="通用工具",
        tags=["search", "knowledge", "search"],
        icon="/home/mcp/knowledge.svg",
    )

    db.refresh(installed_server)
    assert result["display_name"] == "Acme Knowledge Search"
    assert result["updated_installations"] == 1
    assert result["tags"] == ["search", "knowledge"]
    assert installed_server.display_name == result["display_name"]
    assert installed_server.description == result["description"]
    assert installed_server.user_intro == result["user_intro"]
    assert installed_server.icon == result["icon"]
    assert db.query(McpMarketVersion).one().version == "1.0.0"
    assert db.query(McpMarketVersion).one().tool_hash == version.tool_hash


@pytest.mark.asyncio
async def test_install_uses_installers_own_encrypted_credentials(db, monkeypatch):
    _approved_market(db)
    _patch_probe(monkeypatch)

    first = await market.install_market_item(
        db,
        "acme-search",
        owner_user_id="u2",
        installed_by="u2",
        credentials={"Authorization": "Bearer user-two"},
    )
    second = await market.install_market_item(
        db,
        "acme-search",
        owner_user_id="u3",
        installed_by="u3",
        credentials={"Authorization": "Bearer user-three"},
    )
    first_server = db.query(AdminMcpServer).filter_by(server_id=first["server_id"]).one()
    second_server = db.query(AdminMcpServer).filter_by(server_id=second["server_id"]).one()
    assert first_server.owner_user_id == "u2"
    assert second_server.owner_user_id == "u3"
    assert decrypt_mcp_headers(first_server.headers)["Authorization"] == "Bearer user-two"
    assert decrypt_mcp_headers(second_server.headers)["Authorization"] == "Bearer user-three"
    assert first_server.headers != second_server.headers
    assert db.query(McpMarketInstallation).count() == 2


@pytest.mark.asyncio
async def test_curated_templates_seed_once_and_materialize_user_query_key(db, monkeypatch):
    assert set(market.ensure_curated_market_items(db)) == {
        "amap-maps",
        "metaso-search",
        "github-official",
        "gitlab-official",
        "alibaba-cloud-observability",
    }
    assert market.ensure_curated_market_items(db) == []
    gitlab_item = market.get_market_item(
        db,
        "gitlab-official",
        owner_user_id="u2",
        viewer_user_id="u2",
    )
    assert gitlab_item["auth_config"]["default_method"] == "oauth2"
    assert gitlab_item["supports_admin_credentials"] is False
    assert {method["type"] for method in gitlab_item["auth_config"]["methods"]} == {
        "oauth2",
        "token",
    }

    actual_tools = _tools("maps_geo", "maps_weather")
    _patch_probe(monkeypatch, tools=actual_tools)
    installed = await market.install_market_item(
        db,
        "amap-maps",
        owner_user_id="u2",
        installed_by="u2",
        credentials={"AMAP_API_KEY": "user-amap-key"},
    )
    server = db.query(AdminMcpServer).filter_by(server_id=installed["server_id"]).one()
    assert server.url == "https://mcp.amap.com/mcp"
    assert "user-amap-key" not in str(server.headers)
    runtime_url, runtime_headers = materialize_mcp_http_connection(
        server.url,
        decrypt_mcp_headers(server.headers),
    )
    assert runtime_url == "https://mcp.amap.com/mcp?key=user-amap-key"
    assert runtime_headers == {}
    assert server.tools_json == actual_tools
    assert server.owner_user_id == "u2"
    assert server.icon == "/home/mcp/internet.svg"


@pytest.mark.asyncio
async def test_curated_bearer_token_gets_prefix_and_can_be_rotated(db, monkeypatch):
    market.ensure_curated_market_items(db)
    _patch_probe(monkeypatch, tools=_tools("metaso_web_search"))

    installed = await market.install_market_item(
        db,
        "metaso-search",
        owner_user_id="u2",
        installed_by="u2",
        credentials={"METASO_API_KEY": "first-key"},
    )
    server = db.query(AdminMcpServer).filter_by(server_id=installed["server_id"]).one()
    assert decrypt_mcp_headers(server.headers)["Authorization"] == "Bearer first-key"

    updated = await market.install_market_item(
        db,
        "metaso-search",
        owner_user_id="u2",
        installed_by="u2",
        credentials={"METASO_API_KEY": "Bearer second-key"},
    )
    db.refresh(server)
    assert updated["action"] == "existing"
    assert decrypt_mcp_headers(server.headers)["Authorization"] == "Bearer second-key"


@pytest.mark.asyncio
async def test_high_risk_install_requires_explicit_confirmation(db, monkeypatch):
    _approved_market(db, risk_tool="delete_records")
    _patch_probe(monkeypatch)
    with pytest.raises(BadRequestError, match="高风险"):
        await market.install_market_item(
            db,
            "acme-search",
            owner_user_id="u2",
            installed_by="u2",
            credentials={"Authorization": "Bearer u2"},
        )
    result = await market.install_market_item(
        db,
        "acme-search",
        owner_user_id="u2",
        installed_by="u2",
        credentials={"Authorization": "Bearer u2"},
        confirm_high_risk=True,
    )
    assert result["risk_level"] == "high"


@pytest.mark.asyncio
async def test_tool_drift_pauses_new_installation(db, monkeypatch):
    _approved_market(db)
    _patch_probe(monkeypatch, tools=_tools("search_documents", "delete_documents"))
    with pytest.raises(BadRequestError, match="发生变化"):
        await market.install_market_item(
            db,
            "acme-search",
            owner_user_id="u2",
            installed_by="u2",
            credentials={"Authorization": "Bearer u2"},
        )
    item = db.query(McpMarketItem).filter_by(slug="acme-search").one()
    assert item.status == "changed"
    assert db.query(McpMarketInstallation).count() == 0


@pytest.mark.asyncio
async def test_failed_update_probe_never_commits_plaintext_credentials(db, monkeypatch):
    _approved_market(db)
    _patch_probe(monkeypatch)
    installed = await market.install_market_item(
        db,
        "acme-search",
        owner_user_id="u2",
        installed_by="u2",
        credentials={"Authorization": "Bearer original"},
    )
    _patch_probe(monkeypatch, tools=_tools("changed_tool"))

    with pytest.raises(BadRequestError, match="发生变化"):
        await market.install_market_item(
            db,
            "acme-search",
            owner_user_id="u2",
            installed_by="u2",
            credentials={"Authorization": "Bearer replacement"},
        )

    server = db.query(AdminMcpServer).filter_by(server_id=installed["server_id"]).one()
    assert decrypt_mcp_headers(server.headers)["Authorization"] == "Bearer original"
    assert "original" not in str(server.headers)
    assert "replacement" not in str(server.headers)


@pytest.mark.asyncio
async def test_security_suspension_disables_all_derived_servers(db, monkeypatch):
    _approved_market(db)
    _patch_probe(monkeypatch)
    await market.install_market_item(
        db,
        "acme-search",
        owner_user_id="u2",
        installed_by="u2",
        credentials={"Authorization": "Bearer u2"},
    )
    await market.install_market_item(
        db,
        "acme-search",
        owner_user_id="u3",
        installed_by="u3",
        credentials={"Authorization": "Bearer u3"},
    )
    result = market.set_suspended(db, "acme-search", suspended=True, reason="malware")
    assert result["disabled_installations"] == 2
    assert all(not row.is_enabled for row in db.query(AdminMcpServer).all())
    assert {row.status for row in db.query(McpMarketInstallation).all()} == {"suspended"}

    restored = market.set_suspended(db, "acme-search", suspended=False)
    assert restored["restored_installations"] == 2
    assert all(row.is_enabled for row in db.query(AdminMcpServer).all())
    assert {row.status for row in db.query(McpMarketInstallation).all()} == {"active"}


@pytest.mark.asyncio
async def test_review_rechecks_live_snapshot(db, monkeypatch):
    source = _private_server(db)
    _patch_probe(monkeypatch)
    submitted = await market.submit_to_marketplace(
        db,
        source.server_id,
        owner_user_id="u1",
        submitter_name="Alice",
        category="信息检索",
        version="1.0.0",
    )
    _patch_probe(monkeypatch, tools=_tools("changed_after_submission"))

    with pytest.raises(BadRequestError, match="重新提交"):
        await market.review_submission(
            db,
            submitted["submission_id"],
            approve=True,
            reviewed_by="admin",
        )

    assert db.query(McpMarketSubmission).one().status == "pending"
    assert db.query(McpMarketItem).count() == 0


@pytest.mark.asyncio
async def test_missing_source_marks_listing_changed(db):
    _approved_market(db)
    result = await market.revalidate_market_item(db, "acme-search")
    assert result["status"] == "changed"
    assert "不存在" in result["status_reason"]


@pytest.mark.skipif(_IS_CE_TREE, reason="CE marketplace listings are always public")
def test_mcp_visibility_uses_shared_marketplace_scope(db):
    item, _ = _approved_market(db)
    market.ml.set_listing_visibility(
        db,
        market.ml.KIND_MCP,
        item.slug,
        visibility="scoped",
        grants=[{"principal_type": "user", "principal_id": "u2"}],
    )
    visible = market.list_market_items(
        db,
        owner_user_id="u2",
        viewer_user_id="u2",
    )
    hidden = market.list_market_items(
        db,
        owner_user_id="u3",
        viewer_user_id="u3",
    )
    assert [row["slug"] for row in visible["items"]] == ["acme-search"]
    assert hidden["items"] == []


@pytest.mark.asyncio
async def test_delisted_item_blocks_user_install_but_not_admin_install(db, monkeypatch):
    item, _ = _approved_market(db)
    _patch_probe(monkeypatch)
    market.ml.set_listing_enabled(db, market.ml.KIND_MCP, item.slug, False)

    with pytest.raises(ResourceNotFoundError):
        await market.install_market_item(
            db,
            item.slug,
            owner_user_id="u2",
            installed_by="u2",
            credentials={"Authorization": "Bearer u2"},
        )

    installed = await market.install_market_item(
        db,
        item.slug,
        owner_user_id=None,
        installed_by="admin",
        credentials={"Authorization": "Bearer global"},
    )
    assert installed["action"] == "installed"
