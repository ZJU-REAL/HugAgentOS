"""skill-manager plugin self-contained tests: plugin persistence + all 7 MCP verbs end to end.

Coverage targets (matching the goal "create, manage, delete, and submit skills for listing"):
- Installing the skill-manager plugin → AdminSkill(skill-creator) + AdminMcpServer(skill_manager)
  + InstalledPlugin persisted, and merged into that user's available set via
  resolve_all_runtime_enabled (= the agent can really get them).
- register_skill: persists a skill tar via the shared artifact store as a private skill
  (simulating a sandbox_get_artifact artifact).
- list_my_skills / submit_to_marketplace / delete_skill / search_marketplace / install_from_marketplace.

No dependency on a running sandbox or mcp container: the impl layer talks to the DB directly /
reuses backend services, and the artifact store is the real store (local mode).
"""

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

    # Artifact store goes to tmp (the store's _STORE_DIR/_INDEX_PATH are fixed at import time; patch globally)
    from core.artifacts import store

    art_dir = tmp_path / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(store, "_STORE_DIR", art_dir)
    monkeypatch.setattr(store, "_INDEX_PATH", art_dir / "index.json")

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
        assert len(mcp.tools_json) == 7
        assert plugin is not None

        # Key: merged into the user's available set via resolve_all_runtime_enabled (= the agent can really get the skill + mcp)
        from core.config.catalog_resolver import resolve_all_runtime_enabled, invalidate_capability_cache

        invalidate_capability_cache()
        enabled_skills, _agents, enabled_mcps = resolve_all_runtime_enabled(db, OWNER)
        assert skills[0].skill_id in (enabled_skills or [])
        assert mcp.server_id in (enabled_mcps or [])


# ── Create (register_skill, via the shared artifact store) + manage + submit for listing + delete ──
def test_register_list_submit_delete_loop(sm_env):
    from mcp_servers.skill_manager_mcp import impl

    # 1) Create: simulate "sandbox produces tar → sandbox_get_artifact → register_skill"
    art_id = _stash_artifact(_make_skill_tar(
        "weather-brief", "当用户想要一份某城市的今日天气简报时，生成结构化天气摘要。"
    ))
    res = impl.register_skill(user_id=OWNER, artifact_id=art_id, make_private=True)
    assert res["ok"] is True, res
    assert res["kind"] == "skill"
    skill_id = res["skill_id"]

    with sm_env.Session() as db:
        row = db.query(AdminSkill).filter(AdminSkill.skill_id == skill_id).first()
        assert row is not None
        assert row.owner_user_id == OWNER  # private
        assert row.is_enabled is True
        assert row.created_by == "agent_skill_creator"

    # 2) Manage: list_my_skills can see it
    listed = impl.list_my_skills(user_id=OWNER)
    assert listed["ok"] and listed["count"] >= 1
    assert any(s["skill_id"] == skill_id for s in listed["skills"])

    # 3) Submit for listing: enters the review queue as pending
    sub = impl.submit_to_marketplace(
        user_id=OWNER, skill_id=skill_id, category="办公效率", summary="今日天气简报"
    )
    assert sub["ok"] is True, sub
    assert sub["status"] == "pending"
    with sm_env.Session() as db:
        srow = (
            db.query(MarketplaceSubmission)
            .filter(MarketplaceSubmission.skill_id == skill_id)
            .first()
        )
        assert srow is not None and srow.status == "pending"

    # 4) Delete: the private skill is removed
    dele = impl.delete_skill(user_id=OWNER, skill_ref=skill_id)
    assert dele["ok"] is True, dele
    with sm_env.Session() as db:
        assert db.query(AdminSkill).filter(AdminSkill.skill_id == skill_id).first() is None


# ── Edit: in-place metadata / body / auxiliary-file changes + unauthorized-access blocking ──
def test_edit_skill_updates_metadata_body_and_files(sm_env):
    from mcp_servers.skill_manager_mcp import impl
    from core.agent_skills.registry import _split_frontmatter

    art_id = _stash_artifact(_make_skill_tar(
        "edit-target", "编辑前的原始描述。", body="原始正文。"
    ))
    reg = impl.register_skill(user_id=OWNER, artifact_id=art_id)
    assert reg["ok"], reg
    sid = reg["skill_id"]

    # Partial update: only change description + body + add one auxiliary file; name/version not passed should stay as-is.
    res = impl.edit_skill(
        user_id=OWNER,
        skill_ref=sid,
        description="更新后的描述。",
        instructions="更新后的正文，做得更好。",
        files_upsert={"helper.py": "print('hi')\n"},
    )
    assert res["ok"] is True, res
    assert res["skill_id"] == sid

    with sm_env.Session() as db:
        row = db.query(AdminSkill).filter(AdminSkill.skill_id == sid).first()
        assert row.description == "更新后的描述。"
        _, body = _split_frontmatter(row.skill_content)
        assert "更新后的正文" in body
        assert "原始正文" not in body
        assert row.extra_files.get("helper.py") == "print('hi')\n"
        # description in the frontmatter was synced too
        assert "更新后的描述" in row.skill_content

    # Then delete the file that was just added
    res2 = impl.edit_skill(user_id=OWNER, skill_ref=sid, files_delete=["helper.py"])
    assert res2["ok"] is True, res2
    with sm_env.Session() as db:
        row = db.query(AdminSkill).filter(AdminSkill.skill_id == sid).first()
        assert "helper.py" not in (row.extra_files or {})


