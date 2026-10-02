from tests.sandbox.runner_client import run_runner

"""Regressions for desktop identity, explicit bindings and immutable runs."""


import pytest
from core.capabilities import registry, skills
from core.capabilities.ref import profile_id
from core.services import desktop_cloud_bridge as bridge
from core.services import desktop_cloud_skills as cloud_skills
from core.services.desktop_capability_protocol import build_skill_manifest


from tests.capabilities.runtime_recovery_support import (
    _ordered_skill_manifest,
    state,
    _zip,
    _intent,
    durable_index,
    _shell_id,
    _state_v2,
)


@pytest.mark.asyncio
async def test_concurrent_runs_in_same_chat_cannot_repoint_running_script(
    durable_index, caps_root, tmp_path, monkeypatch
):
    import asyncio
    from core.capabilities import runtime
    from core.services.desktop_capability_protocol import skill_content_hash
    from services.script_runner_service import server

    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    monkeypatch.setenv("DEPLOY_PROFILE", "local")
    monkeypatch.setattr(server, "WORKSPACE_ROOT", str(tmp_path / "workspace"))
    skills.publish_local_skill(
        "example", files={"SKILL.md": "v1"}, content_hash=skill_content_hash("v1", {})
    )
    old = runtime.prepare("concurrent-old", "u", skill_ids=["example"])
    skills.publish_local_skill(
        "example", files={"SKILL.md": "v2"}, content_hash=skill_content_hash("v2", {})
    )
    new = runtime.prepare("concurrent-new", "u", skill_ids=["example"])
    first = asyncio.create_task(
        run_runner(
            server.ProcessRequest(
                script_content=f"sleep 0.2; cat {old.view_dir}/example/SKILL.md",
                script_name="old.sh",
                language="bash",
                session_id="same-chat",
                user_id="u",
                capability_view_key=old.view_dir.parent.name,
            )
        )
    )
    await asyncio.sleep(0.05)
    second = await run_runner(
        server.ProcessRequest(
            script_content=f"cat {new.view_dir}/example/SKILL.md",
            script_name="new.sh",
            language="bash",
            session_id="same-chat",
            user_id="u",
            capability_view_key=new.view_dir.parent.name,
        )
    )
    before = await first
    assert (before.exit_code, before.stdout.strip()) == (0, "v1")
    assert (second.exit_code, second.stdout.strip()) == (0, "v2")


def test_multiple_failed_updates_keep_actual_resolved_hash(index_db, caps_root, monkeypatch):
    from core.services.desktop_capability_protocol import skill_content_hash

    st, inst = _intent(monkeypatch)
    monkeypatch.setattr(cloud_skills, "_download", lambda *_: _zip({"SKILL.md": "v1"}))
    cloud_skills.prepare_one(st, inst.install_id)
    _intent(monkeypatch, "v2")
    _, latest = _intent(monkeypatch, "v3")
    assert latest.content_hash == skill_content_hash("v3", {})
    assert latest.payload["resolved_content_hash"] == skill_content_hash("v1", {})


def test_device_disable_survives_cloud_manifest_refresh(index_db, caps_root, monkeypatch):
    st, inst = _intent(monkeypatch)
    registry.set_enabled(inst.install_id, False)
    from core.services.desktop_capability_protocol import skill_content_hash

    manifest = build_skill_manifest(
        [
            {
                "skill_id": "example",
                "display_name": "Example",
                "description": "",
                "version": "1",
                "scope": "shared",
                "content_hash": skill_content_hash("v1", {}),
                "mcp_server_ids": [],
                "enabled": True,
                "source_plugin": "",
            }
        ]
    )
    cloud_skills._reconcile_intent(_ordered_skill_manifest(st, manifest), st)
    assert not registry.get(inst.install_id).enabled


