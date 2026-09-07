"""Desktop plugin activation keeps the prepared source and tool surface."""

import asyncio
from types import SimpleNamespace
import pytest
from core.capabilities import plugins, skills, runtime
from core.llm import plugin_loader
from core.services.desktop_capability_protocol import skill_content_hash
from core.llm.tool_collector import ToolCollector


def test_desktop_directory_defers_skill_until_load_plugin(index_db, caps_root, monkeypatch):
    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    body = "---\nname: report\ndescription: Write reports\n---\nUse the report template."
    skills.publish_local_skill(
        "report",
        files={
            "SKILL.md": "---\nname: report\ndescription: Write reports\n---\nUse the report template."
        },
        content_hash=skill_content_hash(body, {}),
        owner_user_id="owner",
    )
    plugins.publish_local_plugin(
        {"slug": "reports", "name": "Reports", "components": {"skills": ["report"]}},
        owner_user_id="owner",
    )
    plan = plugin_loader.resolve_desktop_progressive_plugins(
        user_id="owner",
        enabled_skill_ids=["report"],
        enabled_mcp_ids=[],
        plugin_ids=None,
    )
    assert [p.slug for p in plan.deferred] == ["reports"]
    run = runtime.prepare(
        "run", "owner", skill_ids=["report"], plugin_ids=[plan.directory[0].install_id]
    )
    run = runtime.preflight(run, skill_ids=["report"], plugin_ids=[plan.directory[0].install_id])
    basic = SimpleNamespace(mcps=[], skills_or_loaders=[])
    collector = ToolCollector()
    context = {
        "toolkit": SimpleNamespace(tool_groups=[basic]),
        "loader": runtime.frozen_loader(run),
        "prepared_run": run,
        "prepared_servers": {},
        "persist": False,
    }
    plugin_loader.register_load_plugin(collector, plan.deferred_by_slug(), context)
    assert basic.skills_or_loaders == []
    tool = next(t for t in collector.function_tools if t.name == "load_plugin")
    asyncio.run(tool(plugin="reports"))
    assert len(basic.skills_or_loaders) == 1


@pytest.mark.parametrize("bound", [False, True])
def test_partial_plugin_and_explicit_skill_preserve_selected_surface(
    index_db, caps_root, monkeypatch, bound
):
    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    for name in ["report", "chart"]:
        body = f"---\nname: {name}\ndescription: Test skill\n---\nInstructions"
        skills.publish_local_skill(
            name,
            files={"SKILL.md": body},
            content_hash=skill_content_hash(body, {}),
            owner_user_id="owner",
        )
    plugins.publish_local_plugin(
        {"slug": "reports", "components": {"skills": ["report", "chart"]}}, owner_user_id="owner"
    )
    args = dict(
        user_id="owner",
        enabled_skill_ids=["report"],
        enabled_mcp_ids=[],
        plugin_ids=["reports"] if bound else None,
    )
    plan = plugin_loader.resolve_desktop_progressive_plugins(**args)
    assert plan.deferred_skill_ids == {"report"}
    run = runtime.prepare("partial", "owner", skill_ids=["report"])
    frozen = runtime.preflight(
        run, skill_ids=["report"], plugin_nodes=plan.directory[0].capability_nodes
    )
    assert frozen.dependency_report["ready"]
    explicit = plugin_loader.resolve_desktop_progressive_plugins(
        **args, invoked_skill_ids=["report"]
    )
    assert explicit.deferred_skill_ids == set()


