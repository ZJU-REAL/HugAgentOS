"""Regression tests for CE-only runtime seams."""

import inspect
import json
import re
from pathlib import Path
from types import SimpleNamespace

from api.routes.v1.auth import router as auth_router
from api.routes.v1.users import router as users_router
from cli import DEFAULT_LOCAL_CONTEXT_LENGTH, build_parser, configure_model
from core.db import model_repository


def test_ce_has_no_commercial_license_package():
    import importlib.util

    assert importlib.util.find_spec("core.licensing") is None


def test_ce_has_no_enterprise_modules_or_model_exports():
    import importlib.util

    import core.db.models as models

    for module_name in (
        "edition_ee",
        "core.kb.dify_kb",
        "core.auth.team_permissions",
        "core.services.team_service",
        "core.db.repository.team",
    ):
        assert importlib.util.find_spec(module_name) is None
    for model_name in (
        "Team",
        "TeamMember",
        "TeamFolder",
        "Role",
        "RoleAssignment",
        "InviteCode",
        "MarketplaceVisibilityGrant",
        "ChatSessionUserState",
    ):
        assert not hasattr(models, model_name)


def test_ce_runtime_sources_have_no_commercial_symbols():
    backend = Path(__file__).resolve().parents[1]
    pattern = re.compile(
        r"\b(?:TeamMember|TeamFolder|team_id|team_folder_id|"
        r"linked_team_folder_id|share_scope|grant_team_ids|team_read|team_edit|"
        r"list_team_files|stage_team_file|resolve_team_file_permission|"
        r"require_team_file_permission|team_cache_dir|team_folders)\b|"
        r"/v1/(?:me/teams|my-teams|teams)(?:/|\b)|\bkind=team\b"
    )
    hits = []
    for relative_root in ("api", "core", "mcp_servers", "orchestration"):
        for path in (backend / relative_root).rglob("*.py"):
            for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    hits.append(f"{path.relative_to(backend)}:{line_no}")
    assert hits == []


def test_ce_openapi_and_tables_have_no_organization_contracts():
    from api.app import app
    from core.db.engine import Base

    openapi = app.openapi()
    paths = set(openapi["paths"])
    assert "/v1/chats/{chat_id}/share" not in paths
    assert not any(
        path == prefix or path.startswith(prefix + "/")
        for path in paths
        for prefix in ("/v1/me/teams", "/v1/my-teams", "/v1/teams")
    )
    openapi_text = json.dumps(openapi, ensure_ascii=False).lower()
    for term in (
        "team_id",
        "team_folder_id",
        "share_scope",
        "team_read",
        "team_edit",
        "团队",
    ):
        assert term not in openapi_text
    agent_properties = openapi["components"]["schemas"]["AgentCreateRequest"]["properties"]
    assert "team_id" not in agent_properties
    kb_properties = openapi["components"]["schemas"]["CreateKBSpaceRequest"]["properties"]
    assert "grant_team_ids" not in kb_properties
    assert "chat_session_user_states" not in Base.metadata.tables
    forbidden_columns = {
        "artifacts": {"team_id", "team_folder_id"},
        "chat_sessions": {"share_scope"},
        "marketplace_listing_states": {"visibility"},
        "projects": {"team_id", "linked_team_folder_id"},
        "user_agents": {"team_id"},
    }
    for table_name, names in forbidden_columns.items():
        assert not (set(Base.metadata.tables[table_name].columns.keys()) & names)


def test_ce_auth_router_keeps_local_session_contract():
    paths = {route.path for route in auth_router.routes}
    assert {
        "/v1/auth/ticket/exchange",
        "/v1/auth/session/check",
        "/v1/auth/logout",
    } <= paths


def test_ce_user_router_exposes_self_service_password_change():
    paths = {route.path for route in users_router.routes}
    assert "/v1/me/password" in paths
    assert "/v1/me/onboarding/complete" in paths


def test_dingtalk_status_env_does_not_import_ee_sandbox_module(monkeypatch, tmp_path):
    from core.sandbox import _common
    from core.services.dingtalk_service import _dws_env

    monkeypatch.setattr(_common, "dws_home_dir", lambda _user_id: tmp_path / "dws-home")
    monkeypatch.setattr(
        _common,
        "dws_extra_envs",
        lambda: {"DWS_TRUSTED_DOMAINS": "*.dingtalk.com"},
    )

    env = _dws_env("ce-user")

    assert env["HOME"] == str(tmp_path / "dws-home")
    assert env["DWS_TRUSTED_DOMAINS"] == "*.dingtalk.com"


