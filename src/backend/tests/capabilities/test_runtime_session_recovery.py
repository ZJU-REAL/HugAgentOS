
"""Regressions for desktop identity, explicit bindings and immutable runs."""


import pytest
from core.capabilities import registry, skills
from core.capabilities.ref import cloud_ref, profile_id
from core.services import desktop_cloud_bridge as bridge
from core.services import desktop_cloud_skills as cloud_skills
from core.services import desktop_cloud_bundles as bundles


from tests.capabilities.runtime_recovery_support import durable_index, _shell_id, _state_v2


def test_switch_rebuild_never_puts_new_account_in_old_users_view(
    durable_index, caps_root, tmp_path, monkeypatch
):
    from core.capabilities import store
    from core.db.models import UserShadow
    from core.services.desktop_capability_protocol import skill_content_hash
    from core.capabilities.paths import revision_for_hash

    with durable_index() as db:
        UserShadow.__table__.create(db.get_bind(), checkfirst=True)
        db.add_all(
            [
                UserShadow(user_id="local-a", username="A", user_center_id=_shell_id("center-a")),
                UserShadow(user_id="local-b", username="B", user_center_id=_shell_id("center-b")),
            ]
        )
        db.commit()
    current = [_state_v2("a")]
    monkeypatch.setattr(bridge, "get_state", lambda: current[0])
    monkeypatch.setattr(
        bridge,
        "get_identity_state",
        lambda: {
            "user_center_id": "center-"
            + ("a" if current[0]["token"] == _state_v2("a")["token"] else "b"),
            "shell_user_center_id": _shell_id(
                "center-" + ("a" if current[0]["token"] == _state_v2("a")["token"] else "b")
            ),
        },
    )
    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    monkeypatch.setattr(skills, "device_view_dir", lambda: tmp_path / "shared" / "skills")
    monkeypatch.setattr(skills, "user_view_dir", lambda uid: tmp_path / "shared" / "skills_u" / uid)
    monkeypatch.setattr("core.agent_skills.cache_refresh.refresh_skill_caches", lambda: None)
    for uid in ("a", "b"):
        profile = profile_id("https://cloud.example", "cloud-" + uid)
        digest = skill_content_hash("private-" + uid, {})
        inst = registry.upsert(
            profile_id=profile,
            ref=cloud_ref("https://cloud.example", "skill", "private", scope="private"),
            content_hash=digest,
        )
        comp = store.write_from_files(
            "skill", profile, "private", revision_for_hash(digest), {"SKILL.md": "private-" + uid}
        )
        registry.set_state(inst.install_id, "ready", resolved_revision=comp.revision)
    skills.rebuild_user_view("local-a")
    old = skills.user_view_dir("local-a")
    assert (old / "private" / "SKILL.md").read_text() == "private-a"
    current[0] = _state_v2("b")
    bridge._rebuild_identity_views()
    assert not (old / "private").exists()
    skills.rebuild_user_view("local-b")
    assert (skills.user_view_dir("local-b") / "private" / "SKILL.md").read_text() == "private-b"


def test_ensure_cloud_ready_downloads_only_unready_cloud_components(
    durable_index, caps_root, monkeypatch
):
    """对话里选中尚未下载的云端技能 / 插件时按需准备：只对当前账号未就绪的记录调下载，
    插件定义就绪后再准备它的组件；已就绪的与别的账号的记录不碰。"""
    from core.capabilities.preparation import ensure_cloud_ready
    from core.services import desktop_cloud_bundles, desktop_cloud_skills

    current = [_state_v2("a")]
    monkeypatch.setattr(bridge, "get_state", lambda: current[0])
    monkeypatch.setattr(skills, "account_authorized_for", lambda uid: uid == "local-a")
    profile = profile_id("https://cloud.example", "cloud-a")
    other = profile_id("https://cloud.example", "cloud-b")
    pending_skill = registry.upsert(
        profile_id=profile,
        ref=cloud_ref("https://cloud.example", "skill", "pdf-editing", scope="shared"),
        content_hash="a" * 64,
    )
    ready_skill = registry.upsert(
        profile_id=profile,
        ref=cloud_ref("https://cloud.example", "skill", "ready", scope="shared"),
        content_hash="b" * 64,
    )
    registry.set_state(ready_skill.install_id, "ready", resolved_revision="rev-b")
    foreign = registry.upsert(
        profile_id=other,
        ref=cloud_ref("https://cloud.example", "skill", "pdf-editing", scope="shared"),
        content_hash="c" * 64,
    )
    plugin = registry.upsert(
        profile_id=profile,
        ref=cloud_ref("https://cloud.example", "plugin", "knowledge", scope="shared"),
        content_hash="d" * 64,
    )
    component = registry.upsert(
        profile_id=profile,
        ref=cloud_ref("https://cloud.example", "skill", "knowledge-daily", scope="shared"),
        content_hash="e" * 64,
    )
    prepared = {"skills": [], "definitions": []}

    def fake_bundles(state, install_ids):
        prepared["definitions"].append(list(install_ids))
        for iid in install_ids:
            registry.set_state(iid, "ready", resolved_revision="rev")
            registry.set_components(iid, {component.install_id: True})
        return [{"install_id": iid, "ok": True} for iid in install_ids]

    def fake_skills(state, install_ids):
        prepared["skills"].append(list(install_ids))
        for iid in install_ids:
            registry.set_state(iid, "ready", resolved_revision="rev")
        return [{"install_id": iid, "ok": True} for iid in install_ids]

    monkeypatch.setattr(desktop_cloud_bundles, "prepare", fake_bundles)
    monkeypatch.setattr(desktop_cloud_skills, "prepare", fake_skills)

    assert ensure_cloud_ready("local-b", skill_keys=["pdf-editing"]) == []
    assert prepared == {"skills": [], "definitions": []}

    assert (
        ensure_cloud_ready(
            "local-a", skill_keys=["pdf-editing", "ready"], plugin_keys=["knowledge"]
        )
        == []
    )
    assert prepared["definitions"] == [[plugin.install_id]]
    assert sorted(prepared["skills"][0]) == sorted([pending_skill.install_id, component.install_id])
    assert registry.get(foreign.install_id).state != "ready"
    assert registry.get(pending_skill.install_id).ready and registry.get(component.install_id).ready


