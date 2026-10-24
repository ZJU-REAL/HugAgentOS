"""Regression coverage for the tool-permission hardening pass.

Each test pins one way the declarative gateway could silently stop governing
something: a second spelling of a governed path, an omitted optional argument,
a trusted unattended run, or an MCP tool accidentally entering the registry.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from agentscope.message import ToolCallBlock
from core.llm.factory.runtime.evidence import _default_allow_builtin_tools
from core.sandbox.permissions.dispatch_models import DOMAIN_LOCAL_COMMAND, DOMAIN_LOCAL_PATH, DOMAIN_MYSPACE, PermissionEnforcementError, PermissionRuntime
from core.sandbox.permissions.tool_specs import ToolPermissionRegistry, builtin_tool_permission
from core.llm.tool_permissions import ToolPermissionService


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
    ],
)
def test_physical_myspace_path_still_requires_the_tool_confirmation(tool_name, path_arg):
    """``/workspace/myspace/<uid>/…`` must not skip the persistence gate.

    Both logical and physical spellings reach the mounted persistent directory
    and require the same approval.
    """
    spec = builtin_tool_permission(tool_name)
    args = {
        path_arg: "/workspace/myspace/user-1/report.docx",
        "content": "x",
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





@pytest.mark.asyncio
async def test_trusted_unattended_run_bypasses_builtin_tool_confirmation():
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
async def test_trusted_unattended_run_issues_a_confined_builtin_bash_ticket():
    """A trusted unattended run skips the prompt, not the sandbox.

    Channel and automation entry points are trusted enough not to stop and ask,
    but they are not a reason to hand a command more of the machine than the
    user's own configuration allows — so the ticket carries that configuration's
    preset and scope, exactly as an interactive run would. The sandbox only
    exists in local mode, which is where these entry points meet host commands.
    """
    registry = ToolPermissionRegistry()
    registry.register("Bash", builtin_tool_permission("Bash"), source="native")
    service = ToolPermissionService(
        registry,
        _runtime(
            interactive=False,
            approval_available=False,
            default_allow=True,
        ),
    )

    with patch("core.config.local_mode.local_mode_enabled", return_value=True):
        outcome = await service.authorize(_tool_call("Bash", {"command": "touch /tmp/x"}))

    assert outcome.proceed is True
    assert outcome.ticket is not None
    assert [intent.domain for intent in outcome.ticket.intents] == [DOMAIN_LOCAL_COMMAND]
    assert outcome.ticket.local_command is not None
    assert outcome.ticket.local_command.command == "touch /tmp/x"
    assert outcome.ticket.local_command.approval_mode == "ask"
    assert outcome.ticket.local_command.confined is True
    assert outcome.audit["decision"] == "allow_trusted_unattended"


@pytest.mark.asyncio
async def test_trusted_unattended_run_still_obeys_a_policy_denial():
    """Being unattended answers the confirmation; it does not waive the policy.

    The earlier implementation returned a ticket before evaluating any intent,
    so a scheduled run could read or write host paths the user's own policy
    blocks outright — the one thing no preset is allowed to skip.
    """
    from core.sandbox.permissions.models import Policy

    registry = ToolPermissionRegistry()
    registry.register("Read", builtin_tool_permission("Read"), source="native")
    service = ToolPermissionService(
        registry,
        _runtime(
            interactive=False,
            approval_available=False,
            default_allow=True,
        ),
    )

    with (
        patch("core.config.local_mode.local_mode_enabled", return_value=True),
        patch('features.desktop.local.grants.grants_for_gate', return_value=[]),
        patch(
            'features.desktop.local.grants.policy_for_gate',
            return_value=Policy(out_of_scope="block"),
        ),
    ):
        outcome = await service.authorize(_tool_call("Read", {"file_path": "/etc/shadow"}))

    assert outcome.proceed is False
    assert outcome.ticket is None
    assert outcome.audit["decision"] == "deny"


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
