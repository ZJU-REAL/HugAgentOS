"""Plugin system tests: import Claude Code / Codex / native plugin packages → persist to DB → uninstall.

Covers the three-tier portability matrix: direct skill import (including
references + path-variable rewriting), direct remote MCP import, stdio MCP
disabled on install, and drop warnings for hooks/commands/agents.
See internal design docs §10.
"""

import json


import pytest

from core.db.models import AdminMcpServer, AdminSkill, InstalledPlugin

from core.infra.exceptions import BadRequestError

from core.plugins.packaging.archive import _extract_plugin_zip
from core.plugins.packaging import importer as pi

from core.plugins import management as ps

from core.plugins.management import packages as plugin_packages
from core.plugins.packaging import sources as plugin_sources


OWNER = "test_user_123"


from tests.plugin_test_support import _zip_cc_plugin, _make_standard_plugin


def test_import_zip_rejects_backslash_path_traversal():
    """Normalizing Windows separators must not reopen a ZIP traversal path."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("plugin.json", json.dumps({"name": "unsafe-plugin"}))
        zf.writestr("..\\outside.txt", "blocked")

    with pytest.raises(BadRequestError, match="非法压缩包条目"):
        with _extract_plugin_zip(buf.getvalue()):
            pass


def test_long_namespaced_component_ids_remain_unique():
    """Long skill/MCP names with the same prefix must not overwrite one another."""
    slug = "petrochemical-product-skills-v1-0"
    skill_ids = {
        plugin_sources._make_skill_id(slug, name, OWNER)
        for name in (
            "petrochemical-prosperity-analysis",
            "petrochemical-price-trend",
            "petrochemical-product-opportunity-analysis",
        )
    }
    server_ids = {
        plugin_sources._make_server_id(slug, name, OWNER)
        for name in (
            "petrochemical-product-market-data-provider-primary",
            "petrochemical-product-market-data-provider-secondary",
        )
    }

    assert len(skill_ids) == 3
    assert len(server_ids) == 2
    assert all(len(item) <= 63 for item in skill_ids)


def test_global_plugin_visible_to_user(tmp_path, db_session):
    """Plugins globally installed by an admin are visible in a front-end user's own plugin list (read-only) + detail viewable."""
    zip_bytes = _zip_cc_plugin(tmp_path)
    ps.import_plugin_from_zip(db_session, zip_bytes, owner_user_id=None, secrets={"api_token": "g"})

    # User perspective: include_global=True → sees the global plugin, marked is_global
    items = ps.list_installed(db_session, owner_user_id="random_user", include_global=True)
    g = next((p for p in items if p["slug"] == "hello-toolkit"), None)
    assert g is not None and g["is_global"] is True
    # Without include_global → not visible (own plugins only)
    assert ps.list_installed(db_session, owner_user_id="random_user") == []

    # A user can view global plugin detail (owner=None is visible to everyone)
    detail = ps.get_installed_detail(db_session, g["install_id"], owner_user_id="random_user")
    assert detail["is_global"] is True and len(detail["skills"]) == 2


def test_user_toggle_global_plugin_per_user(tmp_path, db_session):
    """A user disabling a global plugin = writing their own catalog override (kind=skill/mcp), leaving the global state untouched and other users unaffected.

    (Note: assert directly on CatalogOverride here instead of going through
    resolve_all_runtime_enabled — the latter reads the global catalog engine
    rather than the test db_session, which is unavailable in unit tests;
    per-user effectiveness is verified inside the container.)
    """
    from core.services.catalog_service import CatalogService

    zip_bytes = _zip_cc_plugin(tmp_path)
    res = ps.import_plugin_from_zip(
        db_session, zip_bytes, owner_user_id=None, secrets={"api_token": "g"}
    )
    install_id = res["install_id"]
    sids = [x["id"] for x in res["import_report"]["imported"] if x["type"] == "skill"]

    # User A disables this global plugin (for themselves) → writes a per-user override
    ps.set_plugin_enabled_for_user(db_session, install_id, enabled=False, user_id="userA")
    ov_a = CatalogService(db_session).get_user_overrides("userA")
    disabled = {o["id"] for o in ov_a.get("skills", []) if o["enabled"] is False}
    assert all(s in disabled for s in sids)
    # User B did nothing → no override
    assert CatalogService(db_session).get_user_overrides("userB").get("skills", []) == []
    # Global is_enabled was not modified
    for s in db_session.query(AdminSkill).filter(AdminSkill.skill_id.in_(sids)).all():
        assert s.is_enabled is True


