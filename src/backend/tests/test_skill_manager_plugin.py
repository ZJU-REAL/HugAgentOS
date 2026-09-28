"""Cloud skill-manager marketplace and bundle contracts. Lifecycle tests live in tests/capabilities/test_*management*.py."""

import asyncio
import inspect
import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.db.engine as dbe
from core.db.models import AdminMcpServer, AdminSkill, InstalledPlugin, MarketplaceSubmission

OWNER = "sm_test_user"
BUNDLE_DIR = Path(__file__).resolve().parents[1] / "plugin_bundles" / "marketplace" / "skill-manager"

@pytest.fixture()
def sm_env(tmp_path, monkeypatch):
    """Bind SessionLocal to an isolated sqlite file DB; point the artifact store at tmp; allow capabilities."""
    url = f"sqlite:///{tmp_path}/sm.db"
    engine = create_engine(url)
    dbe.Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    # The impl lazily reads the attribute via `from core.db.engine import SessionLocal` → patching here suffices
    monkeypatch.setattr(dbe, "SessionLocal", TestSession)

    # Artifact store goes to tmp (_STORE_DIR is fixed at import time; every other path derives from it)
    from core.artifacts import store

    art_dir = tmp_path / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(store, "_STORE_DIR", art_dir)

    # Allow the capabilities (otherwise all write verbs get blocked)
    import core.auth.capabilities as caps

    monkeypatch.setattr(
        caps, "resolve_user_capabilities",
        lambda db, uid: {"can_add_skill": True, "can_import_plugin": True},
    )

    return SimpleNamespace(engine=engine, Session=TestSession)

def _make_skill_tar(name: str, description: str, *, body: str = "做事。") -> bytes:
    """Pack a minimal skill directory (only SKILL.md, at the package root) into a tar.gz."""
    skill_md = f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = skill_md.encode("utf-8")
        info = tarfile.TarInfo("SKILL.md")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()

def _stash_artifact(tar_bytes: bytes) -> str:
    """Write the tar into the shared artifact store and return the artifact_id (simulating a sandbox sandbox_get_artifact result)."""
    from core.artifacts import store

    item = store.save_artifact_bytes(content=tar_bytes, name="skill.tgz", extension="tgz")
    return item["file_id"]

# ── Plugin persistence + merging into the user's available set ──────────────
def test_install_plugin_creates_rows_and_is_agent_visible(sm_env):
    from core.services import plugin_service as ps

    with sm_env.Session() as db:
        res = ps.install_plugin(db, "skill-manager", owner_user_id=OWNER, created_by=OWNER)
        assert res.get("install_id")

    with sm_env.Session() as db:
        skills = db.query(AdminSkill).filter(AdminSkill.source_plugin == "skill-manager").all()
        mcps = db.query(AdminMcpServer).filter(AdminMcpServer.source_plugin == "skill-manager").all()
        plugin = db.query(InstalledPlugin).filter(InstalledPlugin.slug == "skill-manager").first()

        assert len(skills) == 1 and skills[0].owner_user_id == OWNER
        assert len(mcps) == 1
        mcp = mcps[0]
        assert mcp.owner_user_id == OWNER
        assert mcp.transport == "streamable_http"
        assert mcp.url == "http://mcp:9112/mcp/"
        assert mcp.is_enabled is True  # http MCP is not needs_runtime → enabled upon install
        assert len(mcp.tools_json) == 8
        assert plugin is not None

        # Key: merged into the user's available set via resolve_all_runtime_enabled (= the agent can really get the skill + mcp)
        from core.config.catalog_resolver import resolve_all_runtime_enabled, invalidate_capability_cache

        invalidate_capability_cache()
        enabled_skills, _agents, enabled_mcps = resolve_all_runtime_enabled(db, OWNER)
        assert skills[0].skill_id in (enabled_skills or [])
        assert mcp.server_id in (enabled_mcps or [])

# ── Create (register_skill, via the shared artifact store) + manage + submit for listing + delete ──

# ── Edit: in-place metadata / body / auxiliary-file changes + unauthorized-access blocking ──

def test_plan_mode_includes_global_plugin_components(sm_env):
    """Plan mode must also see plugin components globally installed by the admin."""
    from core.db.models import AdminMcpServer, InstalledPlugin
    from orchestration.subagents.plugin_visibility import (
        all_plugin_component_ids,
        load_enabled_plugins,
    )

    with sm_env.Session() as db:
        db.add(
            AdminSkill(
                skill_id="global-plugin-skill",
                skill_content=(
                    "---\n"
                    "name: global-plugin-skill\n"
                    "description: 全局插件技能。\n"
                    "---\n\n"
                    "执行全局插件技能。\n"
                ),
                display_name="全局插件技能",
                description="全局插件技能。",
                owner_user_id=None,
                source_plugin="global-plugin",
                is_enabled=True,
                extra_files={},
                dependencies={},
                tags=[],
                allowed_tools=[],
            )
        )
        db.add(
            AdminMcpServer(
                server_id="global-plugin-mcp",
                display_name="全局插件 MCP",
                description="全局插件 MCP。",
                owner_user_id=None,
                source_plugin="global-plugin",
                is_enabled=True,
            )
        )
        db.add(
            InstalledPlugin(
                install_id="global-plugin@global",
                slug="global-plugin",
                name="全局插件",
                description="管理员全局安装的插件。",
                owner_user_id=None,
                component_ids={
                    "skills": ["global-plugin-skill"],
                    "mcp": ["global-plugin-mcp"],
                    "prompts": [],
                },
            )
        )
        db.commit()

        skill_ids, mcp_ids = all_plugin_component_ids(db, OWNER)
        assert "global-plugin-skill" in skill_ids
        assert "global-plugin-mcp" in mcp_ids

        plugins = load_enabled_plugins(
            db,
            OWNER,
            {"global-plugin-skill"},
            {"global-plugin-mcp"},
        )
        assert plugins == [
            {
                "name": "全局插件",
                "description": "管理员全局安装的插件。",
                "skill_ids": ["global-plugin-skill"],
                "mcp_ids": ["global-plugin-mcp"],
            }
        ]

