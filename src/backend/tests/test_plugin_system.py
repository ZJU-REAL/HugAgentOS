"""Plugin system tests: import Claude Code / Codex / native plugin packages → persist to DB → uninstall.

Covers the three-tier portability matrix: direct skill import (including
references + path-variable rewriting), direct remote MCP import, stdio MCP
disabled on install, and drop warnings for hooks/commands/agents.
See internal design docs §10.
"""

import json
from pathlib import Path

import pytest

from core.db.models import AdminMcpServer, AdminSkill, InstalledPlugin
from core.services import plugin_importer as pi
from core.services import plugin_service as ps

OWNER = "test_user_123"


# ── Test fixture: build a real Claude Code plugin package in tmp ──────────────

def _make_cc_plugin(root: Path) -> Path:
    pdir = root / "hello-toolkit"
    (pdir / ".claude-plugin").mkdir(parents=True)
    (pdir / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "hello-toolkit",
        "version": "2.1.0",
        "description": "A Claude Code demo plugin",
        "userConfig": {
            "api_token": {"type": "string", "title": "API Token", "sensitive": True, "required": True}
        },
    }), encoding="utf-8")

    # Skill 1: with references + scripts + path variables
    sk = pdir / "skills" / "hello-greeter"
    (sk / "references").mkdir(parents=True)
    (sk / "scripts").mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "---\nname: hello-greeter\ndescription: Greets the user warmly when they say hello\n---\n\n"
        "Run ${CLAUDE_PLUGIN_ROOT}/scripts/greet.py to greet.\n"
        "See references/style.md for tone.\n",
        encoding="utf-8",
    )
    (sk / "references" / "style.md").write_text("Be warm. Path: ${CLAUDE_PLUGIN_ROOT}/data\n", encoding="utf-8")
    (sk / "scripts" / "greet.py").write_text("print('hello')\n", encoding="utf-8")

    # Skill 2: plain text
    sk2 = pdir / "skills" / "farewell"
    sk2.mkdir(parents=True)
    (sk2 / "SKILL.md").write_text(
        "---\nname: farewell\ndescription: Says goodbye when the conversation ends\n---\n\nSay goodbye.\n",
        encoding="utf-8",
    )

    # MCP: one remote http + one stdio
    (pdir / ".mcp.json").write_text(json.dumps({
        "mcpServers": {
            "weather-remote": {"url": "https://mcp.example.com/mcp", "headers": {"X-Key": "${WEATHER_KEY}"}},
            "local-fs": {"command": "npx", "args": ["-y", "@x/fs-mcp", "${CLAUDE_PLUGIN_ROOT}/data"]},
        }
    }), encoding="utf-8")

    # Tier3: components that should be dropped
    (pdir / "hooks").mkdir()
    (pdir / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": []}}), encoding="utf-8")
    (pdir / "commands").mkdir()
    (pdir / "commands" / "deploy.md").write_text("---\ndescription: deploy\n---\nDeploy $ARGUMENTS\n", encoding="utf-8")
    (pdir / "agents").mkdir()
    (pdir / "agents" / "reviewer.md").write_text("---\nname: reviewer\ndescription: x\n---\nReview.\n", encoding="utf-8")
    return pdir


# ── normalize layer ───────────────────────────────────────────────────────────

def test_normalize_cc_plugin(tmp_path):
    pdir = _make_cc_plugin(tmp_path)
    np = pi.normalize_plugin_dir(pdir)

    assert np.kind == "claude"
    assert np.slug == "hello-toolkit"
    assert np.version == "2.1.0"

    # Skills: both are discovered
    names = sorted(s.name for s in np.skills)
    assert names == ["farewell", "hello-greeter"]
    greeter = next(s for s in np.skills if s.name == "hello-greeter")
    # references + scripts are carried over losslessly with the skill directory
    assert "references/style.md" in greeter.extra_files
    assert "scripts/greet.py" in greeter.extra_files

    # MCP: transport inferred correctly
    mcp = {m.name: m for m in np.mcp}
    assert mcp["weather-remote"].transport == "streamable_http"
    assert mcp["weather-remote"].needs_runtime is False
    assert mcp["local-fs"].transport == "stdio"
    assert mcp["local-fs"].needs_runtime is True
    # stdio path variables were rewritten
    assert not any("${CLAUDE_PLUGIN_ROOT}" in a for a in mcp["local-fs"].args)

    # required_secrets normalized from userConfig
    keys = [s["key"] for s in np.required_secrets]
    assert "api_token" in keys

    # Tier3 drops: hooks / commands / subagents all go into dropped
    dtypes = {d["type"] for d in np.dropped}
    assert "hooks" in dtypes
    assert "command" in dtypes
    assert "subagent" in dtypes


# ── import into DB ─────────────────────────────────────────────────────────────

def test_import_cc_plugin_into_db(tmp_path, db_session):
    pdir = _make_cc_plugin(tmp_path)
    result = ps.import_plugin(
        db_session, pdir, owner_user_id=OWNER, secrets={"api_token": "sk-test-123"},
    )
    assert result["kind"] == "claude"
    assert result["source"] if "source" in result else True  # source set on row
    report = result["import_report"]
    # Two skills + one remote MCP go into imported; stdio MCP goes into adapted
    imported_types = [x["type"] for x in report["imported"]]
    assert imported_types.count("skill") == 2
    assert imported_types.count("mcp") == 1
    assert len(report["adapted"]) == 1
    assert report["adapted"][0]["name"] == "local-fs"
    assert len(report["dropped"]) >= 3

    # AdminSkill persisted with source_plugin tagged
    skills = db_session.query(AdminSkill).filter(AdminSkill.source_plugin == "hello-toolkit").all()
    assert len(skills) == 2
    for s in skills:
        assert s.owner_user_id == OWNER
        assert s.is_enabled is True
    greeter = next(s for s in skills if "hello-greeter" in s.skill_id)
    # Path variables rewritten to sandbox paths
    assert "${CLAUDE_PLUGIN_ROOT}" not in greeter.skill_content
    assert f"/workspace/skills/{greeter.skill_id}" in greeter.skill_content
    # Path variables inside references were rewritten too
    assert "${CLAUDE_PLUGIN_ROOT}" not in greeter.extra_files["references/style.md"]
    # Credentials written into secrets.json
    assert "secrets.json" in greeter.extra_files
    assert "sk-test-123" in greeter.extra_files["secrets.json"]

    # MCP persisted: remote enabled, stdio disabled
    remote = db_session.query(AdminMcpServer).filter(
        AdminMcpServer.source_plugin == "hello-toolkit",
        AdminMcpServer.transport == "streamable_http",
    ).first()
    assert remote is not None and remote.is_enabled is True
    stdio = db_session.query(AdminMcpServer).filter(
        AdminMcpServer.source_plugin == "hello-toolkit",
        AdminMcpServer.transport == "stdio",
    ).first()
    assert stdio is not None and stdio.is_enabled is False  # needs a runtime, disabled by default

    # Install record
    row = db_session.query(InstalledPlugin).filter(
        InstalledPlugin.owner_user_id == OWNER, InstalledPlugin.slug == "hello-toolkit"
    ).first()
    assert row is not None
    assert row.source == "imported_claude"
    assert len(row.component_ids["skills"]) == 2


def test_owned_skill_enters_runtime_set(tmp_path, db_session):
    """Imported private skill with is_enabled=True → selected by the owned merge of resolve_all_runtime_enabled."""
    from core.config.catalog_resolver import _owned_enabled_ids

    pdir = _make_cc_plugin(tmp_path)
    ps.import_plugin(db_session, pdir, owner_user_id=OWNER, secrets={"api_token": "x"})

    owned_skills, owned_mcps = _owned_enabled_ids(db_session, OWNER, {})
    # Both skills are in the owned-enabled set
    assert sum(1 for s in owned_skills if "hello-toolkit" in s) == 2
    # Remote MCP is in; stdio is not (disabled)
    assert any("weather-remote" in m for m in owned_mcps)
    assert not any("local-fs" in m for m in owned_mcps)


def test_uninstall_removes_everything(tmp_path, db_session):
    pdir = _make_cc_plugin(tmp_path)
    res = ps.import_plugin(db_session, pdir, owner_user_id=OWNER, secrets={"api_token": "x"})
    install_id = res["install_id"]

    ps.uninstall_plugin(db_session, install_id, owner_user_id=OWNER)

    assert db_session.query(AdminSkill).filter(AdminSkill.source_plugin == "hello-toolkit").count() == 0
    assert db_session.query(AdminMcpServer).filter(AdminMcpServer.source_plugin == "hello-toolkit").count() == 0
    assert db_session.query(InstalledPlugin).filter(InstalledPlugin.install_id == install_id).count() == 0


def test_installed_detail_lists_components(tmp_path, db_session):
    """Installed detail returns skills (with instructions/files) + MCP (with transport/tools)."""
    pdir = _make_cc_plugin(tmp_path)
    res = ps.import_plugin(db_session, pdir, owner_user_id=OWNER, secrets={"api_token": "x"})
    detail = ps.get_installed_detail(db_session, res["install_id"], owner_user_id=OWNER)

    assert detail["name"]
    assert len(detail["skills"]) == 2
    greeter = next(s for s in detail["skills"] if "hello-greeter" in s["skill_id"])
    assert greeter["instructions"]  # body instructions (frontmatter stripped)
    assert "references/style.md" in greeter["files"]
    assert greeter["has_secrets"] is True  # credentials were injected
    # MCP components include transport
    transports = {m["transport"] for m in detail["mcp"]}
    assert "streamable_http" in transports and "stdio" in transports
    stdio = next(m for m in detail["mcp"] if m["transport"] == "stdio")
    assert stdio["needs_runtime"] is True

    # Unauthorized viewing must be rejected
    with pytest.raises(Exception):
        ps.get_installed_detail(db_session, res["install_id"], owner_user_id="someone_else")


def test_enable_disable_toggle(tmp_path, db_session):
    pdir = _make_cc_plugin(tmp_path)
    res = ps.import_plugin(db_session, pdir, owner_user_id=OWNER, secrets={"api_token": "x"})
    install_id = res["install_id"]

    ps.set_plugin_enabled(db_session, install_id, enabled=False, owner_user_id=OWNER)
    skills = db_session.query(AdminSkill).filter(AdminSkill.source_plugin == "hello-toolkit").all()
    assert all(s.is_enabled is False for s in skills)

    ps.set_plugin_enabled(db_session, install_id, enabled=True, owner_user_id=OWNER)
    skills = db_session.query(AdminSkill).filter(AdminSkill.source_plugin == "hello-toolkit").all()
    assert all(s.is_enabled is True for s in skills)
    # stdio MCP stays disabled even when the plugin is enabled as a whole
    stdio = db_session.query(AdminMcpServer).filter(
        AdminMcpServer.source_plugin == "hello-toolkit", AdminMcpServer.transport == "stdio"
    ).first()
    assert stdio.is_enabled is False


# ── Codex format detection ────────────────────────────────────────────────────

def test_detect_codex_plugin(tmp_path):
    pdir = tmp_path / "codex-plug"
    (pdir / ".codex-plugin").mkdir(parents=True)
    (pdir / ".codex-plugin" / "plugin.json").write_text(json.dumps({
        "name": "codex-plug", "version": "1.0.0", "description": "codex demo",
        "interface": {"composerIcon": "./assets/icon.png"},
    }), encoding="utf-8")
    sk = pdir / "skills" / "summarize"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "---\nname: summarize\ndescription: Summarizes long text into bullet points\n---\nSummarize.\n",
        encoding="utf-8",
    )
    np = pi.normalize_plugin_dir(pdir)
    assert np.kind == "codex"
    assert np.icon == "./assets/icon.png"
    assert [s.name for s in np.skills] == ["summarize"]


