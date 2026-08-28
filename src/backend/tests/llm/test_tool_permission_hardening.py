"""Regression coverage for the tool-permission hardening pass.

Each test pins one way the declarative gateway could silently stop governing
something: a second spelling of a governed path, an omitted optional argument,
a trusted unattended run, or an MCP tool accidentally entering the registry.
"""

from __future__ import annotations

import pytest
from agentscope.message import ToolCallBlock
from core.llm.agent_factory import _default_allow_builtin_tools
from core.llm.tool_permissions import (
    DOMAIN_LOCAL_COMMAND,
    DOMAIN_LOCAL_PATH,
    DOMAIN_MYSPACE,
    PermissionEnforcementError,
    PermissionRuntime,
    ToolPermissionRegistry,
    ToolPermissionService,
    builtin_tool_permission,
    mcp_tool_permission,
    register_mcp_client_permissions,
)


def _runtime(**overrides) -> PermissionRuntime:
    base = dict(
        chat_id="chat-1",
        user_id="user-1",
        interactive=True,
        approval_available=True,
    )
    base.update(overrides)
    return PermissionRuntime(**base)


def _tool_call(name: str, arguments: dict) -> ToolCallBlock:
    import json

    return ToolCallBlock(type="tool_call", id="tc-1", name=name, input=json.dumps(arguments))


# ── My Space: both spellings of the same destination are governed ────────────


@pytest.mark.parametrize(
    "tool_name,path_arg",
    [
        ("Write", "file_path"),
        ("Edit", "file_path"),
        ("Delete", "path"),
        ("Move", "src_path"),
        ("CreateFolder", "path"),
    ],
)
def test_physical_myspace_path_still_requires_the_myspace_confirmation(tool_name, path_arg):
    """``/workspace/myspace/<uid>/…`` must not skip the persistence gate.

    Both spellings reach the same cross-session storage and both reverse-sync
    to the artifact table, so judging on the raw argument prefix alone left a
    silent bypass of the confirmation.
    """
    spec = builtin_tool_permission(tool_name)
    args = {
        path_arg: "/workspace/myspace/user-1/report.docx",
        "content": "x",
        "dst_path": "/myspace/b.docx",
    }

    domains = [intent.domain for intent in spec.resolver(args, _runtime())]

    assert DOMAIN_MYSPACE in domains


def test_logical_myspace_path_is_still_governed():
    spec = builtin_tool_permission("Write")
    intents = spec.resolver({"file_path": "/myspace/a.txt", "content": "x"}, _runtime())

    assert DOMAIN_MYSPACE in [intent.domain for intent in intents]


def test_ordinary_workspace_path_stays_outside_the_myspace_domain():
    spec = builtin_tool_permission("Write")
    intents = spec.resolver({"file_path": "/workspace/scratch/tmp.txt", "content": "x"}, _runtime())

    assert DOMAIN_MYSPACE not in [intent.domain for intent in intents]


def test_another_users_myspace_area_is_not_treated_as_mine():
    spec = builtin_tool_permission("Write")
    intents = spec.resolver(
        {"file_path": "/workspace/myspace/user-2/x.txt", "content": "x"}, _runtime()
    )

    assert DOMAIN_MYSPACE not in [intent.domain for intent in intents]


# ── Optional arguments still produce intents ─────────────────────────────────


@pytest.mark.parametrize("tool_name", ["Glob", "Grep"])
def test_optional_path_argument_falls_back_to_the_tool_default(tool_name):
    """An omitted optional ``path`` must not resolve to zero intents."""
    spec = builtin_tool_permission(tool_name)

    intents = spec.resolver({"pattern": "*.py"}, _runtime())

    assert [intent.domain for intent in intents] == [DOMAIN_LOCAL_PATH]


# ── MCP tools are temporarily all whitelisted ────────────────────────────────


@pytest.mark.parametrize(
    "server,tool,config",
    [
        ("automation_task", "create_scheduled_task", {}),
        ("plugin_manager", "install_plugin", {}),
        ("automation-automation_task", "create_scheduled_task", {"source_plugin": "automation"}),
        (
            "skill-manager-skill_manager",
            "delete_skill",
            {"source_plugin": "skill-manager"},
        ),
        ("private_server", "destroy", {"permission_default": "deny"}),
    ],
)
def test_mcp_tools_never_receive_a_permission_spec(server, tool, config):
    assert mcp_tool_permission(server, tool, config) is None


# ── MCP whitelist scans do not touch the remote surface ─────────────────────


class _BrokenClient:
    name = "plugin_manager"

    def __init__(self):
        self.calls = 0

    async def list_tools(self):
        self.calls += 1
        raise RuntimeError("connection reset")


@pytest.mark.asyncio
async def test_mcp_whitelist_scan_does_not_enumerate_or_register_tools():
    registry = ToolPermissionRegistry()
    broken = _BrokenClient()

    scan = await register_mcp_client_permissions(registry, [broken], {})

    assert broken.calls == 0
    assert scan.unresolved == ()
    assert scan.names == frozenset()
    assert registry.names == frozenset()


@pytest.mark.asyncio
async def test_trusted_unattended_run_bypasses_builtin_myspace_confirmation():
    registry = ToolPermissionRegistry()
    registry.register("Write", builtin_tool_permission("Write"), source="native")
    service = ToolPermissionService(
        registry,
        _runtime(
            interactive=False,
            approval_available=False,
            default_allow=True,
        ),
    )

    outcome = await service.authorize(
        _tool_call("Write", {"file_path": "/myspace/a.txt", "content": "x"})
    )

    assert outcome.proceed is True
    assert outcome.ticket is not None
    assert outcome.ticket.authorizes_path("/workspace/myspace/user-1/a.txt", "write")
    assert outcome.audit["decision"] == "allow_trusted_unattended"


@pytest.mark.asyncio
async def test_trusted_unattended_run_issues_an_unconfined_builtin_bash_ticket():
    registry = ToolPermissionRegistry()
    registry.register("bash", builtin_tool_permission("bash"), source="native")
    service = ToolPermissionService(
        registry,
        _runtime(
            interactive=False,
            approval_available=False,
            default_allow=True,
        ),
    )

    outcome = await service.authorize(_tool_call("bash", {"command": "touch /tmp/x"}))

    assert outcome.proceed is True
    assert outcome.ticket is not None
    assert [intent.domain for intent in outcome.ticket.intents] == [DOMAIN_LOCAL_COMMAND]
    assert outcome.ticket.local_command is not None
    assert outcome.ticket.local_command.command == "touch /tmp/x"
    assert outcome.ticket.local_command.approval_mode == "full"
    assert outcome.ticket.local_command.read_only is False
    assert outcome.audit["decision"] == "allow_trusted_unattended"


@pytest.mark.parametrize(
    "channel_origin,automation_run,expected",
    [
        (None, False, False),
        ({}, False, False),
        ({"channel_id": "feishu-bot"}, False, True),
        (None, True, True),
    ],
)
def test_only_external_channels_and_automation_default_allow_builtins(
    channel_origin, automation_run, expected
):
    assert (
        _default_allow_builtin_tools(
            channel_origin=channel_origin,
            automation_run=automation_run,
        )
        is expected
    )


# ── Denial must not masquerade as a disk error ───────────────────────────────


def test_permission_enforcement_error_is_not_an_oserror():
    """The file tools wrap real I/O in ``except OSError``; a denial must not hide there."""
    assert not issubclass(PermissionEnforcementError, OSError)