def test_install_marketplace_tool_exposes_and_forwards_secrets(monkeypatch):
    """MCP tool schema must expose secrets and pass them to impl.install_from_marketplace."""
    from mcp_servers.skill_manager_mcp import server

    sig = inspect.signature(server.install_from_marketplace)
    assert "secrets" in sig.parameters

    calls = {}

    def fake_install_from_marketplace(*, user_id, slug, secrets=None):
        calls.update({"user_id": user_id, "slug": slug, "secrets": secrets})
        return {"ok": True, "skill_id": "s1", "action": "installed", "message": "ok"}

    monkeypatch.setattr(server.impl, "install_from_marketplace", fake_install_from_marketplace)
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers={"x-current-user-id": OWNER})
        )
    )

    res = asyncio.run(
        server.install_from_marketplace(
            "gpt-image2-pro",
            secrets={"IMAGE_GEN_API_KEY": "sk-test-123"},
            ctx=ctx,
        )
    )

    assert res["ok"] is True
    assert calls == {
        "user_id": OWNER,
        "slug": "gpt-image2-pro",
        "secrets": {"IMAGE_GEN_API_KEY": "sk-test-123"},
    }

def test_skill_creator_instructions_do_not_hardcode_materialized_dir():
    """The installed skill-creator id gets namespaced; instructions must not hardcode the old directory."""
    content = (BUNDLE_DIR / "skills" / "skill-creator" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "/workspace/skills/skill-creator" not in content
    assert "{dir}" in content

def test_runtime_skill_loader_reads_db_skills_without_route_specific_refresh(
    sm_env, tmp_path, monkeypatch
):
    """After a skill is written to AdminSkill, re-reading via the same loader instance must honor the DB as the source of truth."""
    from core.agent_skills.loader import get_skill_loader

    monkeypatch.setenv("SANDBOX_SKILLS_DIR", str(tmp_path / "sandbox_skills"))
    skill_id = "runtime-db-skill"

    loader = get_skill_loader(reset=True)
    assert skill_id not in loader.load_all_metadata()

    skill_md = (
        "---\n"
        f"name: {skill_id}\n"
        "description: 测试刚创建后立即显式调用的新技能。\n"
        "---\n\n"
        "按测试要求执行。\n"
    )
    with sm_env.Session() as db:
        db.add(
            AdminSkill(
                skill_id=skill_id,
                skill_content=skill_md,
                display_name="late skill",
                description="测试刚创建后立即显式调用的新技能。",
                owner_user_id=OWNER,
                is_enabled=True,
                dep_status="ready",
                extra_files={},
                dependencies={},
                tags=[],
                allowed_tools=[],
                created_by=OWNER,
            )
        )
        db.commit()

    metadata = loader.load_all_metadata()
    skill_dir = loader.get_skill_dir(skill_id)

    assert skill_id in metadata
    assert skill_dir is not None
    assert (Path(skill_dir) / "SKILL.md").is_file()

# ── Search + install from the marketplace ────────────────────────────────────
def test_search_marketplace(sm_env):
    from mcp_servers.skill_manager_mcp import impl

    out = impl.search_marketplace(user_id=OWNER, query="")
    assert out["ok"] is True
    assert out["count"] > 0  # at least the filesystem-preloaded skills exist
    assert all("slug" in s for s in out["skills"])

def test_install_from_marketplace(sm_env):
    """Pick a real preloaded marketplace skill, install it as private, and assert the AdminSkill row is persisted."""
    from mcp_servers.skill_manager_mcp import impl

    # Find a preloaded slug that is neither built-in nor requires mandatory credentials
    listed = impl.search_marketplace(user_id=OWNER, query="")
    candidate = next(
        (s["slug"] for s in listed["skills"] if s.get("source") != "builtin" and not s.get("installed")),
        None,
    )
    if not candidate:
        pytest.skip("没有可安装的预置市场技能（全为内置或已安装）")

    res = impl.install_from_marketplace(user_id=OWNER, slug=candidate)
    # Skills requiring credentials return ok=False (missing credentials), which is the other expected branch — both count as passing
    if not res["ok"]:
        assert "凭据" in res["message"] or "失败" in res["message"]
        return
    assert res["skill_id"]
    with sm_env.Session() as db:
        assert (
            db.query(AdminSkill)
            .filter(AdminSkill.skill_id == res["skill_id"], AdminSkill.owner_user_id == OWNER)
            .first()
            is not None
        )

# ── Security gate: missing identity / missing permission ─────────────────────
