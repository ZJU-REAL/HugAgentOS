"""Local bash permission integration.

Covers the seam between the permission layer and the execution boundary: what
scope a command is granted, that an unconfinable command is refused rather than
run, and that the sandbox the boundary applies is the one the permission layer
decided on.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from agentscope.message import ToolCallBlock
from core.llm.tool_permissions import (
    APPROVAL_ASK,
    CURRENT_PERMISSION_TICKET,
    FAIL_CLOSED_MODE,
    PermissionRuntime,
    ToolPermissionRegistry,
    ToolPermissionService,
    builtin_tool_permission,
)
from core.llm.tools.sandbox_tool import register_bash
from core.sandbox.local_policy import Grant, Policy
from core.sandbox.oslayer import AccessMode, NetworkPolicy, SandboxUnavailableError

_CONFINE = "core.sandbox.os_sandbox.confine"


def _host_provider(execute=None):
    """A provider that runs commands on the host, so the OS sandbox applies."""

    async def _unreachable(_req):
        raise AssertionError("execution should not have been reached")

    return SimpleNamespace(runs_on_host=True, execute=execute or _unreachable)


def _patch_host_provider(execute=None):
    return patch("core.sandbox.get_sandbox_provider", return_value=_host_provider(execute))


def _capture_policy(captured: dict):
    """Patch target that records the policy and stops before the real backend."""

    def _capture(policy, context):
        captured["policy"] = policy
        captured["resolved"] = policy.filesystem.resolve(context)
        captured["writable"] = [
            root.root for root in captured["resolved"].roots_for(AccessMode.WRITE)
        ]
        raise SandboxUnavailableError("stop after capture")

    return _capture


class _Toolkit:
    fn = None

    def register_tool_function(self, fn, **_kwargs):
        self.fn = fn


def _bash(*, interactive: bool):
    toolkit = _Toolkit()
    register_bash(
        toolkit,
        loader=None,
        loaded_skill_ids=set(),
        chat_id="chat-1",
        interactive=interactive,
    )
    assert toolkit.fn is not None
    return toolkit.fn


def _payload(response) -> dict:
    block = response.content[0]
    text = block["text"] if isinstance(block, dict) else block.text
    return json.loads(text)


async def _authorize(command: str, *, interactive: bool, approval_mode: str = APPROVAL_ASK):
    registry = ToolPermissionRegistry()
    spec = builtin_tool_permission("bash")
    assert spec is not None
    registry.register("bash", spec, source="test")
    service = ToolPermissionService(
        registry,
        PermissionRuntime(
            chat_id="chat-1",
            user_id="user-1",
            interactive=interactive,
            approval_available=interactive,
            approval_mode=approval_mode,
        ),
    )
    return await service.authorize(
        ToolCallBlock(
            id="bash-1",
            name="bash",
            input=json.dumps({"command": command}),
        )
    )


async def _run_authorized(command: str, *, interactive: bool, approval_mode: str = APPROVAL_ASK):
    outcome = await _authorize(command, interactive=interactive, approval_mode=approval_mode)
    assert outcome.proceed is True
    assert outcome.ticket is not None
    token = CURRENT_PERMISSION_TICKET.set(outcome.ticket)
    try:
        return await _bash(interactive=interactive)(command)
    finally:
        CURRENT_PERMISSION_TICKET.reset(token)


async def test_noninteractive_confirm_is_rejected_before_execution():
    with (
        patch.dict("os.environ", {"SANDBOX_TOOLS_ENABLED": "true"}),
        patch("core.config.local_mode.local_mode_enabled", return_value=True),
        patch("core.services.local_grant_service.grants_for_gate", return_value=[]),
        patch(
            "core.services.local_grant_service.policy_for_gate",
            return_value=Policy(),
        ),
    ):
        outcome = await _authorize("curl https://example.com", interactive=False)
    assert outcome.proceed is False
    assert outcome.payload["status"] == "blocked_non_interactive"


async def test_local_bash_execution_boundary_rejects_missing_ticket():
    with (
        patch.dict("os.environ", {"SANDBOX_TOOLS_ENABLED": "true"}),
        patch("core.config.local_mode.local_mode_enabled", return_value=True),
    ):
        response = await _bash(interactive=True)("ls")
    payload = _payload(response)
    assert payload["blocked"] is True
    assert "授权票据" in payload["error"]


@pytest.mark.parametrize("approval_mode", [APPROVAL_ASK, FAIL_CLOSED_MODE])
async def test_a_command_that_cannot_be_confined_is_refused(approval_mode):
    """没有可用后端就拒跑，绝不裸跑——这是本模块要守住的核心行为。"""
    with (
        patch.dict("os.environ", {"SANDBOX_TOOLS_ENABLED": "true"}),
        patch("core.config.local_mode.local_mode_enabled", return_value=True),
        patch("core.services.local_grant_service.grants_for_gate", return_value=[]),
        patch(
            "core.services.local_grant_service.policy_for_gate",
            return_value=Policy(),
        ),
        patch(_CONFINE, side_effect=SandboxUnavailableError("sandbox unavailable")),
        _patch_host_provider(),
    ):
        response = await _run_authorized("ls -la", interactive=True, approval_mode=approval_mode)
    payload = _payload(response)
    assert payload["blocked"] is True
    assert payload["sandbox_unavailable"] is True


async def test_the_execution_boundary_forwards_the_launch_and_the_command_verbatim():
    """The command text is untouched; confinement travels beside it, not inside it."""
    executed = {}

    class _Result:
        stdout, stderr, exit_code, execution_time_ms = "ok", "", 0, 1

    async def _execute(req):
        executed["script"] = req.script_content
        executed["launch"] = req.sandbox_launch
        return _Result()

    with (
        patch.dict("os.environ", {"SANDBOX_TOOLS_ENABLED": "true"}),
        patch("core.config.local_mode.local_mode_enabled", return_value=True),
        patch("core.services.local_grant_service.grants_for_gate", return_value=[]),
        patch(
            "core.services.local_grant_service.policy_for_gate",
            return_value=Policy(),
        ),
        _patch_host_provider(_execute),
    ):
        response = await _run_authorized("ls -la", interactive=True)

    payload = _payload(response)
    assert payload.get("blocked") is None
    assert payload["exit_code"] == 0
    assert executed["script"] == "ls -la"
    assert executed["launch"] is not None
    assert executed["launch"].argv_prefix


async def test_blocking_the_network_category_restricts_the_sandbox_network():
    """用户在本地权限里把网络设成 block，沙箱这一维就真的关掉。"""
    captured: dict = {}
    with (
        patch.dict("os.environ", {"SANDBOX_TOOLS_ENABLED": "true"}),
        patch("core.config.local_mode.local_mode_enabled", return_value=True),
        patch("core.services.local_grant_service.grants_for_gate", return_value=[]),
        patch(
            "core.services.local_grant_service.policy_for_gate",
            return_value=Policy(danger={"network": "block"}),
        ),
        patch(_CONFINE, new=_capture_policy(captured)),
        _patch_host_provider(),
    ):
        await _run_authorized("ls -la", interactive=True)

    assert captured["policy"].network is NetworkPolicy.RESTRICTED


async def test_leaving_the_network_category_alone_keeps_the_sandbox_network_open():
    captured: dict = {}
    with (
        patch.dict("os.environ", {"SANDBOX_TOOLS_ENABLED": "true"}),
        patch("core.config.local_mode.local_mode_enabled", return_value=True),
        patch("core.services.local_grant_service.grants_for_gate", return_value=[]),
        patch(
            "core.services.local_grant_service.policy_for_gate",
            return_value=Policy(),
        ),
        patch(_CONFINE, new=_capture_policy(captured)),
        _patch_host_provider(),
    ):
        await _run_authorized("ls -la", interactive=True)

    assert captured["policy"].network is NetworkPolicy.ENABLED


async def test_approved_copy_only_adds_destination_to_one_shot_write_set():
    captured: dict = {}

    with TemporaryDirectory() as tmp:
        source_dir = Path(tmp) / "source"
        destination_dir = Path(tmp) / "destination"
        source_dir.mkdir()
        destination_dir.mkdir()
        source = source_dir / "input.txt"
        source.write_text("x", encoding="utf-8")
        destination = destination_dir / "output.txt"

        with (
            patch.dict("os.environ", {"SANDBOX_TOOLS_ENABLED": "true"}),
            patch("core.config.local_mode.local_mode_enabled", return_value=True),
            patch(
                "core.services.local_grant_service.grants_for_gate",
                return_value=[Grant(str(source_dir), "read")],
            ),
            patch(
                "core.services.local_grant_service.policy_for_gate",
                return_value=Policy(),
            ),
            patch(
                "core.llm.tools._myspace_confirm.gate",
                new=AsyncMock(return_value=None),
            ),
            patch(_CONFINE, new=_capture_policy(captured)),
            _patch_host_provider(),
            _patch_host_provider(),
        ):
            response = await _run_authorized(
                f"cp {source} {destination}", interactive=True, approval_mode=FAIL_CLOSED_MODE
            )

    payload = _payload(response)
    assert payload["sandbox_unavailable"] is True
    assert str(destination_dir) in captured["writable"]
    assert str(source) not in captured["writable"]
    assert str(source_dir) not in captured["writable"]


async def test_system_overlapping_grant_is_not_a_standing_os_write_bind():
    captured: dict = {}

    with (
        patch.dict("os.environ", {"SANDBOX_TOOLS_ENABLED": "true"}),
        patch("core.config.local_mode.local_mode_enabled", return_value=True),
        patch(
            "core.services.local_grant_service.grants_for_gate",
            return_value=[Grant("/", "readwrite")],
        ),
        patch(
            "core.services.local_grant_service.policy_for_gate",
            return_value=Policy(),
        ),
        patch(_CONFINE, new=_capture_policy(captured)),
        _patch_host_provider(),
    ):
        response = await _run_authorized("ls", interactive=True, approval_mode=FAIL_CLOSED_MODE)

    assert _payload(response)["sandbox_unavailable"] is True
    assert "/" not in captured["writable"]


async def test_an_unattended_run_is_confined_like_an_interactive_one():
    """自动化/子智能体只是跳过「问一句」，不跳过沙箱。"""
    captured: dict = {}
    registry = ToolPermissionRegistry()
    spec = builtin_tool_permission("bash")
    assert spec is not None
    registry.register("bash", spec, source="test")
    service = ToolPermissionService(
        registry,
        PermissionRuntime(
            chat_id="chat-1",
            user_id="user-1",
            interactive=False,
            approval_available=False,
            default_allow=True,
            approval_mode=APPROVAL_ASK,
        ),
    )

    with (
        patch.dict("os.environ", {"SANDBOX_TOOLS_ENABLED": "true"}),
        patch("core.config.local_mode.local_mode_enabled", return_value=True),
        patch("core.services.local_grant_service.grants_for_gate", return_value=[]),
        patch(
            "core.services.local_grant_service.policy_for_gate",
            return_value=Policy(),
        ),
        patch(_CONFINE, new=_capture_policy(captured)),
        _patch_host_provider(),
    ):
        outcome = await service.authorize(
            ToolCallBlock(id="bash-1", name="bash", input=json.dumps({"command": "ls -la"}))
        )
        assert outcome.ticket is not None
        assert outcome.ticket.local_command is not None
        assert outcome.ticket.local_command.confined is True
        token = CURRENT_PERMISSION_TICKET.set(outcome.ticket)
        try:
            response = await _bash(interactive=False)("ls -la")
        finally:
            CURRENT_PERMISSION_TICKET.reset(token)

    assert _payload(response)["sandbox_unavailable"] is True
    assert captured["policy"] is not None