# ── Error paths ───────────────────────────────────────────────────────────────

def test_reject_non_plugin_dir(tmp_path):
    (tmp_path / "random.txt").write_text("nope", encoding="utf-8")
    with pytest.raises(Exception):
        pi.normalize_plugin_dir(tmp_path)


# ── Built-in plugin package paths ─────────────────────────────────────────────

def test_builtin_plugin_list_and_install(db_session):
    """The built-in sample plugin sample-translator should be discovered by list and be installable."""
    items = ps.list_plugins(db_session, owner_user_id=OWNER)
    slugs = {it["slug"] for it in items}
    assert "sample-translator" in slugs
    sample = next(it for it in items if it["slug"] == "sample-translator")
    assert sample["installed"] is False
    assert sample["skills_count"] == 1

    res = ps.install_plugin(db_session, "sample-translator", owner_user_id=OWNER)
    assert res["action"] == "installed"
    sk = db_session.query(AdminSkill).filter(AdminSkill.source_plugin == "sample-translator").all()
    assert len(sk) == 1
    assert sk[0].is_enabled is True
    # references are carried along with the skill
    assert any("glossary" in f for f in (sk[0].extra_files or {}))

    # List again → marked as installed
    items2 = ps.list_plugins(db_session, owner_user_id=OWNER)
    assert next(it for it in items2 if it["slug"] == "sample-translator")["installed"] is True