@pytest.mark.parametrize("mutation", ["revoke", "tamper"])
def test_load_plugin_rechecks_definition_before_exposing_skills(
    index_db, caps_root, monkeypatch, mutation
):
    from core.capabilities import registry
    from core.capabilities.errors import CapabilityError

    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    body = "---\nname: report\ndescription: Test skill\n---\nInstructions"
    skills.publish_local_skill(
        "report",
        files={"SKILL.md": body},
        content_hash=skill_content_hash(body, {}),
        owner_user_id="owner",
    )
    component = plugins.publish_local_plugin(
        {"slug": "reports", "components": {"skills": ["report"]}}, owner_user_id="owner"
    )
    plan = plugin_loader.resolve_desktop_progressive_plugins(
        user_id="owner", enabled_skill_ids=["report"], enabled_mcp_ids=[]
    )
    run = runtime.prepare("revoke", "owner", skill_ids=["report"])
    run = runtime.preflight(
        run, skill_ids=["report"], plugin_nodes=plan.directory[0].capability_nodes
    )
    loader = runtime.frozen_loader(run)
    basic = SimpleNamespace(mcps=[], skills_or_loaders=[])
    collector = ToolCollector()
    plugin_loader.register_load_plugin(
        collector,
        plan.deferred_by_slug(),
        {
            "toolkit": SimpleNamespace(tool_groups=[basic]),
            "loader": loader,
            "prepared_run": run,
            "prepared_servers": {},
            "persist": False,
        },
    )
    if mutation == "revoke":
        registry.set_enabled("plugin:local:reports", False)
    else:
        component.entry_file.write_text("{}")
    with pytest.raises(CapabilityError):
        asyncio.run(collector.get_tool("load_plugin")._func("reports"))
    assert basic.skills_or_loaders == []


def test_pending_cloud_plugin_is_discovered_from_manifest(index_db, caps_root, monkeypatch):
    import io, zipfile
    from core.capabilities import registry
    from core.capabilities.ref import cloud_ref, profile_id
    from core.services import desktop_cloud_bridge as bridge, desktop_cloud_bundles as bundles
    from core.services.desktop_capability_protocol import entity_content_hash
    from tests.capabilities.test_runtime_recovery import state
    from core.db.engine import Base

    with index_db() as db:
        Base.metadata.create_all(db.get_bind())
    monkeypatch.setattr("core.db.engine.SessionLocal", index_db)
    st = state("cloud-owner")
    monkeypatch.setattr(bridge, "get_state", lambda: st)
    monkeypatch.setattr(skills, "current_local_user_id", lambda: "owner")
    monkeypatch.setattr(bridge, "ensure_current_authorization", lambda: None)
    files = plugins.plugin_manifest_files({"slug": "reports", "components": {"skills": ["report"]}})
    inst = registry.upsert(
        profile_id=profile_id(st["cloud_base"], "cloud-owner"),
        ref=cloud_ref(st["cloud_base"], "plugin", "reports", scope="private"),
        content_hash=entity_content_hash(files),
        payload={"cloud_install_id": "reports@cloud", "components": {"skills": ["report"]}},
    )
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as out:
        for name, body in files.items():
            out.writestr(name, body)
    # Bundle download is the network boundary; publication/verification remain real.
    monkeypatch.setattr(bundles, "_download", lambda *a: archive.getvalue())
    body = "---\nname: report\ndescription: Cloud report\n---\nInstructions"
    registry.upsert(
        profile_id=inst.profile_id,
        ref=cloud_ref(st["cloud_base"], "skill", "report", scope="private"),
        content_hash=skill_content_hash(body, {}),
    )
    skill_archive = io.BytesIO()
    with zipfile.ZipFile(skill_archive, "w") as out:
        out.writestr("SKILL.md", body)
    from core.services import desktop_cloud_skills

    monkeypatch.setattr(desktop_cloud_skills, "_download", lambda *a: skill_archive.getvalue())
    assert not registry.get(inst.install_id).ready
    defaults = plugin_loader.prepare_desktop_plugin_skill_defaults("owner", [])
    assert defaults == ["report"]
    plan = plugin_loader.resolve_desktop_progressive_plugins(
        user_id="owner", enabled_skill_ids=defaults, enabled_mcp_ids=[]
    )
    assert plan.deferred_skill_ids == {"report"}
    assert plan.directory[0].install_id == inst.install_id


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["", "child-agent"])
async def test_load_plugin_adds_frozen_mcp_schema_to_live_toolkit(
    index_db, caps_root, monkeypatch, scope
):
    from agentscope.tool import Toolkit

    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    plugins.publish_local_plugin(
        {"slug": "lookup", "components": {"mcp": ["lookup-mcp"]}}, owner_user_id="owner"
    )
    plan = plugin_loader.resolve_desktop_progressive_plugins(
        user_id="owner",
        enabled_skill_ids=[],
        enabled_mcp_ids=["lookup-mcp"],
        plugin_ids=["lookup"] if scope else None,
    )
    run = runtime.prepare("mcp-run", "owner", skill_ids=[], scope_id=scope)
    configs = {
        "lookup-mcp": {
            "transport": "streamable_http",
            "url": "https://cloud.example/call",
            "schema_source": "cloud_manifest",
            "gateway_invoke_url": "https://cloud.example/call",
            "schema_hash": "a" * 64,
            "manifest_tools": [{"name": "find_record", "inputSchema": {"type": "object"}}],
        }
    }
    runtime.bind_mcp(run, configs, None)
    run = runtime.preflight(
        run, available_mcp={"lookup-mcp"}, plugin_nodes=plan.directory[0].capability_nodes
    )
    collector = ToolCollector()
    context = {
        "prepared_run": run,
        "prepared_servers": configs,
        "persist": False,
        "loader": runtime.frozen_loader(run),
        "close_list": [],
    }
    plugin_loader.register_load_plugin(collector, plan.deferred_by_slug(), context)
    toolkit = Toolkit(tools=collector.function_tools)
    context["toolkit"] = toolkit
    before = await toolkit.get_tool_schemas()
    assert "find_record" not in str(before)
    await collector.get_tool("load_plugin")(plugin="lookup")
    assert "find_record" in str(await toolkit.get_tool_schemas())
    for client in context["close_list"]:
        await client.close()