def test_publish_zip_to_market_then_install(tmp_path, db_session):
    """Admin uploads a zip → publishes a DB market package (no install); only installing from the market creates an InstalledPlugin."""
    zip_bytes = _zip_cc_plugin(tmp_path)

    # 1. Publish: creates a PluginMarketPackage, no InstalledPlugin
    res = ps.publish_plugin_zip_to_market(db_session, zip_bytes)
    assert res["action"] == "published" and res["slug"] == "hello-toolkit"
    assert res["skills_count"] == 2
    assert db_session.query(InstalledPlugin).count() == 0

    # 2. The market list shows it, marked source=uploaded, not installed
    market = ps.list_plugins(db_session, owner_user_id=None)
    m = next((p for p in market if p["slug"] == "hello-toolkit"), None)
    assert m is not None and m["source"] == "uploaded" and m["installed"] is False

    # 3. Detail (unzips the DB package and re-normalizes)
    detail = ps.get_plugin_detail("hello-toolkit", db_session)
    assert len(detail["skills"]) == 2

    # 4. Install from the market → creates InstalledPlugin + global skills
    inst = ps.install_plugin(
        db_session, "hello-toolkit", owner_user_id=None, secrets={"api_token": "x"}
    )
    assert inst["action"] == "installed"
    assert db_session.query(InstalledPlugin).count() == 1
    market2 = ps.list_plugins(db_session, owner_user_id=None)
    assert next(p for p in market2 if p["slug"] == "hello-toolkit")["installed"] is True

    # 5. Publishing again = update; deleting the market package does not affect installed instances
    res2 = ps.publish_plugin_zip_to_market(db_session, zip_bytes)
    assert res2["action"] == "updated"
    ps.delete_market_package(db_session, "hello-toolkit")
    from core.db.models import PluginMarketPackage

    assert db_session.query(PluginMarketPackage).count() == 0
    assert db_session.query(InstalledPlugin).count() == 1  # the installed instance is still there


def test_app_registers_plugin_router():
    """The FastAPI app loads, and the /v1/plugins routes are registered."""
    from api.app import app

    paths = {r.path for r in app.routes}
    assert "/v1/plugins" in paths
    assert "/v1/plugins/import" in paths
    assert "/v1/plugins/feishu-cli/app/status" in paths
    assert "/v1/plugins/feishu-cli/app/init" in paths
    assert "/v1/plugins/feishu-cli/app/reset" in paths


@pytest.mark.asyncio
async def test_feishu_plugin_app_routes_delegate_to_lark_service(monkeypatch):
    from api.routes.v1 import plugins as plugin_routes
    from core.services import lark_service

    calls = []

    class FakeLarkService:
        def __init__(self, db):
            assert db is None

        def app_status(self):
            calls.append("status")
            return {"configured": True, "status": "configured"}

        async def start_app_init(self):
            calls.append("init")
            return {"configured": False, "status": "pending"}

        async def reset_app(self):
            calls.append("reset")
            return {"configured": False, "status": "idle"}

    monkeypatch.setattr(lark_service, "LarkService", FakeLarkService)

    status = await plugin_routes.get_feishu_app_status(_="admin")
    started = await plugin_routes.init_feishu_app(_="admin")
    reset = await plugin_routes.reset_feishu_app(_="admin")

    assert status["data"]["configured"] is True
    assert started["data"]["status"] == "pending"
    assert reset["data"]["status"] == "idle"
    assert calls == ["status", "init", "reset"]