def test_prepared_run_rejects_new_session_of_the_same_account(
    durable_index, caps_root, monkeypatch
):
    from core.capabilities import runtime
    from core.capabilities.errors import PermissionDenied

    current = [_state_v2("a")]
    monkeypatch.setattr(bridge, "get_state", lambda: current[0])
    monkeypatch.setattr(skills, "builtin_candidates", lambda: [])
    from pathlib import Path
    from core.capabilities.resolver import Candidate, Resolution

    monkeypatch.setattr(skills, "resolve_for_user", lambda _: Resolution())
    monkeypatch.setattr(skills, "current_local_user_id", lambda: "local-a")
    monkeypatch.setattr(bridge, "ensure_current_authorization", lambda *_: None)
    config = {"search": {"url": "https://cloud.example/gateway/search"}}
    monkeypatch.setattr(bridge, "cloud_gateway_mcp_configs", lambda *_: config)
    run = runtime.prepare("session-run", "local-a", skill_ids=[])
    assert run.profile is None
    profile = skills.current_account_profile()
    choice = Candidate(
        install_id="mcp:" + profile + ":search",
        runtime_name="search",
        kind="mcp",
        profile=profile,
        source="cloud",
        path=Path("<cloud>"),
    )
    runtime.bind_mcp(run, config, Resolution(chosen={"search": choice}))
    run = runtime.get("session-run")
    assert run.profile == profile
    current[0] = _state_v2("a", nonce="rotated")
    runtime.validate(run)
    current[0] = _state_v2("a", epoch=2)
    with pytest.raises(PermissionDenied):
        runtime.validate(run)


def test_authorization_check_makes_no_network_call(index_db, caps_root, monkeypatch):
    """装配前的授权判定不发网络请求——同步只由登录和变更信号驱动，没有事前探测。"""
    current = [_state_v2("a")]
    monkeypatch.setattr(bridge, "get_state", lambda: current[0])

    def forbidden(*args, **kwargs):
        raise AssertionError("authorization must not probe the cloud")

    monkeypatch.setattr("httpx.get", forbidden)
    for _ in range(3):
        bridge.ensure_current_authorization()
    assert current[0] is not None


@pytest.mark.asyncio
async def test_revoked_grant_is_decided_by_the_gateway_call(index_db, caps_root, monkeypatch):
    """撤权由云端在真实网关调用时裁决：401 立刻清掉本机的桥状态。"""
    import httpx
    import mcp.types
    from core.llm.mcp_manager import GatewayMCPTool

    current = [_state_v2("a")]
    monkeypatch.setattr(bridge, "get_state", lambda: current[0])
    monkeypatch.setattr(bridge, "clear_state", lambda: current.__setitem__(0, None))
    tool = GatewayMCPTool(
        mcp_name="search",
        tool=mcp.types.Tool(name="search", inputSchema={"type": "object"}),
        invoke_url="https://cloud.example/api/v1/desktop/capability/gateway/search/call",
        schema_hash="frozen",
        headers=bridge.cloud_headers(current[0]),
        timeout=5,
        transport=httpx.MockTransport(lambda request: httpx.Response(401, json={})),
    )
    with pytest.raises(RuntimeError):
        await tool()
    assert current[0] is None


@pytest.mark.asyncio
async def test_running_gateway_uses_rotated_token_but_rejects_new_account(
    index_db, caps_root, monkeypatch
):
    import httpx
    import mcp.types
    from core.llm.mcp_manager import GatewayMCPTool
    from core.capabilities.errors import CloudUnavailable

    current = [_state_v2("a")]
    monkeypatch.setattr(bridge, "get_state", lambda: current[0])
    seen = []

    def handler(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"data": {"content": [{"type": "text", "text": "ok"}]}})

    tool = GatewayMCPTool(
        mcp_name="search",
        tool=mcp.types.Tool(name="search", inputSchema={"type": "object"}),
        invoke_url="https://cloud.example/api/v1/desktop/capability/gateway/search/call",
        schema_hash="frozen",
        headers=bridge.cloud_headers(current[0]),
        timeout=5,
        transport=httpx.MockTransport(handler),
    )
    current[0] = _state_v2("a", nonce="rotated")
    await tool()
    assert seen == ["Bearer " + current[0]["token"]]
    current[0] = _state_v2("b")
    with pytest.raises(CloudUnavailable):
        await tool()
    assert len(seen) == 1


def test_missing_explicit_source_never_falls_back(index_db):
    from pathlib import Path
    from core.capabilities.resolver import Candidate, resolve

    builtin = Candidate(
        install_id="skill:builtin:example",
        runtime_name="example",
        kind="skill",
        profile="builtin",
        source="builtin",
        path=Path("/tmp/unused"),
    )
    result = resolve("skill", [builtin], preferences={"example": "skill:p_old:example"})
    assert not result.chosen and result.reasons["example"] == "preferred_missing"