def test_plugin_selected_skill_version_constraint_is_not_bypassed(index_db, caps_root, monkeypatch):
    from core.capabilities.errors import PackageMissing

    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    body = "---\nname: report\ndescription: Test\n---\nInstructions"
    skills.publish_local_skill(
        "report",
        files={"SKILL.md": body},
        content_hash=skill_content_hash(body, {}),
        version="1.0",
        owner_user_id="owner",
    )
    plugins.publish_local_plugin(
        {
            "slug": "reports",
            "components": {"skills": [{"id": "report", "version_constraint": ">=2"}]},
        },
        owner_user_id="owner",
    )
    with pytest.raises(PackageMissing):
        plugin_loader.resolve_desktop_progressive_plugins(
            user_id="owner", enabled_skill_ids=["report"], enabled_mcp_ids=[]
        )


def test_preflight_checks_requirement_against_frozen_skill_version(
    index_db, caps_root, monkeypatch
):
    from core.capabilities.dependency import DependencyMissing

    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])

    def install(version):
        body = f"---\nname: report\ndescription: Test\n---\nVersion {version}"
        skills.publish_local_skill(
            "report",
            files={"SKILL.md": body},
            content_hash=skill_content_hash(body, {}),
            version=version,
            owner_user_id="owner",
        )

    install("1.0")
    run = runtime.prepare("frozen-version", "owner", skill_ids=["report"])
    install("2.0")
    plugins.publish_local_plugin(
        {
            "slug": "reports",
            "components": {"skills": [{"id": "report", "version_constraint": ">=2"}]},
        },
        owner_user_id="owner",
    )
    plan = plugin_loader.resolve_desktop_progressive_plugins(
        user_id="owner", enabled_skill_ids=["report"], enabled_mcp_ids=[]
    )
    with pytest.raises(DependencyMissing):
        runtime.preflight(
            run, skill_ids=["report"], plugin_nodes=plan.directory[0].capability_nodes
        )
