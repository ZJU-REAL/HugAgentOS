"""Cloud plugin-manager marketplace and bundle contracts. Lifecycle tests live in tests/capabilities/test_*management*.py."""

import asyncio
import inspect
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.db.engine as dbe
from core.db.models import AdminMcpServer, AdminSkill, InstalledPlugin

OWNER = "pm_test_user"
OTHER = "pm_other_user"
BUNDLE_ROOT = Path(__file__).resolve().parents[1] / "plugin_bundles" / "marketplace"
BUNDLE_DIR = BUNDLE_ROOT / "plugin-manager"

@pytest.fixture()
def pm_env(tmp_path, monkeypatch):
    """Bind SessionLocal to an isolated sqlite file DB; point the artifact store at tmp; allow capabilities."""
    url = f"sqlite:///{tmp_path}/pm.db"
    engine = create_engine(url)
    dbe.Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(dbe, "SessionLocal", TestSession)

    from core.artifacts import store

    art_dir = tmp_path / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(store, "_STORE_DIR", art_dir)

    import core.auth.capabilities as caps

    monkeypatch.setattr(
        caps,
        "resolve_user_capabilities",
        lambda db, uid: {"can_import_plugin": True, "can_add_skill": True},
    )

    return SimpleNamespace(engine=engine, Session=TestSession)

def _add(tf: tarfile.TarFile, name: str, text: str) -> None:
    data = text.encode("utf-8")
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tf.addfile(info, io.BytesIO(data))