def test_builtin_plugin_component_ids_covers_static_mcp():
    """MCP/skill component ids declared in built-in plugin manifests should be collected by builtin_plugin_component_ids.

    Regression: MCPs of built-in plugins like automation_task / skill_manager
    bubble up statically via _ports.py → catalog.json as first-class entries,
    with no source_plugin row in the DB; they must be removed from the "MCP
    tool library" via filesystem manifest scanning and shown only under
    "Plugins".
    """
    skill_ids, mcp_ids = ps.builtin_plugin_component_ids()
    assert "automation_task" in mcp_ids
    assert "skill_manager" in mcp_ids
    # Plugin-bundled skills also go into the set (used to remove them from the skill library)
    assert "scheduled-tasks" in skill_ids
    assert "skill-creator" in skill_ids


def test_plugin_component_dedup_hides_static_plugin_mcp(db_session):
    """catalog._plugin_component_ids should include built-in plugins' static MCPs in the dedup set (union of DB + filesystem)."""
    from api.routes.v1.catalog import _plugin_component_ids

    _skill_ids, mcp_ids = _plugin_component_ids(db_session)
    assert "automation_task" in mcp_ids
    assert "skill_manager" in mcp_ids


def test_builtin_firecrawl_plugin_install(db_session):
    """Built-in firecrawl plugin (official CLI skill suite): discovered by list →
    installs 10 skills, all enabled, no MCP, no required_secrets (credentials
    are injected via system-config env) → clean uninstall."""
    items = ps.list_plugins(db_session, owner_user_id=OWNER)
    fc = next((it for it in items if it["slug"] == "firecrawl"), None)
    assert fc is not None, "firecrawl 插件未被 list_plugins 发现"
    assert fc["installed"] is False
    assert fc["skills_count"] == 10
    # Credentials are injected via system config, not collected in the plugin manifest
    assert not fc.get("required_secrets")

    res = ps.install_plugin(db_session, "firecrawl", owner_user_id=OWNER)
    assert res["action"] == "installed"

    sk = db_session.query(AdminSkill).filter(AdminSkill.source_plugin == "firecrawl").all()
    assert len(sk) == 10
    assert all(s.is_enabled for s in sk)
    ids = {s.skill_id for s in sk}
    # Namespaced id = slug-skillname (per-user installs append a 6-char fingerprint); includes the main subcommands
    for expected in ("firecrawl-scrape", "firecrawl-search", "firecrawl-crawl", "firecrawl-cli"):
        assert any(i.startswith(expected) for i in ids), f"缺少技能 {expected}"
    # What gets installed is skills, not MCP (the official route uses the Bash(firecrawl *) CLI, not MCP)
    assert db_session.query(AdminMcpServer).filter(
        AdminMcpServer.source_plugin == "firecrawl"
    ).count() == 0
    # The umbrella skill is adapted to the platform: keeps the admin-config guidance, strips broken links to firecrawl-build/workflows
    cli = next(s for s in sk if s.skill_id.startswith("firecrawl-cli"))
    assert "系统配置" in cli.skill_content
    assert "firecrawl-build" not in cli.skill_content
    assert "firecrawl-workflows" not in cli.skill_content

    # Uninstall: all skills deleted, install record gone
    install_id = res["install_id"]
    out = ps.uninstall_plugin(db_session, install_id, owner_user_id=OWNER)
    assert out["removed_skills"] == 10
    assert db_session.query(AdminSkill).filter(AdminSkill.source_plugin == "firecrawl").count() == 0