def test_route_import_and_uninstall_e2e(tmp_path, db_session):
    """Import a plugin zip through the real FastAPI route stack, then list and uninstall."""
    from api.app import app
    from core.auth.backend import UserContext, get_current_user
    from core.db.engine import get_db
    from core.db.models import UserShadow
    from fastapi.testclient import TestClient

    # Create a test user with can_import_plugin enabled
    db_session.add(
        UserShadow(
            user_id=OWNER,
            username="Tester",
            extra_data={"can_import_plugin": True},
        )
    )
    db_session.commit()

    def _override_db():
        yield db_session

    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=OWNER,
        user_center_id="c1",
        username="Tester",
        email="t@e.com",
    )
    app.dependency_overrides[get_db] = _override_db

    client = TestClient(app)
    try:
        zip_bytes = _zip_cc_plugin(tmp_path)

        # Import
        resp = client.post(
            "/v1/plugins/import",
            files={"file": ("hello-toolkit.zip", zip_bytes, "application/zip")},
            data={"secrets": json.dumps({"api_token": "sk-e2e"})},
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()["data"]
        assert data["kind"] == "claude"
        install_id = data["install_id"]
        report = data["import_report"]
        assert len([x for x in report["imported"] if x["type"] == "skill"]) == 2
        assert len(report["dropped"]) >= 3  # hooks + command + subagent

        # Installed list
        resp = client.get("/v1/plugins/installed")
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        assert any(it["install_id"] == install_id for it in items)

        # DB persistence check: skills carry source_plugin, path variables rewritten
        sk = db_session.query(AdminSkill).filter(AdminSkill.source_plugin == "hello-toolkit").all()
        assert len(sk) == 2

        # Uninstall
        resp = client.delete(f"/v1/plugins/installed/{install_id}")
        assert resp.status_code == 200
        assert (
            db_session.query(AdminSkill).filter(AdminSkill.source_plugin == "hello-toolkit").count()
            == 0
        )
    finally:
        app.dependency_overrides.clear()


def test_normalize_standard_plugin(tmp_path):
    np = pi.normalize_plugin_dir(_make_standard_plugin(tmp_path))
    assert np.kind == "native"
    assert np.slug == "std-toolkit"
    assert np.version == "1.2.3"
    # Platform fields come from the extension namespace
    assert np.connection == "lark"
    assert np.admin_config and np.admin_config["fields"][0]["key"] == "std.url"
    assert [s["key"] for s in np.required_secrets] == ["api_key"]
    # mcp.json type discriminator wins; ext metadata overlays display fields
    by_name = {m.name: m for m in np.mcp}
    remote = by_name["std-remote"]
    assert remote.transport == "streamable_http" and not remote.needs_runtime
    assert remote.display_name == "标准远程"
    assert [t["name"] for t in remote.tools] == ["ping"]
    local = by_name["std-local"]
    assert local.transport == "stdio" and local.needs_runtime
    assert local.cwd and "${PLUGIN_ROOT}" not in local.cwd
    # default_enabled derives from the filesystem: skills + remote MCP on, stdio off
    assert np.default_enabled["skills"] == ["std-skill"]
    assert np.default_enabled["mcp"] == ["std-remote"]


def test_standard_plugin_import_persists_cwd_and_meta(tmp_path, db_session):
    pdir = _make_standard_plugin(tmp_path)
    ps.import_plugin(db_session, pdir, owner_user_id=OWNER, secrets={"api_key": "k"})
    # Standard path variables (${PLUGIN_ROOT}/${PLUGIN_DATA}) rewritten at persist time
    sk = db_session.query(AdminSkill).filter(AdminSkill.source_plugin == "std-toolkit").first()
    assert "${PLUGIN_ROOT}" not in sk.skill_content
    assert "${PLUGIN_DATA}" not in sk.skill_content
    assert "/workspace/skills/" in sk.skill_content
    servers = {
        m.server_id: m
        for m in db_session.query(AdminMcpServer)
        .filter(AdminMcpServer.source_plugin == "std-toolkit")
        .all()
    }
    local = next(m for sid, m in servers.items() if m.transport == "stdio")
    assert (local.extra_config or {}).get("cwd", "").endswith("/srv")
    assert local.is_enabled is False  # stdio installed disabled
    remote = next(m for sid, m in servers.items() if m.transport == "streamable_http")
    assert remote.display_name == "标准远程"
    assert [t["name"] for t in (remote.tools_json or [])] == ["ping"]


def test_legacy_topfield_manifest_still_imports(tmp_path):
    """Legacy native manifests with platform fields at the top level keep working."""
    pdir = tmp_path / "legacy-pack"
    pdir.mkdir()
    (pdir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "legacy-pack",
                "display_name": "旧版包",
                "category": "效率工具",
                "connection": "dingtalk",
                "mcpServers": {
                    "old-remote": {"transport": "streamable_http", "url": "http://x/mcp/"}
                },
            }
        ),
        encoding="utf-8",
    )
    sk = pdir / "skills" / "legacy-skill"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "---\nname: legacy-skill\ndescription: legacy demo\n---\n\nBody.\n", encoding="utf-8"
    )
    np = pi.normalize_plugin_dir(pdir)
    assert np.name == "旧版包" and np.category == "效率工具"
    assert np.connection == "dingtalk"
    assert np.mcp[0].transport == "streamable_http"


def test_market_meta_seed_override_and_install(db_session):
    # Seed applies without any override
    meta = ps.resolve_market_meta(db_session, "automation")
    assert meta["display_name"] == "定时任务管理"
    # Admin override wins over the seed and flows into the market list
    ps.set_market_meta(db_session, "automation", display_name="自动化任务", category="效率")
    meta = ps.resolve_market_meta(db_session, "automation")
    assert meta["display_name"] == "自动化任务" and meta["category"] == "效率"
    items = {
        it["slug"]: it
        for it in ps.list_plugins(db_session, owner_user_id=None, include_disabled=True)
    }
    assert items["automation"]["name"] == "自动化任务"
    # Install picks up the effective metadata for the installed record
    ps.install_plugin(db_session, "automation", owner_user_id=None)
    row = (
        db_session.query(InstalledPlugin)
        .filter(InstalledPlugin.install_id == "automation@global")
        .first()
    )
    assert row.name == "自动化任务"
    # Clearing the override falls back to the seed
    ps.set_market_meta(db_session, "automation", display_name="", category="")
    assert ps.resolve_market_meta(db_session, "automation")["display_name"] == "定时任务管理"


