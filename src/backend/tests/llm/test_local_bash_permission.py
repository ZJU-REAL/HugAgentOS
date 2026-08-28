"""Local bash permission integration: unattended asks and missing sandboxes fail closed."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agentscope.message import ToolCallBlock
from core.llm.tool_permissions import (
    CURRENT_PERMISSION_TICKET,
    PermissionRuntime,
    ToolPermissionRegistry,
    ToolPermissionService,
    builtin_tool_permission,
)
from core.llm.tools.sandbox_tool import register_bash
from core.sandbox.local_policy import Grant, Policy
from core.sandbox.os_sandbox import OsSandboxUnavailableError


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


async def _authorize(command: str, *, interactive: bool):
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
        ),
    )
    return await service.authorize(
        ToolCallBlock(
            id="bash-1",
            name="bash",
            input=json.dumps({"command": command}),
        )
    )


async def _run_authorized(command: str, *, interactive: bool):
    outcome = await _authorize(command, interactive=interactive)
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


async def test_strict_mode_rejects_missing_os_sandbox():
    """``strict`` promises nothing on the host changes, so it refuses unconfined."""
    unavailable = OsSandboxUnavailableError("sandbox unavailable")
    with (
        patch.dict("os.environ", {"SANDBOX_TOOLS_ENABLED": "true"}),
        patch("core.config.local_mode.local_mode_enabled", return_value=True),
        patch("core.services.local_grant_service.grants_for_gate", return_value=[]),
        patch(
            "core.services.local_grant_service.policy_for_gate",
            return_value=Policy(),
        ),
        patch("core.services.local_grant_service.get_approval_mode", return_value="strict"),
        patch("core.sandbox.os_sandbox.wrap_command", side_effect=unavailable),
    ):
        response = await _run_authorized("ls -la", interactive=True)
    payload = _payload(response)
    assert payload["blocked"] is True
    assert payload["sandbox_unavailable"] is True


async def test_standard_mode_degrades_instead_of_losing_the_shell():
    """No bundled backend (Windows, bwrap-less Linux) must not mean "no bash"."""
    unavailable = OsSandboxUnavailableError("sandbox unavailable")
    executed = {}

    class _Result:
        stdout, stderr, exit_code, execution_time_ms = "ok", "", 0, 1

    async def _execute(req):
        executed["script"] = req.script_content
        return _Result()

    with (
        patch.dict("os.environ", {"SANDBOX_TOOLS_ENABLED": "true"}),
        patch("core.config.local_mode.local_mode_enabled", return_value=True),
        patch("core.services.local_grant_service.grants_for_gate", return_value=[]),
        patch(
            "core.services.local_grant_service.policy_for_gate",
            return_value=Policy(),
        ),
        patch("core.services.local_grant_service.get_approval_mode", return_value="standard"),
        patch("core.sandbox.os_sandbox.wrap_command", side_effect=unavailable),
        patch(
            "core.sandbox.get_sandbox_provider",
            return_value=SimpleNamespace(execute=_execute),
        ),
    ):
        response = await _run_authorized("ls -la", interactive=True)

    payload = _payload(response)
    assert payload.get("blocked") is None
    assert payload["exit_code"] == 0
    # The command ran verbatim, and the degraded isolation is reported, not hidden.
    assert executed["script"] == "ls -la"
    assert "未受 OS 沙箱约束" in payload["confinement_warning"]


async def test_approved_copy_only_adds_destination_to_one_shot_write_set():
    captured = {}

    def capture_and_stop(_command, write_paths, **_kwargs):
        captured["write_paths"] = write_paths
        raise OsSandboxUnavailableError("stop after capture")

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
                "core.services.local_grant_service.get_approval_mode",
                return_value="strict",
            ),
            patch(
                "core.llm.tools._myspace_confirm.gate",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "core.sandbox.os_sandbox.wrap_command",
                side_effect=capture_and_stop,
            ),
        ):
            response = await _run_authorized(f"cp {source} {destination}", interactive=True)

    payload = _payload(response)
    assert payload["sandbox_unavailable"] is True
    assert str(destination_dir) in captured["write_paths"]
    assert str(source) not in captured["write_paths"]
    assert str(source_dir) not in captured["write_paths"]


async def test_system_overlapping_grant_is_not_a_standing_os_write_bind():
    captured = {}

    def capture_and_stop(_command, write_paths, **_kwargs):
        captured["write_paths"] = write_paths
        raise OsSandboxUnavailableError("stop after capture")

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
        patch(
            "core.services.local_grant_service.get_approval_mode",
            return_value="strict",
        ),
        patch(
            "core.sandbox.os_sandbox.wrap_command",
            side_effect=capture_and_stop,
        ),
    ):
        response = await _run_authorized("ls", interactive=True)

    assert _payload(response)["sandbox_unavailable"] is True
    assert "/" not in captured["write_paths"]