def _make_plugin_tar(slug: str = "demo-plugin") -> bytes:
    """Pack a minimal plugin package (plugin.json + one skill) into a tar.gz, root at package root."""
    manifest = {
        "name": slug,
        "version": "1.0.0",
        "description": "一个用于测试的最小插件。",
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        _add(tf, "plugin.json", json.dumps(manifest, ensure_ascii=False))
        _add(
            tf,
            "skills/demo-skill/SKILL.md",
            "---\nname: demo-skill\ndescription: 当用户需要演示时使用。\n---\n\n演示。\n",
        )
    return buf.getvalue()

def _make_skill_only_tar() -> bytes:
    """A skill package (no plugin.json) — import_plugin must refuse it, not silently half-import."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        _add(tf, "SKILL.md", "---\nname: lone-skill\ndescription: 单个技能。\n---\n\n正文。\n")
    return buf.getvalue()

def _stash(tar_bytes: bytes, name: str = "plugin.tgz") -> str:
    from core.artifacts import store

    return store.save_artifact_bytes(content=tar_bytes, name=name, extension="tgz")["file_id"]

# ── Bundle shape ────────────────────────────────────────────────────────────
def test_bundle_manifest_is_wellformed():
    manifest = json.loads((BUNDLE_DIR / "plugin.json").read_text(encoding="utf-8"))
    mcp_json = json.loads((BUNDLE_DIR / "mcp.json").read_text(encoding="utf-8"))

    assert manifest["name"] == "plugin-manager"
    ext_mcp = manifest["extensions"]["org.hugagent"]["mcp"]
    assert set(ext_mcp) == set(mcp_json["mcpServers"]), "扩展段的服务名必须与 mcp.json 一一对应"
    assert mcp_json["mcpServers"]["plugin_manager"]["url"] == "http://mcp:9116/mcp/"

    from mcp_servers.plugin_manager_mcp import server

    declared = {t["name"] for t in ext_mcp["plugin_manager"]["tools"]}
    actual = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert declared == actual
    assert len(actual) == 8

    assert (BUNDLE_DIR / "skills" / "plugin-creator" / "SKILL.md").is_file()
    assert (BUNDLE_DIR / "skills" / "plugin-creator" / "scripts" / "validate_plugin.py").is_file()

def test_port_is_registered():
    from mcp_servers._ports import PORTS, package_name

    assert PORTS["plugin_manager"] == 9116
    assert package_name("plugin_manager") == "plugin_manager_mcp"

def test_executor_identity_matches_the_bundle():
    manifest = json.loads((BUNDLE_DIR / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["extensions"]["org.hugagent"]["local_execution"] == {"id": manifest["name"], "version": 1}

# ── The shipped validator must accept every real bundle in the repo ─────────
def test_shipped_validator_accepts_all_real_bundles():
    import subprocess
    import sys

    script = BUNDLE_DIR / "skills" / "plugin-creator" / "scripts" / "validate_plugin.py"
    for bundle in sorted(p for p in BUNDLE_ROOT.iterdir() if (p / "plugin.json").is_file()):
        r = subprocess.run(
            [sys.executable, str(script), str(bundle)], capture_output=True, text=True
        )
        assert r.returncode == 0, f"{bundle.name} 未通过自检：\n{r.stdout}\n{r.stderr}"

# ── Plugin persistence + merging into the user's available set ──────────────
def test_install_plugin_creates_rows_and_is_agent_visible(pm_env):
    from core.plugins import management as ps

    with pm_env.Session() as db:
        res = ps.install_plugin(db, "plugin-manager", owner_user_id=OWNER, created_by=OWNER)
        assert res.get("install_id")

    with pm_env.Session() as db:
        skills = db.query(AdminSkill).filter(AdminSkill.source_plugin == "plugin-manager").all()
        mcps = db.query(AdminMcpServer).filter(AdminMcpServer.source_plugin == "plugin-manager").all()
        plugin = db.query(InstalledPlugin).filter(InstalledPlugin.slug == "plugin-manager").first()

        assert len(skills) == 1 and skills[0].owner_user_id == OWNER
        assert len(mcps) == 1
        mcp = mcps[0]
        assert mcp.url == "http://mcp:9116/mcp/"
        assert mcp.is_enabled is True
        assert len(mcp.tools_json) == 8
        assert plugin is not None

        from core.config.catalog_resolver import (
            invalidate_capability_cache,
            resolve_all_runtime_enabled,
        )

        invalidate_capability_cache()
        enabled_skills, _agents, enabled_mcps = resolve_all_runtime_enabled(db, OWNER)
        assert skills[0].skill_id in (enabled_skills or [])
        assert mcp.server_id in (enabled_mcps or [])

# ── Market verbs ────────────────────────────────────────────────────────────
def test_search_plugin_market_lists_builtin_bundles(pm_env):
    from mcp_servers.plugin_manager_mcp import impl

    res = impl.search_plugin_market(user_id=OWNER)
    assert res["ok"], res
    slugs = {p["slug"] for p in res["plugins"]}
    assert {"agent-manager", "plugin-manager", "skill-manager"} <= slugs

    narrowed = impl.search_plugin_market(user_id=OWNER, query="__no_such_plugin__")
    assert narrowed["count"] == 0

def test_get_plugin_info_previews_components(pm_env):
    from mcp_servers.plugin_manager_mcp import impl

    res = impl.get_plugin_info(user_id=OWNER, slug="agent-manager")
    assert res["ok"], res
    assert res["installed"] is False
    assert len(res["skills"]) == 1
    assert len(res["mcp_servers"]) == 1
    assert len(res["mcp_servers"][0]["tools"]) == 8

    impl.install_plugin(user_id=OWNER, slug="agent-manager")
    assert impl.get_plugin_info(user_id=OWNER, slug="agent-manager")["installed"] is True

def test_get_plugin_info_unknown_slug_fails_cleanly(pm_env):
    from mcp_servers.plugin_manager_mcp import impl

    res = impl.get_plugin_info(user_id=OWNER, slug="__nope__")
    assert not res["ok"] and res["message"].startswith("❌")

# ── Install → list → disable → enable → uninstall ───────────────────────────

# ── The self-uninstall guard ────────────────────────────────────────────────

# ── Import from the shared artifact store ───────────────────────────────────

def test_plugin_root_detection_matches_the_importer(pm_env):
    """"什么算插件包"只能有一套判定。

    导入器认原生 / .claude-plugin / .codex-plugin 三种布局；本地若少认一种，
    同一个包就会"后台上传能装、对话里说不是插件包"。
    """
    from mcp_servers._packaging import is_plugin_root, locate_root

    for layout in ("plugin.json", ".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
        d = Path(pm_env.engine.url.database).parent / f"pkg_{layout.replace('/', '_')}"
        f = d / layout
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"name": "x"}), encoding="utf-8")
        assert is_plugin_root(d), f"{layout} 布局没被认成插件包"
        assert locate_root(d) == d

    empty = Path(pm_env.engine.url.database).parent / "pkg_empty"
    empty.mkdir(parents=True, exist_ok=True)
    assert not is_plugin_root(empty)

# ── Isolation / read-only global plugins ────────────────────────────────────

# ── Capability flag gating ──────────────────────────────────────────────────

# ── Server layer ────────────────────────────────────────────────────────────
def test_server_tools_forward_user_header(pm_env, monkeypatch):
    from mcp_servers.plugin_manager_mcp import server

    captured = {}

    def _fake(user_id, kind):
        captured["user_id"] = user_id
        return {"ok": True, "plugins": [], "count": 0, "message": ""}

    from core.services import cloud_management
    monkeypatch.setattr(cloud_management, "list_installed", _fake)
    handler = server.mcp._tool_manager.get_tool("list_plugins").fn

    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers={"x-current-user-id": OWNER})
        )
    )
    asyncio.run(handler(ctx=ctx))
    assert captured["user_id"] == OWNER

    captured.clear()
    asyncio.run(handler(ctx=None))
    assert captured["user_id"] == ""

def test_every_server_tool_is_async_and_documented():
    from mcp_servers.plugin_manager_mcp import server

    tools = asyncio.run(server.mcp.list_tools())
    assert len(tools) == 8
    for t in tools:
        fn = server.mcp._tool_manager.get_tool(t.name).fn
        assert inspect.iscoroutinefunction(fn), f"{t.name} 必须是 async"
        assert (t.description or "").strip(), f"{t.name} 缺工具描述"
