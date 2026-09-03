import asyncio
from types import SimpleNamespace

import pytest
from api.routes.v1.chats import _resolve_explicit_capability_invocation
from api.schemas import ChatRequest
from core.config import catalog_resolver
from core.db.models import (
    AdminMcpServer,
    AdminSkill,
    CatalogOverride,
    InstalledPlugin,
    UserShadow,
)
from core.llm.middlewares import (
    ExplicitPluginInvocationError,
    ExplicitPluginToolChoiceMiddleware,
    ExplicitSkillInvocationError,
    ExplicitSkillToolChoiceMiddleware,
)
from core.services import plugin_service
from fastapi import HTTPException
from orchestration.workflow import _build_skill_injection


def _runtime_catalog(*_args, **_kwargs):
    return {
        "skills": [
            {"id": "public-skill", "enabled": True},
            {"id": "admin-disabled-skill", "enabled": False},
        ],
        "mcp": [
            {"id": "public-mcp", "enabled": True},
            {"id": "admin-disabled-mcp", "enabled": False},
        ],
    }


def test_explicit_resolution_ignores_personal_switch_but_keeps_hard_boundaries(
    db_session, monkeypatch
):
    monkeypatch.setattr(catalog_resolver, "get_runtime_catalog", _runtime_catalog)
    db_session.add(UserShadow(user_id="user-a", username="User A"))
    db_session.add_all(
        [
            CatalogOverride(user_id="user-a", kind="skill", item_id="public-skill", enabled=False),
            CatalogOverride(user_id="user-a", kind="mcp", item_id="public-mcp", enabled=False),
            AdminSkill(
                skill_id="private-skill",
                skill_content="# Private",
                display_name="Private",
                description="Private",
                owner_user_id="user-a",
                is_enabled=True,
                dep_status="ready",
            ),
            AdminSkill(
                skill_id="foreign-skill",
                skill_content="# Foreign",
                display_name="Foreign",
                description="Foreign",
                owner_user_id="user-b",
                is_enabled=True,
                dep_status="ready",
            ),
            AdminMcpServer(
                server_id="private-mcp",
                display_name="Private MCP",
                owner_user_id="user-a",
                is_enabled=True,
            ),
        ]
    )
    db_session.commit()

    allowed_skills, allowed_mcps, unavailable_skills, unavailable_mcps = (
        catalog_resolver.resolve_explicit_runtime_capabilities(
            db_session,
            "user-a",
            skill_ids=[
                "public-skill",
                "private-skill",
                "admin-disabled-skill",
                "foreign-skill",
            ],
            mcp_ids=["public-mcp", "private-mcp", "admin-disabled-mcp"],
        )
    )

    assert allowed_skills == ["public-skill", "private-skill"]
    assert allowed_mcps == ["public-mcp", "private-mcp"]
    assert unavailable_skills == ["admin-disabled-skill", "foreign-skill"]
    assert unavailable_mcps == ["admin-disabled-mcp"]


def test_plugin_id_is_authoritative_and_unavailable_components_are_skipped(db_session, monkeypatch):
    monkeypatch.setattr(catalog_resolver, "get_runtime_catalog", _runtime_catalog)
    db_session.add(
        InstalledPlugin(
            install_id="office@user-a",
            slug="office",
            name="办公插件",
            owner_user_id="user-a",
            component_ids={
                "skills": ["public-skill", "admin-disabled-skill"],
                "mcp": ["public-mcp", "admin-disabled-mcp"],
            },
        )
    )
    db_session.commit()
    request = ChatRequest(
        chat_id="chat-1",
        message="处理材料",
        plugin_id="office@user-a",
        plugin_name="伪造名称",
    )

    resolved = _resolve_explicit_capability_invocation(db_session, request, "user-a")

    assert resolved.plugin_name == "办公插件"
    assert resolved._resolved_skill_ids == ["public-skill"]
    assert resolved._resolved_mcp_ids == ["public-mcp"]
    assert resolved._resolved_plugin_skill_ids == ["public-skill"]
    assert resolved._resolved_plugin_mcp_ids == ["public-mcp"]