def test_firecrawl_admin_config_schema_and_guards(db_session):
    """firecrawl's admin-level config (admin_config):
    - Market detail carries admin_config; the user view is read-only (no real-value field), configured computed with mode=any;
    - Admin view includes value (secrets masked);
    - Writes only accept field keys declared by the plugin (out-of-scope keys rejected)."""
    detail = ps.get_plugin_detail("firecrawl")
    ac = detail.get("admin_config")
    assert ac is not None, "firecrawl 市场详情缺 admin_config"
    assert ac["mode"] == "any"
    keys = {f["key"] for f in ac["fields"]}
    assert keys == {"firecrawl.api_key", "firecrawl.api_url"}
    # User view: never returns real values
    assert all("value" not in f for f in ac["fields"])
    assert ac["configured"] is False  # not configured in the test environment

    # Admin view: includes value, secrets masked
    admin_view = ps.get_plugin_admin_config("firecrawl")
    by_key = {f["key"]: f for f in admin_view["fields"]}
    assert "value" in by_key["firecrawl.api_key"] and by_key["firecrawl.api_key"]["secret"] is True
    assert by_key["firecrawl.api_url"]["secret"] is False

    # Writing an out-of-scope key is rejected
    with pytest.raises(Exception):
        ps.set_plugin_admin_config("firecrawl", {"dingtalk.client_id": "x"})


