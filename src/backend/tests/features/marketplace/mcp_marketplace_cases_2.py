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


def test_tool_hash_is_order_independent():
    first = _tools("search", "delete_record")
    second = list(reversed(first))
    assert tool_snapshot_hash(first) == tool_snapshot_hash(second)


@pytest.mark.asyncio
async def test_submit_snapshots_without_credentials_then_approve(db, monkeypatch):
    source = _private_server(db)
    _patch_probe(monkeypatch)

    result = await mcp_submissions.submit_to_marketplace(
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

    reviewed = await mcp_submissions.review_submission(
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

    async def _probe(row, **_options):
        return True, ""

    monkeypatch.setattr(mcp_installation, 'probe_mcp_connectivity', _probe)
    monkeypatch.setattr(mcp_revalidation, 'probe_mcp_connectivity', _probe)
    monkeypatch.setattr(mcp_submissions, 'probe_mcp_connectivity', _probe)
    monkeypatch.setattr(mcp_publishing, 'probe_mcp_connectivity', _probe)
    monkeypatch.setattr(mcp_installation, 'refresh_mcp_caches', lambda: None)
    monkeypatch.setattr(mcp_administration, 'refresh_mcp_caches', lambda: None)
    monkeypatch.setattr(mcp_versions, 'refresh_mcp_caches', lambda: None)
    monkeypatch.setattr(mcp_publishing, 'refresh_mcp_caches', lambda: None)

    submitted = await mcp_submissions.submit_to_marketplace(
        source.server_id,
        owner_user_id="u1",
        submitter_name="Alice",
        category="信息检索",
        version="1.0.0",
    )
    reviewed = await mcp_submissions.review_submission(
        submitted["submission_id"],
        approve=True,
        reviewed_by="admin",
    )
    installed = await mcp_installation.install_market_item(
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
        await mcp_submissions.submit_to_marketplace(
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

    submitted = await mcp_submissions.submit_to_marketplace(
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

    published = await mcp_publishing.publish_admin_server(
        source.server_id,
        mcp_publishing.Publication(
            category="信息检索",
            version="1.0.0",
        ),
    )
    assert published["installed"] is True
    installation = db.query(McpMarketInstallation).one()
    assert installation.server_id == source.server_id
    assert installation.owner_user_id is None
    assert db.query(AdminMcpServer).count() == 1
    listed = mcp_listing.list_market_items(
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

    published = await mcp_publishing.publish_admin_server(
        source.server_id,
        mcp_publishing.Publication(
            category="信息检索",
            version="1.0.0",
            link_as_installation=False,
        ),
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

    installed = await mcp_installation.install_market_item(
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

    published = await mcp_publishing.publish_admin_server(
        source.server_id,
        mcp_publishing.Publication(
            category="信息检索",
            version="1.0.0",
            link_as_installation=False,
        ),
    )
    listed = mcp_listing.get_market_item(
        db,
        published["slug"],
        owner_user_id="u2",
        viewer_user_id="u2",
    )
    assert listed["credentials_managed_by_admin"] is True
    assert listed["requires_user_credentials"] is False
    assert "publisher-secret" not in json.dumps(listed)

    installed = await mcp_installation.install_market_item(
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

    published = await mcp_publishing.publish_admin_server(
        source.server_id,
        mcp_publishing.Publication(
            category="信息检索",
            version="1.0.0",
            link_as_installation=False,
        ),
    )
    assert published["credentials_managed_by_admin"] is True
    assert published["url_origin"] == "https://mcp.example.test"
    assert "publisher-query-secret" not in json.dumps(published)

    installed = await mcp_installation.install_market_item(
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