def test_explicit_plugin_middleware_forces_only_plugin_capabilities():
    middleware = ExplicitPluginToolChoiceMiddleware(
        plugin_id="security@user-a",
        plugin_name="安全管理",
        skill_ids=["security-overview"],
        mcp_tool_names=["inspect_security_state"],
    )
    captured = {}

    async def reasoning_next(**kwargs):
        captured.update(kwargs)
        if False:
            yield None

    async def acting_next(**kwargs):
        yield {"tool_result": "ok"}

    async def run():
        async for _ in middleware.on_reasoning(None, {}, reasoning_next):
            pass
        async for _ in middleware.on_acting(
            None,
            {
                "tool_call": SimpleNamespace(
                    name="view_text_file",
                    input=('{"file_path":"/workspace/skills/' 'security-overview/SKILL.md"}'),
                )
            },
            acting_next,
        ):
            pass

    asyncio.run(run())

    choice = captured["tool_choice"]
    assert choice.mode == "required"
    assert choice.tools == ["inspect_security_state", "view_text_file"]
    assert middleware._satisfied is True


def test_explicit_plugin_middleware_rejects_unrelated_skill_read():
    middleware = ExplicitPluginToolChoiceMiddleware(
        plugin_id="security@user-a",
        plugin_name="安全管理",
        skill_ids=["security-overview"],
        mcp_tool_names=[],
    )

    async def acting_next(**kwargs):
        yield {"tool_result": "ok"}

    async def run():
        async for _ in middleware.on_acting(
            None,
            {
                "tool_call": SimpleNamespace(
                    name="view_text_file",
                    input='{"file_path":"/workspace/skills/other/SKILL.md"}',
                )
            },
            acting_next,
        ):
            pass

    asyncio.run(run())

    assert middleware._satisfied is False


def test_explicit_plugin_middleware_accepts_its_mcp_tool():
    middleware = ExplicitPluginToolChoiceMiddleware(
        plugin_id="security@user-a",
        plugin_name="安全管理",
        skill_ids=[],
        mcp_tool_names=["inspect_security_state"],
    )

    async def acting_next(**kwargs):
        yield {"tool_result": "ok"}

    async def run():
        async for _ in middleware.on_acting(
            None,
            {"tool_call": SimpleNamespace(name="inspect_security_state", input="{}")},
            acting_next,
        ):
            pass

    asyncio.run(run())

    assert middleware._satisfied is True


def test_explicit_plugin_middleware_fails_closed_without_real_use():
    middleware = ExplicitPluginToolChoiceMiddleware(
        plugin_id="security@user-a",
        plugin_name="安全管理",
        skill_ids=["security-overview"],
        mcp_tool_names=[],
    )

    async def reply_next(**kwargs):
        yield "model answered without plugin usage"

    async def run():
        async for _ in middleware.on_reply(None, {}, reply_next):
            pass

    with pytest.raises(ExplicitPluginInvocationError, match="未实际读取其技能"):
        asyncio.run(run())


def test_explicit_plugin_injection_declares_mandatory_real_use():
    hint = _build_skill_injection(
        {
            "mcp_ids": ["security-mcp"],
            "plugin_name": "安全管理",
        }
    )

    assert hint is not None
    assert "这不是可忽略的偏好" in hint["content"]
    assert "不得跳过插件能力直接回答" in hint["content"]


def test_explicit_skill_middleware_requires_the_exact_skill_file():
    middleware = ExplicitSkillToolChoiceMiddleware(
        skill_id="security-overview",
        skill_name="安全概览",
    )
    captured = {}

    async def reasoning_next(**kwargs):
        captured.update(kwargs)
        if False:
            yield None

    async def acting_next(**kwargs):
        yield {"tool_result": "ok"}

    async def run():
        async for _ in middleware.on_reasoning(None, {}, reasoning_next):
            pass
        async for _ in middleware.on_acting(
            None,
            {
                "tool_call": SimpleNamespace(
                    name="view_text_file",
                    input=('{"file_path":"/workspace/skills/' 'security-overview/SKILL.md"}'),
                )
            },
            acting_next,
        ):
            pass

    asyncio.run(run())

    assert captured["tool_choice"].tools == ["view_text_file"]
    assert middleware._satisfied is True


def test_explicit_skill_middleware_does_not_accept_another_skill():
    middleware = ExplicitSkillToolChoiceMiddleware(
        skill_id="security-overview",
        skill_name="安全概览",
    )

    async def acting_next(**kwargs):
        yield {"tool_result": "ok"}

    async def run():
        async for _ in middleware.on_acting(
            None,
            {
                "tool_call": SimpleNamespace(
                    name="view_text_file",
                    input='{"file_path":"/workspace/skills/other/SKILL.md"}',
                )
            },
            acting_next,
        ):
            pass

    asyncio.run(run())

    assert middleware._satisfied is False


