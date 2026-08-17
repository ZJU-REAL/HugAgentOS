"""plugin-manager plugin self-contained tests: plugin persistence + all 7 MCP verbs end to end.

Coverage targets (matching the goal "search, install, import, enable/disable, uninstall plugins"):
- Installing the plugin-manager plugin → AdminSkill(plugin-creator) + AdminMcpServer(plugin_manager)
  + InstalledPlugin persisted, and merged into that user's available set.
- search_plugin_market / get_plugin_info / install_plugin / list_my_plugins / import_plugin
  / set_plugin_enabled / uninstall_plugin.
- Guards that actually matter: the self-uninstall guard, capability gating, cross-user
  isolation, global plugins being read-only, ambiguous refs asking instead of guessing,
  and non-plugin archives being rejected by import.

No dependency on a running sandbox or mcp container: the impl layer talks to the DB directly /
reuses backend services, and the artifact store is the real store (local mode).
"""

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
    monkeypatch.setattr(store, "_INDEX_PATH", art_dir / "index.json")

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
    assert len(actual) == 7

    assert (BUNDLE_DIR / "skills" / "plugin-creator" / "SKILL.md").is_file()
    assert (BUNDLE_DIR / "skills" / "plugin-creator" / "scripts" / "validate_plugin.py").is_file()


def test_port_is_registered():
    from mcp_servers._ports import PORTS, package_name

    assert PORTS["plugin_manager"] == 9116
    assert package_name("plugin_manager") == "plugin_manager_mcp"


def test_self_slug_matches_the_bundle():
    """自卸载守卫靠 slug 比对；bundle 改名而常量没跟上，守卫就静默失效了。"""
    from mcp_servers.plugin_manager_mcp import impl

    manifest = json.loads((BUNDLE_DIR / "plugin.json").read_text(encoding="utf-8"))
    assert impl.SELF_SLUG == manifest["name"]


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
    from core.services import plugin_service as ps

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
        assert len(mcp.tools_json) == 7
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
def test_install_list_toggle_uninstall_loop(pm_env):
    from mcp_servers.plugin_manager_mcp import impl

    inst = impl.install_plugin(user_id=OWNER, slug="agent-manager")
    assert inst["ok"], inst
    install_id = inst["install_id"]

    listed = impl.list_my_plugins(user_id=OWNER)
    assert listed["ok"] and listed["count"] == 1
    row = listed["plugins"][0]
    assert row["slug"] == "agent-manager" and row["enabled"] is True and row["is_global"] is False
    assert row["components"]["skills"] == 1 and row["components"]["mcp"] == 1

    off = impl.set_plugin_enabled(user_id=OWNER, plugin_ref=install_id, enabled=False)
    assert off["ok"], off
    assert impl.list_my_plugins(user_id=OWNER)["plugins"][0]["enabled"] is False

    on = impl.set_plugin_enabled(user_id=OWNER, plugin_ref="agent-manager", enabled=True)
    assert on["ok"], on
    assert impl.list_my_plugins(user_id=OWNER)["plugins"][0]["enabled"] is True

    gone = impl.uninstall_plugin(user_id=OWNER, plugin_ref=install_id)
    assert gone["ok"], gone
    assert impl.list_my_plugins(user_id=OWNER)["count"] == 0

    # Uninstall really reverse-deletes the components it brought in.
    with pm_env.Session() as db:
        assert db.query(AdminSkill).filter(AdminSkill.source_plugin == "agent-manager").count() == 0
        assert (
            db.query(AdminMcpServer).filter(AdminMcpServer.source_plugin == "agent-manager").count()
            == 0
        )