def test_mcp_public_environment_is_frozen_while_credentials_rotate(
    durable_index, caps_root, monkeypatch
):
    from core.capabilities import runtime

    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    run = runtime.prepare("env-run", "u", skill_ids=[])
    runtime.bind_mcp(
        run, {"local": {"command": "node", "env": {"MODE": "safe", "API_KEY": "old"}}}, None
    )
    runtime.bind_mcp(
        run, {"local": {"command": "node", "env": {"MODE": "safe", "API_KEY": "new"}}}, None
    )
    # 凭据轮换不算契约变化；公共环境变量一改就算，于是这个连接器被摘掉。
    changed = runtime.bind_mcp(
        run, {"local": {"command": "node", "env": {"MODE": "other", "API_KEY": "new"}}}, None
    )
    assert "local" not in changed
    assert runtime.get("env-run").unavailable["mcp:local"] == "connector_changed"


@pytest.mark.parametrize("extra", ["__pycache__/evil.pyc", ".git/hooks/evil", "large.bin"])
def test_full_package_hash_never_skips_hidden_or_executable_bytes(
    index_db, caps_root, monkeypatch, extra
):
    from core.capabilities import store
    from core.capabilities.errors import IntegrityFailed
    from core.capabilities.paths import revision_for_hash

    st, inst = _intent(monkeypatch)
    store.write_from_files(
        "skill",
        inst.profile_id,
        inst.key,
        revision_for_hash(inst.content_hash),
        {"SKILL.md": "v1", extra: b"unlisted extra"},
    )
    with pytest.raises(IntegrityFailed):
        cloud_skills.prepare_one(st, inst.install_id)


def test_full_package_hash_rejects_limit_instead_of_skipping(tmp_path, monkeypatch):
    from core.capabilities import archive
    from core.capabilities.errors import IntegrityFailed

    root = tmp_path / "package"
    root.mkdir()
    (root / "SKILL.md").write_text("v1")
    (root / "extra").write_bytes(b"123456789")
    monkeypatch.setattr(archive, "MAX_MEMBER_BYTES", 8)
    with pytest.raises(IntegrityFailed):
        skills.skill_dir_hash(root, fresh=True)


def test_expired_cloud_token_keeps_local_copy_identity_until_logout(
    durable_index, caps_root, monkeypatch
):
    from core.db.models import UserShadow
    from core.agent_skills.backends.capability_store import CapabilityStoreBackend
    from core.services.desktop_capability_protocol import skill_content_hash

    with durable_index() as db:
        UserShadow.__table__.create(db.get_bind(), checkfirst=True)
        db.add(UserShadow(user_id="local-a", username="A", user_center_id=_shell_id("center-a")))
        db.commit()
    now = [100.0]
    monkeypatch.setattr(bridge.time, "time", lambda: now[0])
    monkeypatch.setattr(bridge, "_purge_persisted_state", lambda: None)
    monkeypatch.setattr(bridge, "_rebuild_identity_views", lambda: None)
    monkeypatch.setattr(bridge, "_refresh_manifest_async", lambda **kwargs: None)
    skills.publish_local_skill(
        "copy",
        files={"SKILL.md": "copy"},
        content_hash=skill_content_hash("copy", {}),
        owner_user_id="local-a",
    )
    st = _state_v2("a")
    bridge.set_state(st["cloud_base"], st["token"], 1)
    now[0] += 2
    assert bridge.get_state() is None
    assert skills.current_account_profile() is None
    assert skills.current_local_user_id() == "local-a"
    assert CapabilityStoreBackend(local=True).exists("copy")
    bridge.clear_state()
    assert skills.current_local_user_id() is None
    assert not CapabilityStoreBackend(local=True).exists("copy")


@pytest.mark.asyncio
async def test_dedicated_agent_factory_reaches_config_after_freezing_definition(
    durable_index, caps_root, monkeypatch
):
    from core.llm import factory as agent_factory
    import prompts.prompt_config as prompt_config
    from core.capabilities.agents import AgentDefinition
    from core.capabilities import runtime

    monkeypatch.setattr(skills, "current_account_profile", lambda: None)
    monkeypatch.setattr(
        "core.llm.tool_permissions.resolve_approval_mode", lambda *args, **kwargs: "auto"
    )

    class ConfigurationReached(Exception):
        pass

    monkeypatch.setattr(
        prompt_config, "load_prompt_config", lambda: (_ for _ in ()).throw(ConfigurationReached())
    )
    definition = AgentDefinition(agent_id="dedicated", name="Dedicated", system_prompt="frozen")
    with pytest.raises(ConfigurationReached):
        await agent_factory.create_agent_executor(
            user_agent=definition,
            current_user_id="owner",
            run_id="dedicated-run",
            disable_tools=True,
        )
    changed = AgentDefinition(agent_id="dedicated", name="Dedicated", system_prompt="new")
    assert runtime.pin_agent_definition("dedicated-run", "owner", changed).system_prompt == "frozen"