def test_ce_ignores_stale_database_query_builtin(db_session, monkeypatch):
    from core.config.catalog_runtime import get_runtime_catalog, invalidate_runtime_catalog_cache
    from core.db.models import AdminMcpServer
    from core.services import mcp_service
    from core.services.mcp_service import McpServerConfigService
    from mcp_servers._ports import PORTS

    assert "query_database" not in PORTS
    db_session.add_all(
        [
            AdminMcpServer(
                server_id="query_database",
                display_name="Legacy database query",
                transport="streamable_http",
                url="http://mcp:9101/mcp/",
                is_enabled=True,
            ),
            AdminMcpServer(
                server_id="custom_remote_mcp",
                display_name="Custom remote MCP",
                transport="streamable_http",
                url="https://mcp.example.com/mcp/",
                is_enabled=True,
            ),
        ]
    )
    db_session.commit()
    monkeypatch.setattr(mcp_service, "SessionLocal", lambda: db_session)

    servers = McpServerConfigService().get_all_servers(enabled_only=True)

    assert "query_database" not in servers
    assert "custom_remote_mcp" in servers

    invalidate_runtime_catalog_cache()
    catalog = get_runtime_catalog(db_session)
    catalog_mcp_ids = {item["id"] for item in catalog["mcp"]}
    assert "database_query" not in catalog_mcp_ids
    assert "query_database" not in catalog_mcp_ids
    assert "custom_remote_mcp" in catalog_mcp_ids

    assert mcp_service.prune_removed_builtin_mcp_servers(db_session) == ["query_database"]
    assert db_session.get(AdminMcpServer, "query_database") is None
    assert db_session.get(AdminMcpServer, "custom_remote_mcp") is not None


def test_ce_local_launcher_covers_default_plugin_mcp_servers():
    from cli import _DEFAULT_PLUGINS
    from mcp_servers._launcher import PORTS as LAUNCHER_PORTS
    from mcp_servers._ports import PORTS, package_name

    assert _DEFAULT_PLUGINS == ["automation", "skill-manager", "sites"]
    assert {
        "automation_task": 9108,
        "skill_manager": 9112,
        "site_publish": 9113,
    }.items() <= PORTS.items()
    assert package_name("site_publish") == "site_publish_mcp"
    assert LAUNCHER_PORTS["site_publish_mcp"] == 9113


def test_ce_fresh_database_bootstraps_default_plugins(db_session):
    from core.db.models import AdminMcpServer, ContentBlock, InstalledPlugin
    from core.services import plugin_service

    assert plugin_service.ensure_default_plugins_bootstrapped(db_session) is True
    installs = db_session.query(InstalledPlugin).filter(InstalledPlugin.owner_user_id.is_(None))
    assert {row.slug for row in installs.all()} == {
        "automation",
        "skill-manager",
        "sites",
    }
    assert {
        row.source_plugin
        for row in db_session.query(AdminMcpServer)
        .filter(AdminMcpServer.source_plugin.is_not(None))
        .all()
    } == {"automation", "skill-manager", "sites"}
    marker = db_session.get(ContentBlock, plugin_service.DEFAULT_BOOTSTRAP_MARKER_ID)
    assert marker.payload["plugins"] == ["automation", "skill-manager", "sites"]
    assert plugin_service.ensure_default_plugins_bootstrapped(db_session) is False


def test_ce_launcher_and_catalog_cover_core_mcp_servers():
    from core.services.mcp_service import BUILTIN_MCP_SERVERS
    from mcp_servers._launcher import PORTS as LAUNCHER_PORTS
    from mcp_servers._ports import PORTS, package_name

    expected = {
        "retrieve_dataset_content": 9100,
        "internet_search": 9102,
        "generate_chart_tool": 9104,
        "web_fetch": 9106,
        "batch_runner": 9107,
    }
    assert expected.items() <= PORTS.items()
    assert {
        package_name(server_id): port for server_id, port in expected.items()
    }.items() <= LAUNCHER_PORTS.items()

    specs = {str(item["server_id"]): item for item in BUILTIN_MCP_SERVERS}
    assert expected.keys() <= specs.keys()
    assert all(specs[server_id]["is_enabled"] is True for server_id in expected)


