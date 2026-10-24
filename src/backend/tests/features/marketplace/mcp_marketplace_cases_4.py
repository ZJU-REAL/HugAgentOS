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
async def test_curated_templates_seed_once_and_materialize_user_query_key(db, monkeypatch):
    assert set(mcp_curated.ensure_curated_market_items(db)) == {
        "amap-maps",
        "metaso-search",
        "github-official",
        "gitlab-official",
        "alibaba-cloud-observability",
        "notion-official",
    }
    assert mcp_curated.ensure_curated_market_items(db) == []
    gitlab_item = mcp_listing.get_market_item(
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
    installed = await mcp_installation.install_market_item(
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
    mcp_curated.ensure_curated_market_items(db)
    _patch_probe(monkeypatch, tools=_tools("metaso_web_search"))

    installed = await mcp_installation.install_market_item(
        "metaso-search",
        owner_user_id="u2",
        installed_by="u2",
        credentials={"METASO_API_KEY": "first-key"},
    )
    server = db.query(AdminMcpServer).filter_by(server_id=installed["server_id"]).one()
    assert decrypt_mcp_headers(server.headers)["Authorization"] == "Bearer first-key"

    updated = await mcp_installation.install_market_item(
        "metaso-search",
        owner_user_id="u2",
        installed_by="u2",
        credentials={"METASO_API_KEY": "Bearer second-key"},
    )
    db.refresh(server)
    assert updated["action"] == "existing"
    assert decrypt_mcp_headers(server.headers)["Authorization"] == "Bearer second-key"


@pytest.mark.asyncio
async def test_tool_drift_pauses_new_installation(db, monkeypatch):
    _approved_market(db)
    _patch_probe(monkeypatch, tools=_tools("search_documents", "delete_documents"))
    with pytest.raises(BadRequestError, match="发生变化"):
        await mcp_installation.install_market_item(
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
    installed = await mcp_installation.install_market_item(
        "acme-search",
        owner_user_id="u2",
        installed_by="u2",
        credentials={"Authorization": "Bearer original"},
    )
    _patch_probe(monkeypatch, tools=_tools("changed_tool"))

    with pytest.raises(BadRequestError, match="发生变化"):
        await mcp_installation.install_market_item(
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
    await mcp_installation.install_market_item(
        "acme-search",
        owner_user_id="u2",
        installed_by="u2",
        credentials={"Authorization": "Bearer u2"},
    )
    await mcp_installation.install_market_item(
        "acme-search",
        owner_user_id="u3",
        installed_by="u3",
        credentials={"Authorization": "Bearer u3"},
    )
    result = mcp_administration.set_suspended(db, "acme-search", suspended=True, reason="malware")
    assert result["disabled_installations"] == 2
    assert all(not row.is_enabled for row in db.query(AdminMcpServer).all())
    assert {row.status for row in db.query(McpMarketInstallation).all()} == {"suspended"}

    restored = mcp_administration.set_suspended(db, "acme-search", suspended=False)
    assert restored["restored_installations"] == 2
    assert all(row.is_enabled for row in db.query(AdminMcpServer).all())
    assert {row.status for row in db.query(McpMarketInstallation).all()} == {"active"}


@pytest.mark.asyncio
async def test_review_rechecks_live_snapshot(db, monkeypatch):
    source = _private_server(db)
    _patch_probe(monkeypatch)
    submitted = await mcp_submissions.submit_to_marketplace(
        source.server_id,
        owner_user_id="u1",
        submitter_name="Alice",
        category="信息检索",
        version="1.0.0",
    )
    _patch_probe(monkeypatch, tools=_tools("changed_after_submission"))

    with pytest.raises(BadRequestError, match="重新提交"):
        await mcp_submissions.review_submission(
            submitted["submission_id"],
            approve=True,
            reviewed_by="admin",
        )

    assert db.query(McpMarketSubmission).one().status == "pending"
    assert db.query(McpMarketItem).count() == 0


@pytest.mark.asyncio
async def test_missing_source_revalidates_from_reviewed_version(db, monkeypatch):
    _approved_market(db)
    _patch_probe(monkeypatch)
    result = await mcp_revalidation.revalidate_market_item("acme-search")

    assert result["status"] == "active"
    assert result["status_reason"] == ""


@pytest.mark.asyncio
async def test_missing_source_still_detects_live_snapshot_drift(db, monkeypatch):
    _approved_market(db)
    _patch_probe(monkeypatch, tools=_tools("changed_after_review"))

    result = await mcp_revalidation.revalidate_market_item("acme-search")

    assert result["status"] == "changed"
    assert "需发布新版本" in result["status_reason"]


@pytest.mark.asyncio
async def test_manual_admin_revalidation_accepts_drift_as_new_version(db, monkeypatch):
    source = _private_server(db)
    source.owner_user_id = None
    item, old_version = _approved_market(db)
    item.source = "admin"
    db.commit()
    changed_tools = _tools("search_documents", "list_accessible_projects")
    _patch_probe(monkeypatch, tools=changed_tools)

    result = await mcp_revalidation.revalidate_market_item(
        "acme-search",
        accept_changes=True,
    )

    assert result["status"] == "active"
    assert result["snapshot_accepted"] is True
    assert result["previous_version"] == "1.0.0"
    assert result["version"] == "1.0.1"
    versions = {
        row.version: row for row in db.query(McpMarketVersion).filter_by(slug="acme-search").all()
    }
    assert set(versions) == {"1.0.0", "1.0.1"}
    assert versions["1.0.0"].tool_hash == old_version.tool_hash
    assert versions["1.0.1"].tool_hash == tool_snapshot_hash(changed_tools)
    assert versions["1.0.1"].approved_by == "admin-revalidation"
    assert item.latest_version_id == versions["1.0.1"].version_id


@pytest.mark.asyncio
async def test_periodic_revalidation_never_auto_accepts_drift(db, monkeypatch):
    source = _private_server(db)
    source.owner_user_id = None
    item, _ = _approved_market(db)
    item.source = "admin"
    db.commit()
    _patch_probe(monkeypatch, tools=_tools("changed_after_review"))

    result = await mcp_revalidation.revalidate_market_item("acme-search")

    assert result["status"] == "changed"
    assert db.query(McpMarketVersion).filter_by(slug="acme-search").count() == 1


@pytest.mark.asyncio
async def test_manual_revalidation_rejects_empty_tool_snapshot(db, monkeypatch):
    source = _private_server(db)
    source.owner_user_id = None
    item, _ = _approved_market(db)
    item.source = "admin"
    db.commit()
    _patch_probe(monkeypatch, tools=[])

    result = await mcp_revalidation.revalidate_market_item(
        "acme-search",
        accept_changes=True,
    )

    assert result["status"] == "changed"
    assert "未返回任何工具" in result["status_reason"]
    assert db.query(McpMarketVersion).filter_by(slug="acme-search").count() == 1


@pytest.mark.skipif(_IS_CE_TREE, reason="CE marketplace listings are always public")
def test_mcp_visibility_uses_shared_marketplace_scope(db):
    item, _ = _approved_market(db)
    listing.set_listing_visibility(
        db,
        listing.KIND_MCP,
        item.slug,
        visibility="scoped",
        grants=[{"principal_type": "user", "principal_id": "u2"}],
    )
    visible = mcp_listing.list_market_items(
        db,
        owner_user_id="u2",
        viewer_user_id="u2",
    )
    hidden = mcp_listing.list_market_items(
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
    listing.set_listing_enabled(db, listing.KIND_MCP, item.slug, False)

    with pytest.raises(ResourceNotFoundError):
        await mcp_installation.install_market_item(
            item.slug,
            owner_user_id="u2",
            installed_by="u2",
            credentials={"Authorization": "Bearer u2"},
        )

    installed = await mcp_installation.install_market_item(
        item.slug,
        owner_user_id=None,
        installed_by="admin",
        credentials={"Authorization": "Bearer global"},
    )
    assert installed["action"] == "installed"


@pytest.mark.asyncio
async def test_user_installs_admin_published_private_network_mcp(db, monkeypatch):
    _admin_private_market(db)
    _patch_install_side_effects(monkeypatch)

    installed = await mcp_installation.install_market_item(
        "builtin-search",
        owner_user_id="u2",
        installed_by="u2",
        credentials={},
    )

    row = db.query(AdminMcpServer).filter(AdminMcpServer.owner_user_id == "u2").one()
    assert installed["server_id"] == row.server_id
    assert row.url == "http://mcp:9102/mcp/"


@pytest.mark.asyncio
async def test_installer_cannot_redirect_an_admin_item_to_another_private_host(db, monkeypatch):
    _admin_private_market(
        db,
        auth_schema=[
            {
                "key": "ENDPOINT",
                "label": "Endpoint",
                "target": "url",
                "required": True,
                "secret": True,
            }
        ],
    )
    _patch_install_side_effects(monkeypatch)

    with pytest.raises(BadRequestError, match="本机、内网或保留地址"):
        await mcp_installation.install_market_item(
            "builtin-search",
            owner_user_id="u2",
            installed_by="u2",
            credentials={"ENDPOINT": "http://127.0.0.1:9102/mcp/"},
        )
    assert db.query(AdminMcpServer).count() == 0


@pytest.mark.asyncio
async def test_installer_may_keep_the_admin_host_when_overriding_the_endpoint(db, monkeypatch):
    _admin_private_market(
        db,
        auth_schema=[
            {
                "key": "ENDPOINT",
                "label": "Endpoint",
                "target": "url",
                "required": True,
                "secret": True,
            }
        ],
    )
    _patch_install_side_effects(monkeypatch)

    await mcp_installation.install_market_item(
        "builtin-search",
        owner_user_id="u2",
        installed_by="u2",
        credentials={"ENDPOINT": "http://mcp:9102/mcp/tenant-a"},
    )

    assert db.query(AdminMcpServer).filter(AdminMcpServer.owner_user_id == "u2").count() == 1