def test_name_preferences_are_isolated_between_accounts(index_db, monkeypatch, tmp_path):
    from core.capabilities import connectors
    from core.capabilities.resolver import Candidate

    def candidate(iid):
        path = tmp_path / iid.rsplit(":", 1)[-1]
        path.mkdir()
        (path / "SKILL.md").write_text("---\nname: same\ndescription: Test\n---\nBody\n")
        return Candidate(
            install_id=iid,
            runtime_name="same",
            kind="skill",
            profile="local",
            source="local",
            path=path,
        )

    a, b = candidate("skill:local:a"), candidate("skill:local:b")
    monkeypatch.setattr(skills, "candidates", lambda uid: [a] if uid == "a" else [b])
    registry.set_preference("skill", "same", a.install_id, chosen_by="a")
    assert skills.resolve_for_user("b").chosen["same"].install_id == b.install_id
    registry.set_preference("skill", "same", b.install_id, chosen_by="b")
    assert skills.resolve_for_user("a").chosen["same"].install_id == a.install_id
    assert registry.preferences("skill") == {}
    assert registry.clear_preference("skill", "same", user_id="b")
    assert registry.preferences("skill", user_id="a") == {"same": a.install_id}
    registry.set_preference("mcp", "search", "mcp:local:a", chosen_by="a")
    monkeypatch.setattr(skills, "current_local_user_id", lambda: "b")
    cands = connectors.json_candidates({"search": {"enabled": True}})
    assert connectors.resolve_bindings(cands).chosen["search"].install_id == "mcp:local-json:search"


def test_legacy_name_preference_stays_with_its_owner(index_db):
    from core.db.models import DeviceCapabilityNamePreference

    with registry._session() as db:
        db.add(
            DeviceCapabilityNamePreference(
                preference_id="skill:same",
                kind="skill",
                runtime_name="same",
                chosen_install_id="skill:local:old",
                chosen_by="a",
            )
        )
    assert registry.preferences("skill", user_id="a") == {"same": "skill:local:old"}
    assert registry.preferences("skill", user_id="b") == {}
    registry.set_preference("skill", "same", "skill:local:new", chosen_by="a")
    assert registry.preferences("skill", user_id="a") == {"same": "skill:local:new"}
    assert not registry.clear_preference("skill", "same", user_id="b")
    assert registry.clear_preference("skill", "same", user_id="a")
    assert registry.preferences("skill", user_id="a") == {}


def test_enabled_cloud_connector_without_schema_is_unusable(index_db, monkeypatch):
    from core.capabilities import connectors
    from core.capabilities.errors import PackageMissing

    st = state("a")
    ctx = {
        "state": st,
        "profile": profile_id(st["cloud_base"], "a"),
        "manifest_revision": "r1",
        "servers": [
            {"server_id": "empty", "component": "empty", "tools": [], "schema_hash": "hash"}
        ],
    }
    empty = connectors.cloud_candidates(ctx["profile"], ctx["servers"], {})[0]
    assert not empty.usable and empty.state == "schema_empty"
    disabled = connectors.cloud_candidates(ctx["profile"], ctx["servers"], {"empty": False})[0]
    assert not disabled.usable and disabled.state == "disabled"
    monkeypatch.setattr(bridge, "_bridge_context", lambda: ctx)
    monkeypatch.setattr(bridge, "_local_server_ids", lambda: set())
    monkeypatch.setattr(bridge, "_mcp_json_local_declarations", lambda: {})
    monkeypatch.setattr(bridge, "_mcp_json_local_configs", lambda: {})
    with pytest.raises(PackageMissing):
        bridge.cloud_gateway_mcp_configs(["empty"])
    unavailable = {}
    assert bridge.cloud_gateway_mcp_configs(["empty"], unavailable_out=unavailable) == {}
    assert unavailable == {"empty": PackageMissing.code}