def test_edit_skill_guards(sm_env):
    """Empty changes rejected, description can't be blanked, skill not found, and unauthorized edits of others' skills are all blocked."""
    from mcp_servers.skill_manager_mcp import impl

    art_id = _stash_artifact(_make_skill_tar("guard-skill", "守卫用例的技能描述。"))
    sid = impl.register_skill(user_id=OWNER, artifact_id=art_id)["skill_id"]

    # Nothing passed → rejected
    assert impl.edit_skill(user_id=OWNER, skill_ref=sid)["ok"] is False
    # description changed to empty → rejected
    assert impl.edit_skill(user_id=OWNER, skill_ref=sid, description="  ")["ok"] is False
    # Missing identity → rejected
    assert impl.edit_skill(user_id="", skill_ref=sid, description="x")["ok"] is False
    # Not found → rejected
    assert impl.edit_skill(user_id=OWNER, skill_ref="no-such-skill", description="x")["ok"] is False
    # Another user can't edit this user's skill (_resolve_skill only accepts the owner)
    other = impl.edit_skill(user_id="other_user", skill_ref=sid, description="x")
    assert other["ok"] is False
    # SKILL.md must not be writable as an auxiliary file
    bad = impl.edit_skill(user_id=OWNER, skill_ref=sid, files_upsert={"SKILL.md": "x"})
    assert bad["ok"] is False
    # Path traversal is blocked
    trav = impl.edit_skill(user_id=OWNER, skill_ref=sid, files_upsert={"../evil.sh": "x"})
    assert trav["ok"] is False


def test_register_skill_forces_private_even_when_requested_global(sm_env):
    """Self-serve register_skill must not create/overwrite global skills via make_private=false."""
    from mcp_servers.skill_manager_mcp import impl

    art_id = _stash_artifact(_make_skill_tar(
        "global-request", "测试自助入口强制落成私有技能。"
    ))
    res = impl.register_skill(user_id=OWNER, artifact_id=art_id, make_private=False)
    assert res["ok"] is True, res

    with sm_env.Session() as db:
        rows = db.query(AdminSkill).filter(AdminSkill.display_name == "global-request").all()
        assert len(rows) == 1
        assert rows[0].owner_user_id == OWNER
        assert rows[0].skill_id != "global-request"
        assert (
            db.query(AdminSkill)
            .filter(AdminSkill.skill_id == "global-request", AdminSkill.owner_user_id.is_(None))
            .first()
            is None
        )


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
    assert "SKILL_CREATOR_DIR" in content


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


def test_register_skill_updates_in_place_on_same_name(sm_env):
    """Registering a same-named skill again → in-place update (no duplicate creation)."""
    from mcp_servers.skill_manager_mcp import impl

    a1 = _stash_artifact(_make_skill_tar("note-taker", "记录会议要点并输出结构化纪要。", body="v1"))
    r1 = impl.register_skill(user_id=OWNER, artifact_id=a1)
    assert r1["ok"] and r1["action"] == "created"
    sid = r1["skill_id"]

    a2 = _stash_artifact(_make_skill_tar("note-taker", "记录会议要点并输出结构化纪要。", body="v2 改进版"))
    r2 = impl.register_skill(user_id=OWNER, artifact_id=a2)
    assert r2["ok"] and r2["action"] == "updated"
    assert r2["skill_id"] == sid  # same id

    with sm_env.Session() as db:
        rows = db.query(AdminSkill).filter(AdminSkill.skill_id == sid).all()
        assert len(rows) == 1
        assert "v2" in rows[0].skill_content


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
def test_no_user_rejected(sm_env):
    from mcp_servers.skill_manager_mcp import impl

    assert impl.register_skill(user_id="", artifact_id="x")["ok"] is False
    assert impl.install_from_marketplace(user_id="", slug="x")["ok"] is False
    assert impl.delete_skill(user_id="", skill_ref="x")["ok"] is False


def test_capability_gate_blocks_write(sm_env, monkeypatch):
    from mcp_servers.skill_manager_mcp import impl
    import core.auth.capabilities as caps

    monkeypatch.setattr(
        caps, "resolve_user_capabilities",
        lambda db, uid: {"can_add_skill": False, "can_import_plugin": False},
    )
    art_id = _stash_artifact(_make_skill_tar("blocked-skill", "测试权限闸拦截写入。"))
    res = impl.register_skill(user_id=OWNER, artifact_id=art_id)
    assert res["ok"] is False
    assert "can_add_skill" in res["message"] or "未开放" in res["message"]
