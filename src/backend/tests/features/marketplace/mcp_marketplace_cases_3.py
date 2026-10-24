from __future__ import annotations
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
async def test_removed_admin_credential_falls_back_to_installer_input(db, monkeypatch):
    source = _private_server(db, owner="temporary", server_id="unmanaged_market_source")
    source.owner_user_id = None
    db.commit()
    _patch_probe(monkeypatch)

    published = await mcp_publishing.publish_admin_server(
        source.server_id,
        mcp_publishing.Publication(
            category="信息检索",
            version="1.0.0",
            link_as_installation=False,
        ),
    )
    source.headers = {}
    db.commit()
    listed = mcp_listing.get_market_item(
        db,
        published["slug"],
        owner_user_id="u2",
        viewer_user_id="u2",
    )
    assert listed["credentials_managed_by_admin"] is False
    assert listed["requires_user_credentials"] is True

    with pytest.raises(BadRequestError, match="请填写安装凭据"):
        await mcp_installation.install_market_item(
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

    published = await mcp_publishing.publish_admin_server(
        source.server_id,
        mcp_publishing.Publication(
            category="信息检索",
            version="1.0.0",
            link_as_installation=False,
        ),
    )
    updated = mcp_administration.update_market_item(
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
        await mcp_installation.install_market_item(
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

    published = await mcp_publishing.publish_admin_server(
        source.server_id,
        mcp_publishing.Publication(
            category="信息检索",
            version="1.0.0",
            link_as_installation=False,
        ),
    )
    updated = mcp_administration.update_market_item(
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

    installed = await mcp_installation.install_market_item(
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

    monkeypatch.setattr(mcp_installation, 'validate_remote_mcp_url', _validate)
    monkeypatch.setattr(mcp_revalidation, 'validate_remote_mcp_url', _validate)
    monkeypatch.setattr(mcp_submissions, 'validate_remote_mcp_url', _validate)
    monkeypatch.setattr(mcp_publishing, 'validate_remote_mcp_url', _validate)
    monkeypatch.setattr(mcp_installation, 'probe_mcp_connectivity', _fail_probe)
    monkeypatch.setattr(mcp_revalidation, 'probe_mcp_connectivity', _fail_probe)
    monkeypatch.setattr(mcp_submissions, 'probe_mcp_connectivity', _fail_probe)
    monkeypatch.setattr(mcp_publishing, 'probe_mcp_connectivity', _fail_probe)
    monkeypatch.setattr(mcp_installation, 'refresh_mcp_caches', lambda: None)
    monkeypatch.setattr(mcp_administration, 'refresh_mcp_caches', lambda: None)
    monkeypatch.setattr(mcp_versions, 'refresh_mcp_caches', lambda: None)
    monkeypatch.setattr(mcp_publishing, 'refresh_mcp_caches', lambda: None)
    monkeypatch.setattr(admin_mcp_routes, "_probe_connectivity", _fail_probe)
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
    await admin_mcp_routes.create_mcp_server(request)

    listed = mcp_listing.list_market_items(
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
    assert listed["listing_notice"]["discovery_mode"] == "per_install"
    assert listed["tool_count"] == 0

    async def _authenticated_probe(row, **_options):
        assert decrypt_mcp_headers(row.headers)["Authorization"] == "Bearer user-secret"
        row.tools_json = _tools("search_documents")
        return True, ""

    monkeypatch.setattr(mcp_installation, 'probe_mcp_connectivity', _authenticated_probe)
    monkeypatch.setattr(mcp_revalidation, 'probe_mcp_connectivity', _authenticated_probe)
    monkeypatch.setattr(mcp_submissions, 'probe_mcp_connectivity', _authenticated_probe)
    monkeypatch.setattr(mcp_publishing, 'probe_mcp_connectivity', _authenticated_probe)
    installed = await mcp_installation.install_market_item(
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

    async def _probe(row, **_options):
        row.tools_json = discovered_tools
        return True, ""

    monkeypatch.setattr(mcp_installation, 'validate_remote_mcp_url', _validate)
    monkeypatch.setattr(mcp_revalidation, 'validate_remote_mcp_url', _validate)
    monkeypatch.setattr(mcp_submissions, 'validate_remote_mcp_url', _validate)
    monkeypatch.setattr(mcp_publishing, 'validate_remote_mcp_url', _validate)
    monkeypatch.setattr(mcp_installation, 'probe_mcp_connectivity', _probe)
    monkeypatch.setattr(mcp_revalidation, 'probe_mcp_connectivity', _probe)
    monkeypatch.setattr(mcp_submissions, 'probe_mcp_connectivity', _probe)
    monkeypatch.setattr(mcp_publishing, 'probe_mcp_connectivity', _probe)
    monkeypatch.setattr(mcp_installation, 'refresh_mcp_caches', lambda: None)
    monkeypatch.setattr(mcp_administration, 'refresh_mcp_caches', lambda: None)
    monkeypatch.setattr(mcp_versions, 'refresh_mcp_caches', lambda: None)
    monkeypatch.setattr(mcp_publishing, 'refresh_mcp_caches', lambda: None)
    monkeypatch.setattr(admin_mcp_routes, "_probe_connectivity", _probe)
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
    await admin_mcp_routes.create_mcp_server(request)

    source = db.query(AdminMcpServer).filter_by(server_id="admin_token_mcp").one()
    assert decrypt_mcp_headers(source.headers)["Authorization"] == "Bearer central-secret"
    assert "central-secret" not in str(source.headers)
    listed = mcp_listing.list_market_items(
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

    monkeypatch.setattr(mcp_installation, 'validate_remote_mcp_url', _validate)
    monkeypatch.setattr(mcp_revalidation, 'validate_remote_mcp_url', _validate)
    monkeypatch.setattr(mcp_submissions, 'validate_remote_mcp_url', _validate)
    monkeypatch.setattr(mcp_publishing, 'validate_remote_mcp_url', _validate)
    monkeypatch.setattr(mcp_installation, 'probe_mcp_connectivity', _fail_probe)
    monkeypatch.setattr(mcp_revalidation, 'probe_mcp_connectivity', _fail_probe)
    monkeypatch.setattr(mcp_submissions, 'probe_mcp_connectivity', _fail_probe)
    monkeypatch.setattr(mcp_publishing, 'probe_mcp_connectivity', _fail_probe)
    monkeypatch.setattr(mcp_installation, 'refresh_mcp_caches', lambda: None)
    monkeypatch.setattr(mcp_administration, 'refresh_mcp_caches', lambda: None)
    monkeypatch.setattr(mcp_versions, 'refresh_mcp_caches', lambda: None)
    monkeypatch.setattr(mcp_publishing, 'refresh_mcp_caches', lambda: None)
    monkeypatch.setattr(admin_mcp_routes, "_probe_connectivity", _fail_probe)
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
    await admin_mcp_routes.create_mcp_server(request)

    listed = mcp_listing.list_market_items(
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
    assert listed["listing_notice"]["discovery_mode"] == "per_install"


@pytest.mark.asyncio
async def test_move_existing_published_server_removes_global_installation(db, monkeypatch):
    source = _private_server(db, owner="temporary", server_id="published_global")
    source.owner_user_id = None
    db.commit()
    _patch_probe(monkeypatch)

    await mcp_publishing.publish_admin_server(
        source.server_id,
        mcp_publishing.Publication(
            category="信息检索",
            version="1.0.0",
        ),
    )
    assert db.query(McpMarketInstallation).count() == 1

    moved = await mcp_publishing.publish_admin_server(
        source.server_id,
        mcp_publishing.Publication(
            category="信息检索",
            version="1.0.1",
            link_as_installation=False,
        ),
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
    monkeypatch.setattr(mcp_installation, 'refresh_mcp_caches', lambda: None)
    monkeypatch.setattr(mcp_administration, 'refresh_mcp_caches', lambda: None)
    monkeypatch.setattr(mcp_versions, 'refresh_mcp_caches', lambda: None)
    monkeypatch.setattr(mcp_publishing, 'refresh_mcp_caches', lambda: None)

    result = mcp_administration.update_market_item(
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

    first = await mcp_installation.install_market_item(
        "acme-search",
        owner_user_id="u2",
        installed_by="u2",
        credentials={"Authorization": "Bearer user-two"},
    )
    second = await mcp_installation.install_market_item(
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
