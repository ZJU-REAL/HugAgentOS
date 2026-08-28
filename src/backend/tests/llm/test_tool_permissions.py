"""Declarative tool-permission registry and pre-dispatch enforcement."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from agentscope.message import ToolCallBlock, ToolResultState
from agentscope.state import AgentState
from agentscope.tool._response import ToolChunk, ToolResponse
from core.llm.tool_collector import ToolCollector
from core.llm.tool_permissions import (
    CURRENT_PERMISSION_TICKET,
    PermissionRuntime,
    ToolPermissionMiddleware,
    ToolPermissionRegistry,
    ToolPermissionService,
    allow_tool,
    local_command_tool,
    local_path_tool,
    mcp_tool_permission,
)
from core.ontology.toolkit import OntologyFilteredToolkit
from core.sandbox.local_policy import Grant, Policy


def _tool_call(name: str, arguments: dict, call_id: str = "tc-1") -> ToolCallBlock:
    return ToolCallBlock(
        id=call_id,
        name=name,
        input=json.dumps(arguments, ensure_ascii=False),
    )


def _payload(response: ToolResponse) -> dict:
    block = response.content[0]
    text = block["text"] if isinstance(block, dict) else block.text
    return json.loads(text)


def test_registry_treats_missing_as_normal_and_rejects_conflicts():
    registry = ToolPermissionRegistry()
    assert registry.get("new_tool") is None

    registry.register("Read", local_path_tool("file_path", "read"), source="native")
    with pytest.raises(ValueError, match="conflicting permission declarations"):
        registry.register("Read", allow_tool(), source="other")


def test_native_tool_registration_does_not_require_a_declaration():
    async def brand_new_tool() -> ToolResponse:
        return ToolResponse()

    collector = ToolCollector()
    collector.register_tool_function(brand_new_tool)
    assert "brand_new_tool" not in collector.permission_specs

    collector.register_tool_function(brand_new_tool, permission=allow_tool())
    assert collector.permission_specs["brand_new_tool"].key == "allow"


def test_mcp_unknown_server_stays_outside_registry_by_default():
    spec = mcp_tool_permission("private_server", "do_something", {})
    assert spec is None


@pytest.mark.asyncio
async def test_unregistered_tool_passes_without_a_ticket():
    service = ToolPermissionService(
        ToolPermissionRegistry(),
        PermissionRuntime(
            chat_id=None,
            user_id="user-1",
            interactive=False,
            approval_available=False,
        ),
    )

    outcome = await service.authorize(_tool_call("brand_new_tool", {"value": 1}))

    assert outcome.proceed is True
    assert outcome.ticket is None
    assert outcome.audit["decision"] == "allow_unregistered"


@pytest.mark.asyncio
async def test_middleware_dispatches_unregistered_tool_without_binding_ticket():
    middleware = ToolPermissionMiddleware(
        ToolPermissionService(
            ToolPermissionRegistry(),
            PermissionRuntime(
                chat_id=None,
                user_id="user-1",
                interactive=False,
                approval_available=False,
            ),
        )
    )
    call = _tool_call("brand_new_tool", {"value": 1})
    seen = []

    async def handler(**_kwargs):
        seen.append(CURRENT_PERMISSION_TICKET.get())
        yield ToolResponse(state=ToolResultState.SUCCESS)

    events = [
        item
        async for item in middleware.on_acting(
            SimpleNamespace(state=SimpleNamespace(run_id="run-1")),
            {"tool_call": call},
            handler,
        )
    ]

    assert seen == [None]
    assert events[0].metadata["permission"]["decision"] == "allow_unregistered"


@pytest.mark.parametrize("behavior", ["allow", "confirm", "deny"])
def test_mcp_config_is_ignored_while_the_whitelist_policy_is_active(behavior):
    assert (
        mcp_tool_permission(
            "private_server",
            "mutate",
            {"tool_permissions": {"mutate": behavior}},
        )
        is None
    )


@pytest.mark.asyncio
async def test_mcp_mutation_is_unregistered_and_passes_through():
    registry = ToolPermissionRegistry()
    runtime = PermissionRuntime(
        chat_id="chat-1",
        user_id="user-1",
        interactive=False,
        approval_available=False,
    )
    service = ToolPermissionService(registry, runtime)

    outcome = await service.authorize(_tool_call("install_plugin", {"slug": "daily-report"}))

    assert outcome.proceed is True
    assert outcome.ticket is None
    assert outcome.audit["decision"] == "allow_unregistered"


@pytest.mark.asyncio
async def test_local_path_decision_is_resolved_once_by_generic_service():
    registry = ToolPermissionRegistry()
    registry.register("Read", local_path_tool("file_path", "read"), source="native")
    service = ToolPermissionService(
        registry,
        PermissionRuntime(
            chat_id="chat-1",
            user_id="user-1",
            interactive=True,
            approval_available=True,
        ),
    )
    with (
        patch("core.config.local_mode.local_mode_enabled", return_value=True),
        patch(
            "core.services.local_grant_service.grants_for_gate",
            return_value=[Grant("/data/project", "read")],
        ),
        patch(
            "core.services.local_grant_service.policy_for_gate",
            return_value=Policy(),
        ),
    ):
        outcome = await service.authorize(_tool_call("Read", {"file_path": "/data/project/a.txt"}))

    assert outcome.proceed is True
    assert outcome.ticket is not None
    assert outcome.ticket.authorizes_path("/data/project/a.txt", "read")


@pytest.mark.asyncio
async def test_middleware_binds_ticket_only_around_matching_dispatch():
    registry = ToolPermissionRegistry()
    registry.register("safe", allow_tool(), source="native")
    middleware = ToolPermissionMiddleware(
        ToolPermissionService(
            registry,
            PermissionRuntime(
                chat_id="chat-1",
                user_id="user-1",
                interactive=True,
                approval_available=True,
            ),
        )
    )
    call = _tool_call("safe", {"value": 1})
    seen = []

    async def handler(**_kwargs):
        seen.append(CURRENT_PERMISSION_TICKET.get())
        yield ToolResponse(state=ToolResultState.SUCCESS)

    agent = SimpleNamespace(state=SimpleNamespace(run_id="run-1"))
    events = [
        item
        async for item in middleware.on_acting(
            agent,
            {"tool_call": call},
            handler,
        )
    ]

    assert len(events) == 1
    assert seen[0] is not None and seen[0].matches(call)
    assert CURRENT_PERMISSION_TICKET.get() is None
    assert events[0].metadata["permission"]["decision"] == "allow"


@pytest.mark.asyncio
async def test_toolkit_dispatch_guard_rejects_calls_without_a_ticket():
    registry = ToolPermissionRegistry()
    registry.register("safe", allow_tool(), source="native")
    service = ToolPermissionService(
        registry,
        PermissionRuntime(
            chat_id="chat-1",
            user_id="user-1",
            interactive=True,
            approval_available=True,
        ),
    )
    toolkit = OntologyFilteredToolkit()
    toolkit.set_tool_permission_service(service)

    events = [
        item
        async for item in toolkit.call_tool(
            _tool_call("safe", {}),
            SimpleNamespace(),
        )
    ]

    assert events[-1].state == ToolResultState.ERROR
    assert "PermissionEnforcementError" in events[-1].content[0].text


@pytest.mark.asyncio
async def test_toolkit_dispatch_guard_does_not_require_ticket_for_unregistered_tool():
    async def brand_new_tool(value: int) -> ToolChunk:
        return ToolChunk(
            content=[{"type": "text", "text": str(value)}],
            state=ToolResultState.SUCCESS,
        )

    collector = ToolCollector()
    collector.register_tool_function(brand_new_tool)
    toolkit = OntologyFilteredToolkit(tools=collector.function_tools)
    toolkit.set_tool_permission_service(
        ToolPermissionService(
            ToolPermissionRegistry(),
            PermissionRuntime(
                chat_id=None,
                user_id="user-1",
                interactive=False,
                approval_available=False,
            ),
        )
    )

    events = [
        item
        async for item in toolkit.call_tool(
            _tool_call("brand_new_tool", {"value": 7}),
            AgentState(),
        )
    ]

    assert events[-1].state == ToolResultState.SUCCESS
    assert "PermissionEnforcementError" not in str(events[-1].content)


@pytest.mark.asyncio
async def test_local_command_ticket_carries_os_sandbox_constraints():
    registry = ToolPermissionRegistry()
    registry.register("bash", local_command_tool(), source="native")
    service = ToolPermissionService(
        registry,
        PermissionRuntime(
            chat_id="chat-1",
            user_id="user-1",
            interactive=True,
            approval_available=True,
        ),
    )
    with (
        patch("core.config.local_mode.local_mode_enabled", return_value=True),
        patch(
            "core.services.local_grant_service.grants_for_gate",
            return_value=[Grant("/data/project", "readwrite")],
        ),
        patch(
            "core.services.local_grant_service.policy_for_gate",
            return_value=Policy(),
        ),
        patch(
            "core.services.local_grant_service.get_approval_mode",
            return_value="standard",
        ),
    ):
        outcome = await service.authorize(_tool_call("bash", {"command": "ls"}))

    assert outcome.proceed is True
    assert outcome.ticket is not None
    command = outcome.ticket.local_command
    assert command is not None
    assert command.approval_mode == "standard"
    assert "/data/project" in command.write_paths


@pytest.mark.asyncio
async def test_resolver_failure_is_denied_instead_of_failing_open():
    registry = ToolPermissionRegistry()
    registry.register("bash", local_command_tool(), source="native")
    service = ToolPermissionService(
        registry,
        PermissionRuntime(
            chat_id="chat-1",
            user_id="user-1",
            interactive=True,
            approval_available=True,
        ),
    )
    registry._specs["bash"] = registry._specs["bash"].with_resolver(  # noqa: SLF001
        lambda _args, _runtime: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    outcome = await service.authorize(_tool_call("bash", {"command": "ls"}))

    assert outcome.proceed is False
    assert outcome.payload["blocked"] is True
    assert "权限声明解析失败" in outcome.payload["error"]
