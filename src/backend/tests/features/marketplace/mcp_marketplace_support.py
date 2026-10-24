"""End-to-end service tests for the reviewed MCP marketplace lifecycle."""


from __future__ import annotations


from features.marketplace import edition as listing


from features.marketplace import mcp_administration


from features.marketplace import mcp_installation


from features.marketplace import mcp_publishing


from features.marketplace import mcp_revalidation


from features.marketplace import mcp_submissions


from features.marketplace import mcp_versions


from features.marketplace import mcp_administration


from features.marketplace import mcp_listing


from features.marketplace import mcp_curated


from features.marketplace import mcp_installation


from features.marketplace import mcp_publishing


from features.marketplace import mcp_revalidation


from features.marketplace import mcp_submissions


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


from core.db.models import AdminMcpServer


from features.marketplace.models import McpMarketInstallation, McpMarketItem, McpMarketSubmission, McpMarketVersion


from core.infra.exceptions import BadRequestError, ResourceNotFoundError


from core.llm.mcp.client import make_client


from core.capabilities.mcp import management as management


from core.capabilities.mcp import oauth as oauth_service


from core.capabilities.mcp.management import decrypt_mcp_headers, encrypt_legacy_mcp_headers, encrypt_mcp_headers, mask_mcp_headers, materialize_mcp_http_connection, mcp_oauth_bundle_storage_key, mcp_query_secret_storage_key, tool_snapshot_hash, validate_remote_mcp_url


from core.capabilities.mcp.oauth_storage import OAuthBundleStorage


from core.capabilities.mcp.service import McpServerConfigService


from mcp.shared.auth import OAuthToken


from sqlalchemy import create_engine


from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from tests.db_support import share_factory


_IS_CE_TREE = not (Path(__file__).resolve().parents[3] / "edition_ee").exists()


@pytest.fixture
def db(monkeypatch):
    # One connection for every thread: marketplace steps run in worker threads.
    engine = create_engine(
        "sqlite://", future=True, poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, future=True)
    session = Session()
    share_factory(monkeypatch, Session)
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


def _private_server(db, *, owner: str = "u1", server_id: str = "umcp_source"):
    row = AdminMcpServer(
        server_id=server_id,
        display_name="Acme Search",
        description="Search Acme documents",
        transport="streamable_http",
        url="https://mcp.example.test/mcp",
        headers=encrypt_mcp_headers({"Authorization": "Bearer publisher-secret"}),
        tools_json=_tools("search_documents"),
        is_enabled=True,
        owner_user_id=owner,
    )
    db.add(row)
    db.commit()
    return row


def _approved_market(db, *, tools=None):
    snapshot = tools or _tools("search_documents")
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
        source_server_id="umcp_source",
    )
    db.add_all([item, version])
    db.commit()
    return item, version


def _patch_probe(monkeypatch, tools=None):
    async def _validate(*args, **kwargs):
        return None

    async def _probe(row, **_options):
        if tools is not None:
            row.tools_json = tools
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


def _admin_private_market(db, *, auth_schema=None, url: str = "http://mcp:9102/mcp/"):
    """An admin-published item whose endpoint lives on the deployment's own network."""
    snapshot = _tools("search_documents")
    item = McpMarketItem(
        slug="builtin-search",
        display_name="联网搜索",
        description="Built-in search MCP",
        category="信息检索",
        tags=["search"],
        publisher_name="平台",
        source="admin",
        latest_version_id="mcpver_builtin",
        status="active",
    )
    version = McpMarketVersion(
        version_id="mcpver_builtin",
        slug="builtin-search",
        version="1.0.0",
        transport="streamable_http",
        url=url,
        auth_schema=auth_schema or [],
        tools_json=snapshot,
        tool_hash=tool_snapshot_hash(snapshot),
    )
    db.add_all([item, version])
    db.commit()
    return item, version


def _patch_install_side_effects(monkeypatch, *, resolves_to: str = "172.27.0.4"):
    """Keep the real URL guard in play while stubbing the network around it."""

    async def _probe(row, **_options):
        return True, ""

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (resolves_to, 9102))],
    )
    monkeypatch.setattr(mcp_installation, 'probe_mcp_connectivity', _probe)
    monkeypatch.setattr(mcp_revalidation, 'probe_mcp_connectivity', _probe)
    monkeypatch.setattr(mcp_submissions, 'probe_mcp_connectivity', _probe)
    monkeypatch.setattr(mcp_publishing, 'probe_mcp_connectivity', _probe)
    monkeypatch.setattr(mcp_installation, 'refresh_mcp_caches', lambda: None)
    monkeypatch.setattr(mcp_administration, 'refresh_mcp_caches', lambda: None)
    monkeypatch.setattr(mcp_versions, 'refresh_mcp_caches', lambda: None)
    monkeypatch.setattr(mcp_publishing, 'refresh_mcp_caches', lambda: None)