def test_toggle_survives_a_pre_existing_per_user_override(pm_env):
    """用户在「插件」页点过开关就会留下一条每用户覆写，而启用态解析是"覆写优先"。

    停用若写的是组件的全局 is_enabled（管理员级原语），覆写仍说启用 —— 工具回了
    "✅ 已停用"，list_my_plugins 却照样显示 enabled=true。必须走用户级通路。
    """
    from core.services import plugin_service as ps
    from mcp_servers.plugin_manager_mcp import impl

    install_id = impl.install_plugin(user_id=OWNER, slug="agent-manager")["install_id"]

    # 模拟用户先在页面上开过一次（写入覆写）
    with pm_env.Session() as db:
        ps.set_plugin_enabled_for_user(db, install_id, enabled=True, user_id=OWNER)

    off = impl.set_plugin_enabled(user_id=OWNER, plugin_ref=install_id, enabled=False)
    assert off["ok"], off
    assert impl.list_my_plugins(user_id=OWNER)["plugins"][0]["enabled"] is False, (
        "停用没生效：覆写把它盖回去了，说明写的是全局 is_enabled 而不是用户级覆写"
    )

    on = impl.set_plugin_enabled(user_id=OWNER, plugin_ref=install_id, enabled=True)
    assert on["ok"], on
    assert impl.list_my_plugins(user_id=OWNER)["plugins"][0]["enabled"] is True


def test_cannot_disable_itself(pm_env):
    """停用自己和卸载自己是同一个死局：本插件的 MCP 组件一掉，就没有工具能把它开回来。"""
    from mcp_servers.plugin_manager_mcp import impl

    install_id = impl.install_plugin(user_id=OWNER, slug="plugin-manager")["install_id"]

    res = impl.set_plugin_enabled(user_id=OWNER, plugin_ref=install_id, enabled=False)
    assert not res["ok"], "停用自己竟然成功了——和卸载自己是同一个死局"
    assert "不能" in res["message"]

    # 重新启用自己是无害的，不该被拦。
    assert impl.set_plugin_enabled(user_id=OWNER, plugin_ref=install_id, enabled=True)["ok"]


def test_component_level_toggle(pm_env):
    from mcp_servers.plugin_manager_mcp import impl

    install_id = impl.install_plugin(user_id=OWNER, slug="agent-manager")["install_id"]
    with pm_env.Session() as db:
        skill_id = (
            db.query(AdminSkill).filter(AdminSkill.source_plugin == "agent-manager").first().skill_id
        )

    res = impl.set_plugin_enabled(
        user_id=OWNER,
        plugin_ref=install_id,
        enabled=False,
        component_kind="skill",
        component_id=skill_id,
    )
    assert res["ok"], res

    # 断言的是"用户实际还能不能用到它"，不是组件的全局 is_enabled——
    # 用户级停用写的是每用户覆写，全局位保持不动（别人不受影响），这正是我们要的语义。
    with pm_env.Session() as db:
        from core.config.catalog_resolver import (
            invalidate_capability_cache,
            resolve_all_runtime_enabled,
        )

        invalidate_capability_cache()
        eff_skills, _agents, _mcps = resolve_all_runtime_enabled(db, OWNER)
        assert skill_id not in (eff_skills or []), "组件级停用没有对该用户生效"

        row = db.query(AdminSkill).filter(AdminSkill.skill_id == skill_id).first()
        assert row.is_enabled is True, "用户级停用不该改动组件的全局启用位"


def test_component_toggle_requires_both_params(pm_env):
    from mcp_servers.plugin_manager_mcp import impl

    install_id = impl.install_plugin(user_id=OWNER, slug="agent-manager")["install_id"]
    half = impl.set_plugin_enabled(
        user_id=OWNER, plugin_ref=install_id, enabled=False, component_kind="skill"
    )
    assert not half["ok"] and "必须同时提供" in half["message"]

    bad_kind = impl.set_plugin_enabled(
        user_id=OWNER,
        plugin_ref=install_id,
        enabled=False,
        component_kind="wat",
        component_id="x",
    )
    assert not bad_kind["ok"]


# ── The self-uninstall guard ────────────────────────────────────────────────
def test_cannot_uninstall_itself(pm_env):
    """卸掉插件管理插件自己 = 把自己的手砍掉，之后没有工具能把它装回来。"""
    from mcp_servers.plugin_manager_mcp import impl

    install_id = impl.install_plugin(user_id=OWNER, slug="plugin-manager")["install_id"]

    for ref in (install_id, "plugin-manager", "插件管理"):
        res = impl.uninstall_plugin(user_id=OWNER, plugin_ref=ref)
        assert not res["ok"], f"ref={ref} 竟然把自己卸掉了"
        assert "不能卸载" in res["message"]

    # Still installed and still functional.
    assert impl.list_my_plugins(user_id=OWNER)["count"] == 1
    with pm_env.Session() as db:
        assert (
            db.query(InstalledPlugin).filter(InstalledPlugin.slug == "plugin-manager").first()
            is not None
        )