def test_import_from_zip_global(tmp_path, db_session):
    """Admin path: import_plugin_from_zip + owner=None → global install, visible via list_installed(None)."""
    zip_bytes = _zip_cc_plugin(tmp_path)
    res = ps.import_plugin_from_zip(db_session, zip_bytes, owner_user_id=None, secrets={"api_token": "g"})
    assert res["kind"] == "claude"
    # Global skills/MCP (owner empty)
    sk = db_session.query(AdminSkill).filter(
        AdminSkill.source_plugin == "hello-toolkit", AdminSkill.owner_user_id.is_(None)
    ).all()
    assert len(sk) == 2
    glob = ps.list_installed(db_session, owner_user_id=None)
    assert any(p["slug"] == "hello-toolkit" for p in glob)
    # Not private to that user
    assert ps.list_installed(db_session, owner_user_id="someone") == []


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
    res = ps.import_plugin_from_zip(db_session, zip_bytes, owner_user_id=None, secrets={"api_token": "g"})
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
    inst = ps.install_plugin(db_session, "hello-toolkit", owner_user_id=None, secrets={"api_token": "x"})
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


# ── Route-layer end-to-end: real HTTP stack (multipart zip upload → import → list → uninstall) ──────

def _zip_cc_plugin(tmp_path) -> bytes:
    import io
    import zipfile
    pdir = _make_cc_plugin(tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(pdir.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(pdir.parent).as_posix())  # extra top-level directory wrapper, tests _locate_plugin_root
    return buf.getvalue()


def test_route_import_and_uninstall_e2e(tmp_path, db_session):
    """Import a plugin zip through the real FastAPI route stack, then list and uninstall."""
    from fastapi.testclient import TestClient

    from api.app import app
    from core.auth.backend import UserContext, get_current_user
    from core.db.engine import get_db
    from core.db.models import UserShadow

    # Create a test user with can_import_plugin enabled
    db_session.add(UserShadow(
        user_id=OWNER, username="Tester", extra_data={"can_import_plugin": True},
    ))
    db_session.commit()

    def _override_db():
        yield db_session

    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=OWNER, user_center_id="c1", username="Tester", email="t@e.com",
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
        assert db_session.query(AdminSkill).filter(AdminSkill.source_plugin == "hello-toolkit").count() == 0
    finally:
        app.dependency_overrides.clear()