def test_market_meta_rejects_unknown_slug(db_session):
    with pytest.raises(Exception):
        ps.set_market_meta(db_session, "no-such-plugin", display_name="x")


def test_installed_meta_owner_guard(tmp_path, db_session):
    pdir = _make_standard_plugin(tmp_path)
    res = ps.import_plugin(db_session, pdir, owner_user_id=OWNER, secrets={"api_key": "k"})
    install_id = res["install_id"]
    out = ps.set_installed_plugin_meta(
        db_session, install_id, owner_user_id=OWNER, display_name="我的工具箱", category="效率"
    )
    assert out["name"] == "我的工具箱" and out["category"] == "效率"
    with pytest.raises(BadRequestError):
        ps.set_installed_plugin_meta(
            db_session, install_id, owner_user_id="someone_else", display_name="劫持"
        )


def test_market_meta_icon_validation(db_session):
    # Library path and uploaded data-URI are accepted
    ps.set_market_meta(db_session, "automation", icon="/home/mcp/internet.svg")
    assert ps.resolve_market_meta(db_session, "automation")["icon"] == "/home/mcp/internet.svg"
    ps.set_market_meta(db_session, "automation", icon="data:image/svg+xml;base64,PHN2Zy8+")
    assert ps.resolve_market_meta(db_session, "automation")["icon"].startswith("data:image/")
    # Non-image data URIs and arbitrary text are rejected
    with pytest.raises(BadRequestError):
        ps.set_market_meta(db_session, "automation", icon="data:text/html;base64,eA==")
    with pytest.raises(BadRequestError):
        ps.set_market_meta(db_session, "automation", icon="not-an-icon")
    # Oversized data URIs are rejected
    with pytest.raises(BadRequestError):
        ps.set_market_meta(db_session, "automation", icon="data:image/png;base64," + "A" * 300_000)
    # Empty clears the override
    ps.set_market_meta(db_session, "automation", icon="")
    assert "icon" not in ps.resolve_market_meta(db_session, "automation")


def test_capability_logo_tool_ownership(tmp_path, db_session):
    """Public/private connectors and installed plugins expose only tool names."""
    from core.config.catalog_runtime import _public_db_mcp_items
    from api.routes.v1.catalog import _load_owned_capability_items

    ps.import_plugin_from_zip(
        db_session, _zip_cc_plugin(tmp_path), owner_user_id=None, secrets={"api_token": "test"}
    )
    server = (
        db_session.query(AdminMcpServer)
        .filter(AdminMcpServer.source_plugin == "hello-toolkit")
        .first()
    )
    server.tools_json = [
        {"name": "logo_test_tool", "inputSchema": {"secret": "do-not-expose"}},
        {"description": "invalid"},
    ]
    db_session.flush()
    plugin = next(
        p for p in ps.list_installed(db_session, owner_user_id=None) if p["slug"] == "hello-toolkit"
    )
    assert plugin["tools"] == ["logo_test_tool"]
    public = next(
        p
        for p in _public_db_mcp_items(db_session, include_runtime_details=False)
        if p["id"] == server.server_id
    )
    assert public["tools"] == ["logo_test_tool"]
    server.owner_user_id = OWNER
    db_session.flush()
    _, private = _load_owned_capability_items(db_session, OWNER)
    assert next(p for p in private if p["id"] == server.server_id)["tools"] == ["logo_test_tool"]
    _, others = _load_owned_capability_items(db_session, "another-user")
    assert server.server_id not in {p["id"] for p in others}


def test_database_connector_inherits_child_tool_names(db_session, monkeypatch):
    from core.config import catalog_runtime as runtime

    monkeypatch.setattr(runtime, "_database_query_capability_available", lambda: True)
    row = AdminMcpServer(
        server_id="query_database",
        display_name="Database",
        transport="streamable_http",
        tools_json=[{"name": "query_sql"}],
    )
    db_session.add(row)
    db_session.flush()
    items = runtime._public_db_mcp_items(db_session, include_runtime_details=False)
    assert next(item for item in items if item["id"] == "database_query")["tools"] == ["query_sql"]
    assert not any(item["id"] == "query_database" for item in items)