# ── Import from the shared artifact store ───────────────────────────────────
def test_import_plugin_from_artifact(pm_env):
    from mcp_servers.plugin_manager_mcp import impl

    art = _stash(_make_plugin_tar("demo-plugin"))
    res = impl.import_plugin(user_id=OWNER, artifact_id=art)
    assert res["ok"], res
    assert res["install_id"]

    listed = impl.list_my_plugins(user_id=OWNER)
    assert {p["slug"] for p in listed["plugins"]} == {"demo-plugin"}
    with pm_env.Session() as db:
        sk = db.query(AdminSkill).filter(AdminSkill.source_plugin == "demo-plugin").all()
        assert len(sk) == 1 and sk[0].owner_user_id == OWNER


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


def test_get_plugin_info_falls_back_to_installed_detail(pm_env):
    """自己 import 进来的插件不在市场目录里，但详情必须查得到。

    真实端到端里发现的：导入成功 → 紧接着问详情 → "查不到插件"，用户只能自己猜发生了什么。
    """
    from mcp_servers.plugin_manager_mcp import impl

    art = _stash(_make_plugin_tar("selfmade-plugin"))
    assert impl.import_plugin(user_id=OWNER, artifact_id=art)["ok"]

    res = impl.get_plugin_info(user_id=OWNER, slug="selfmade-plugin")
    assert res["ok"], res
    assert res["installed"] is True
    assert len(res["skills"]) == 1

    # 别人的私有插件仍然查不到（回落不能变成越权读取通道）。
    other = impl.get_plugin_info(user_id=OTHER, slug="selfmade-plugin")
    assert not other["ok"]


def test_import_rejects_non_plugin_archive(pm_env):
    """只有 SKILL.md 的包不是插件包——要明确拒绝并指路，不能半吞半吐。"""
    from mcp_servers.plugin_manager_mcp import impl

    art = _stash(_make_skill_only_tar(), name="skill.tgz")
    res = impl.import_plugin(user_id=OWNER, artifact_id=art)
    assert not res["ok"]
    assert "plugin.json" in res["message"] and "register_skill" in res["message"]
    assert impl.list_my_plugins(user_id=OWNER)["count"] == 0


def test_import_missing_artifact_fails_cleanly(pm_env):
    from mcp_servers.plugin_manager_mcp import impl

    assert not impl.import_plugin(user_id=OWNER, artifact_id="")["ok"]
    res = impl.import_plugin(user_id=OWNER, artifact_id="no-such-artifact")
    assert not res["ok"] and "找不到" in res["message"]


def test_import_rejects_path_traversal_archive(pm_env):
    """目录穿越条目必须在解包阶段就被拦下（防护在 _packaging 里，这里守住回归）。"""
    from mcp_servers.plugin_manager_mcp import impl

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        _add(tf, "../evil.json", "{}")
    art = _stash(buf.getvalue(), name="evil.tgz")

    res = impl.import_plugin(user_id=OWNER, artifact_id=art)
    assert not res["ok"]
    assert "解包失败" in res["message"] or "目录穿越" in res["message"]


# ── Isolation / read-only global plugins ────────────────────────────────────
def test_cannot_touch_other_users_plugins(pm_env):
    from mcp_servers.plugin_manager_mcp import impl

    install_id = impl.install_plugin(user_id=OWNER, slug="agent-manager")["install_id"]

    assert impl.list_my_plugins(user_id=OTHER)["count"] == 0
    assert not impl.uninstall_plugin(user_id=OTHER, plugin_ref=install_id)["ok"]
    assert not impl.set_plugin_enabled(user_id=OTHER, plugin_ref=install_id, enabled=False)["ok"]

    with pm_env.Session() as db:
        assert (
            db.query(InstalledPlugin).filter(InstalledPlugin.install_id == install_id).first()
            is not None
        )