def test_ce_fresh_database_seeds_core_mcp_servers_enabled(db_session):
    from core.db.models import AdminMcpServer
    from core.services.mcp_service import seed_builtin_mcp_servers_if_empty

    expected = {
        "retrieve_dataset_content",
        "internet_search",
        "generate_chart_tool",
        "web_fetch",
        "batch_runner",
    }
    seeded = set(seed_builtin_mcp_servers_if_empty(db_session))
    rows = {
        row.server_id: row
        for row in db_session.query(AdminMcpServer)
        .filter(AdminMcpServer.server_id.in_(expected))
        .all()
    }

    assert expected <= seeded
    assert expected == rows.keys()
    assert all(row.is_enabled is True for row in rows.values())


def test_onboard_cli_has_safe_context_window_default():
    args = build_parser().parse_args(["onboard"])
    assert args.model_context_length == DEFAULT_LOCAL_CONTEXT_LENGTH
    assert args.host == "127.0.0.1"


def test_serve_cli_accepts_explicit_public_bind():
    args = build_parser().parse_args(["serve", "--host", "0.0.0.0"])
    assert args.host == "0.0.0.0"


def test_configure_chat_model_persists_context_window(monkeypatch):
    from core.db import engine
    from core.services.model_config import ModelConfigService

    class FakeDb:
        closed = False

        def close(self):
            self.closed = True

    db = FakeDb()
    captured = {}
    invalidated = []

    def fake_create_provider(_db, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(provider_id="provider-test")

    monkeypatch.setattr(engine, "SessionLocal", lambda: db)
    monkeypatch.setattr(model_repository, "create_provider", fake_create_provider)
    monkeypatch.setattr(model_repository, "assign_role", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ModelConfigService,
        "get_instance",
        classmethod(lambda cls: SimpleNamespace(invalidate_cache=lambda: invalidated.append(True))),
    )

    configure_model(
        "http://model.example/v1",
        "test-key",
        "test-model",
        test=False,
        context_length=65536,
    )

    assert captured["extra_config"] == {"context_length": 65536}
    assert db.closed is True
    assert invalidated == [True]


def test_ce_bundles_compilable_default_ontology_pack():
    from core.ontology.schemas import OntologyPackDocument
    from core.ontology.validator import build_runtime_payload

    pack_path = (
        Path(__file__).resolve().parents[1]
        / "configs"
        / "ontology_packs"
        / "enterprise_risk_v1.json"
    )
    document = OntologyPackDocument.model_validate(
        json.loads(pack_path.read_text(encoding="utf-8"))
    )

    runtime = build_runtime_payload([document], "请生成企业风险画像")

    assert runtime["enabled"] is True
    assert runtime["packs"]
    assert runtime["asset_tags"] == {"tool": {}, "skill": {}, "subagent": {}}


def test_ce_schema_keeps_ontology_control_plane_tables():
    import core.db.models  # noqa: F401  register all model metadata
    from core.db.engine import Base

    ontology_tables = {
        "ontology_packs",
        "ontology_pack_versions",
        "ontology_enforcement_events",
        "ontology_review_runs",
        "ontology_drafts",
    }

    assert ontology_tables <= set(Base.metadata.tables)


def test_ce_schema_can_create_without_enterprise_foreign_keys(db_session):
    """The fixture's create_all() is the assertion; keep one explicit DB touch."""
    assert db_session.execute(__import__("sqlalchemy").text("SELECT 1")).scalar_one() == 1


def test_ce_site_contract_has_no_organization_scope():
    from api.app import app
    from core.db.engine import Base

    schemas = app.openapi()["components"]["schemas"]
    assert "team_id" not in schemas["UpdateSiteRequest"]["properties"]
    assert "team_id" not in schemas["PublishBody"]["properties"]
    assert "team_id" not in Base.metadata.tables["sites"].columns


def test_ce_site_publish_tool_has_no_organization_parameter():
    from mcp_servers.site_publish_mcp.server import publish_site

    assert "team_id" not in inspect.signature(publish_site).parameters