def test_explicit_skill_middleware_fails_closed_without_loading():
    middleware = ExplicitSkillToolChoiceMiddleware(
        skill_id="security-overview",
        skill_name="安全概览",
    )

    async def reply_next(**kwargs):
        yield "model answered without loading the skill"

    async def run():
        async for _ in middleware.on_reply(None, {}, reply_next):
            pass

    with pytest.raises(ExplicitSkillInvocationError, match="未实际读取其 SKILL.md"):
        asyncio.run(run())


def test_explicit_skill_injection_declares_mandatory_load(monkeypatch):
    import core.agent_skills.loader as skill_loader

    fake_loader = SimpleNamespace(
        load_all_metadata=lambda: {"security-overview": SimpleNamespace(name="安全概览")},
        get_skill_dir=lambda _skill_id: "/tmp/security-overview",
    )
    monkeypatch.setattr(skill_loader, "get_skill_loader", lambda: fake_loader)
    hint = _build_skill_injection(
        {
            "skill_id": "security-overview",
            "skill_name": "安全概览",
        }
    )

    assert hint is not None
    assert "这不是可忽略的偏好" in hint["content"]
    assert "回答前必须先读取该技能的 SKILL.md" in hint["content"]


@pytest.mark.parametrize("field_name", ["skill_ids", "mcp_ids"])
def test_legacy_client_expanded_plugin_components_are_rejected(field_name):
    with pytest.raises(ValueError, match="client-expanded capability fields are not supported"):
        ChatRequest(
            chat_id="chat-1",
            message="处理材料",
            plugin_name="旧版插件",
            **{field_name: ["client-declared-component"]},
        )


def test_plugin_display_name_without_installation_id_is_rejected():
    with pytest.raises(ValueError, match="plugin_id is required"):
        ChatRequest(chat_id="chat-1", message="处理材料", plugin_name="旧版插件")


def test_foreign_plugin_install_cannot_be_invoked(db_session, monkeypatch):
    monkeypatch.setattr(catalog_resolver, "get_runtime_catalog", _runtime_catalog)
    db_session.add(
        InstalledPlugin(
            install_id="office@user-b",
            slug="office",
            name="办公插件",
            owner_user_id="user-b",
            component_ids={"skills": ["public-skill"]},
        )
    )
    db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        _resolve_explicit_capability_invocation(
            db_session,
            ChatRequest(
                chat_id="chat-1",
                message="处理材料",
                plugin_id="office@user-b",
                plugin_name="办公插件",
            ),
            "user-a",
        )

    assert exc_info.value.status_code == 403


def test_installed_plugin_distinguishes_personal_enabled_from_hard_callability(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        catalog_resolver,
        "resolve_all_runtime_enabled",
        lambda *_args, **_kwargs: ([], [], []),
    )
    db_session.add_all(
        [
            AdminSkill(
                skill_id="callable-plugin-skill",
                skill_content="# Callable",
                display_name="Callable",
                description="Callable",
                owner_user_id=None,
                source_plugin="callable-plugin",
                is_enabled=True,
                dep_status="ready",
            ),
            AdminSkill(
                skill_id="blocked-plugin-skill",
                skill_content="# Blocked",
                display_name="Blocked",
                description="Blocked",
                owner_user_id=None,
                source_plugin="blocked-plugin",
                is_enabled=False,
                dep_status="ready",
            ),
            InstalledPlugin(
                install_id="callable-plugin@global",
                slug="callable-plugin",
                name="Callable plugin",
                owner_user_id=None,
                component_ids={"skills": ["callable-plugin-skill"]},
            ),
            InstalledPlugin(
                install_id="blocked-plugin@global",
                slug="blocked-plugin",
                name="Blocked plugin",
                owner_user_id=None,
                component_ids={"skills": ["blocked-plugin-skill"]},
            ),
        ]
    )
    db_session.commit()

    items = plugin_service.list_installed(
        db_session,
        owner_user_id="user-a",
        include_global=True,
    )
    by_id = {item["install_id"]: item for item in items}

    assert by_id["callable-plugin@global"]["enabled"] is False
    assert by_id["callable-plugin@global"]["callable"] is True
    assert by_id["blocked-plugin@global"]["callable"] is False