def test_global_plugins_are_visible_but_read_only(pm_env):
    """管理员下发的全局插件用户看得见（否则会重复安装），但停用/卸载都不该由自助入口做。"""
    from core.services import plugin_service as ps
    from mcp_servers.plugin_manager_mcp import impl

    with pm_env.Session() as db:
        ps.install_plugin(db, "agent-manager", owner_user_id=None, created_by="admin")

    listed = impl.list_my_plugins(user_id=OWNER)
    assert listed["count"] == 1
    assert listed["plugins"][0]["is_global"] is True

    gid = listed["plugins"][0]["install_id"]
    for res in (
        impl.uninstall_plugin(user_id=OWNER, plugin_ref=gid),
        impl.set_plugin_enabled(user_id=OWNER, plugin_ref=gid, enabled=False),
    ):
        assert not res["ok"]
        assert "全局插件" in res["message"]


def test_ambiguous_ref_returns_candidates_instead_of_guessing(pm_env):
    from mcp_servers.plugin_manager_mcp import impl

    art_a = _stash(_make_plugin_tar("alpha-tool"))
    art_b = _stash(_make_plugin_tar("beta-tool"))
    impl.import_plugin(user_id=OWNER, artifact_id=art_a)
    impl.import_plugin(user_id=OWNER, artifact_id=art_b)

    res = impl.uninstall_plugin(user_id=OWNER, plugin_ref="tool")
    assert not res["ok"]
    assert res.get("need_clarification") is True
    assert len(res["candidates"]) == 2
    assert impl.list_my_plugins(user_id=OWNER)["count"] == 2


# ── Capability flag gating ──────────────────────────────────────────────────
def test_write_verbs_blocked_without_capability(pm_env, monkeypatch):
    from mcp_servers.plugin_manager_mcp import impl

    install_id = impl.install_plugin(user_id=OWNER, slug="agent-manager")["install_id"]

    import core.auth.capabilities as caps

    monkeypatch.setattr(
        caps, "resolve_user_capabilities", lambda db, uid: {"can_import_plugin": False}
    )

    for res in (
        impl.install_plugin(user_id=OWNER, slug="skill-manager"),
        impl.set_plugin_enabled(user_id=OWNER, plugin_ref=install_id, enabled=False),
        impl.uninstall_plugin(user_id=OWNER, plugin_ref=install_id),
    ):
        assert not res["ok"]
        assert "can_import_plugin" in res["message"]

    assert impl.list_my_plugins(user_id=OWNER)["ok"]
    assert impl.search_plugin_market(user_id=OWNER)["ok"]


def test_missing_user_header_is_refused(pm_env):
    from mcp_servers.plugin_manager_mcp import impl

    for res in (
        impl.list_my_plugins(user_id=""),
        impl.search_plugin_market(user_id=""),
        impl.install_plugin(user_id="", slug="x"),
        impl.import_plugin(user_id="", artifact_id="x"),
        impl.uninstall_plugin(user_id="", plugin_ref="x"),
        impl.set_plugin_enabled(user_id="", plugin_ref="x", enabled=True),
    ):
        assert not res["ok"] and "用户身份" in res["message"]


# ── Server layer ────────────────────────────────────────────────────────────
def test_server_tools_forward_user_header(pm_env, monkeypatch):
    from mcp_servers.plugin_manager_mcp import server

    captured = {}

    def _fake(*, user_id):
        captured["user_id"] = user_id
        return {"ok": True, "plugins": [], "count": 0, "message": ""}

    monkeypatch.setattr(server.impl, "list_my_plugins", _fake)

    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers={"x-current-user-id": OWNER})
        )
    )
    asyncio.run(server.list_my_plugins(ctx=ctx))
    assert captured["user_id"] == OWNER

    captured.clear()
    asyncio.run(server.list_my_plugins(ctx=None))
    assert captured["user_id"] == ""


def test_every_server_tool_is_async_and_documented():
    from mcp_servers.plugin_manager_mcp import server

    tools = asyncio.run(server.mcp.list_tools())
    assert len(tools) == 7
    for t in tools:
        fn = getattr(server, t.name)
        assert inspect.iscoroutinefunction(fn), f"{t.name} 必须是 async"
        assert (fn.__doc__ or "").strip(), f"{t.name} 缺 docstring（它就是模型看到的工具描述）"
