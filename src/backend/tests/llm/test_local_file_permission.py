"""Local file tools share the same fail-closed host-path permission gate."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

from agentscope.message import ToolCallBlock
from core.llm.tool_permissions import (
    CURRENT_PERMISSION_TICKET,
    PermissionRuntime,
    ToolPermissionRegistry,
    ToolPermissionService,
    builtin_tool_permission,
)
from core.llm.tools import _myspace_confirm as confirm
from core.llm.tools._paths import to_physical_path
from core.llm.tools._state import ReadEntry, ReadStateTracker
from core.llm.tools.edit_tool import register_edit
from core.llm.tools.write_tool import register_write
from core.sandbox.local_policy import Grant, Policy


def _payload(response) -> dict:
    block = response.content[0]
    text = block["text"] if isinstance(block, dict) else block.text
    return json.loads(text)


class _Toolkit:
    fn = None

    def register_tool_function(self, fn, **_kwargs):
        self.fn = fn


def _service(
    *tool_names: str,
    interactive: bool,
    chat_id: str | None = "chat-1",
) -> ToolPermissionService:
    registry = ToolPermissionRegistry()
    for name in tool_names:
        spec = builtin_tool_permission(name)
        assert spec is not None
        registry.register(name, spec, source="test")
    return ToolPermissionService(
        registry,
        PermissionRuntime(
            chat_id=chat_id,
            user_id="user-1",
            interactive=interactive,
            approval_available=interactive,
        ),
    )


def _call(name: str, arguments: dict) -> ToolCallBlock:
    return ToolCallBlock(
        id=f"{name}-1",
        name=name,
        input=json.dumps(arguments, ensure_ascii=False),
    )


async def test_strict_workspace_write_is_blocked_before_file_io():
    with (
        patch("core.config.local_mode.local_mode_enabled", return_value=True),
        patch("core.services.local_grant_service.grants_for_gate", return_value=[]),
        patch(
            "core.services.local_grant_service.policy_for_gate",
            return_value=Policy(workspace_write="block", out_of_scope="block"),
        ),
    ):
        outcome = await _service("Write", interactive=True).authorize(
            _call("Write", {"file_path": "/workspace/a.txt", "content": "x"})
        )
    assert outcome.proceed is False
    assert outcome.payload["blocked"] is True


async def test_read_grant_allows_read_but_not_unattended_write():
    grants = [Grant("/data/project", "read")]
    with (
        patch("core.config.local_mode.local_mode_enabled", return_value=True),
        patch("core.services.local_grant_service.grants_for_gate", return_value=grants),
        patch(
            "core.services.local_grant_service.policy_for_gate",
            return_value=Policy(out_of_scope="confirm"),
        ),
    ):
        service = _service("Read", "Write", interactive=False)
        allowed = await service.authorize(_call("Read", {"file_path": "/data/project/a.txt"}))
        blocked = await service.authorize(
            _call(
                "Write",
                {"file_path": "/data/project/a.txt", "content": "x"},
            )
        )
    assert allowed.proceed is True
    assert allowed.ticket is not None
    assert blocked.proceed is False
    assert blocked.payload["status"] == "blocked_non_interactive"


async def test_missing_chat_cannot_turn_confirm_into_an_implicit_grant():
    with (
        patch("core.config.local_mode.local_mode_enabled", return_value=True),
        patch("core.services.local_grant_service.grants_for_gate", return_value=[]),
        patch(
            "core.services.local_grant_service.policy_for_gate",
            return_value=Policy(out_of_scope="confirm"),
        ),
    ):
        outcome = await _service("Read", interactive=True, chat_id=None).authorize(
            _call("Read", {"file_path": "/outside/a.txt"})
        )
    assert outcome.proceed is False
    assert outcome.payload["status"] == "blocked_non_interactive"


async def test_view_image_file_path_uses_the_same_local_read_gate():
    with (
        patch("core.config.local_mode.local_mode_enabled", return_value=True),
        patch("core.services.local_grant_service.grants_for_gate", return_value=[]),
        patch(
            "core.services.local_grant_service.policy_for_gate",
            return_value=Policy(out_of_scope="confirm"),
        ),
    ):
        outcome = await _service("view_image", interactive=False).authorize(
            _call("view_image", {"file_path": "/outside/secret.png"})
        )
    assert outcome.proceed is False
    assert outcome.payload["status"] == "blocked_non_interactive"


async def test_allow_session_is_isolated_between_myspace_and_local_host():
    chat_id = "permission-domain-isolation"
    myspace_task = asyncio.create_task(
        confirm.gate(
            chat_id=chat_id,
            op=confirm.OP_WRITE,
            logical_path="/myspace/a.txt",
            interactive=True,
            kind=confirm.KIND_MYSPACE,
        )
    )
    local_task = asyncio.create_task(
        confirm.gate(
            chat_id=chat_id,
            op=confirm.OP_LOCAL_READ,
            logical_path="/outside/a.txt",
            interactive=True,
            kind=confirm.KIND_LOCAL_CMD,
        )
    )
    await asyncio.sleep(0.05)
    pending = confirm.get_all_pending(chat_id)
    local_id = next(
        item["confirm_id"] for item in pending if item["kind"] == confirm.KIND_LOCAL_CMD
    )
    result = confirm.set_decision(chat_id, local_id, confirm.DECISION_ALLOW_SESSION)
    assert result["cascaded"] == []
    assert await asyncio.wait_for(local_task, timeout=2) is None
    assert not myspace_task.done()

    myspace_id = next(
        item["confirm_id"]
        for item in confirm.get_all_pending(chat_id)
        if item["kind"] == confirm.KIND_MYSPACE
    )
    confirm.set_decision(chat_id, myspace_id, confirm.DECISION_DENY)
    blocked = await asyncio.wait_for(myspace_task, timeout=2)
    assert blocked is not None


async def test_local_read_session_grant_does_not_authorize_local_write():
    chat_id = "permission-local-action-isolation"
    read_task = asyncio.create_task(
        confirm.gate(
            chat_id=chat_id,
            op=confirm.OP_LOCAL_READ,
            logical_path="/outside/a.txt",
            interactive=True,
            kind=f"{confirm.KIND_LOCAL_PATH_PREFIX}read",
        )
    )
    write_task = asyncio.create_task(
        confirm.gate(
            chat_id=chat_id,
            op=confirm.OP_LOCAL_WRITE,
            logical_path="/outside/b.txt",
            interactive=True,
            kind=f"{confirm.KIND_LOCAL_PATH_PREFIX}write",
        )
    )
    await asyncio.sleep(0.05)
    pending = confirm.get_all_pending(chat_id)
    read_id = next(
        item["confirm_id"]
        for item in pending
        if item["kind"] == f"{confirm.KIND_LOCAL_PATH_PREFIX}read"
    )
    result = confirm.set_decision(chat_id, read_id, confirm.DECISION_ALLOW_SESSION)
    assert result["cascaded"] == []
    assert await asyncio.wait_for(read_task, timeout=2) is None
    assert not write_task.done()

    write_id = next(
        item["confirm_id"]
        for item in confirm.get_all_pending(chat_id)
        if item["kind"] == f"{confirm.KIND_LOCAL_PATH_PREFIX}write"
    )
    confirm.set_decision(chat_id, write_id, confirm.DECISION_DENY)
    blocked = await asyncio.wait_for(write_task, timeout=2)
    assert blocked is not None


def test_local_physical_path_is_the_canonical_identity_later_opened():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        outside = root / "outside"
        outside.mkdir()
        link = root / "link"
        link.symlink_to(outside, target_is_directory=True)
        with patch("core.config.local_mode.local_mode_enabled", return_value=True):
            physical = to_physical_path(str(link / "secret.txt"), user_id=None)
    assert physical == str(outside / "secret.txt")


async def test_local_write_detects_existing_host_file_before_overwrite():
    with TemporaryDirectory() as tmp:
        target = Path(tmp) / "existing.txt"
        target.write_text("original", encoding="utf-8")
        toolkit = _Toolkit()
        register_write(
            toolkit,
            chat_id="chat-1",
            user_id="user-1",
            state=ReadStateTracker(),
            interactive=False,
        )
        assert toolkit.fn is not None
        provider = MagicMock(name="unused-provider")
        with (
            patch("core.config.local_mode.local_mode_enabled", return_value=True),
            patch(
                "core.services.local_grant_service.grants_for_gate",
                return_value=[Grant(tmp, "readwrite")],
            ),
            patch(
                "core.services.local_grant_service.policy_for_gate",
                return_value=Policy(),
            ),
            patch("core.sandbox.get_sandbox_provider", return_value=provider),
        ):
            call = _call(
                "Write",
                {"file_path": str(target), "content": "replacement"},
            )
            outcome = await _service("Write", interactive=False).authorize(call)
            assert outcome.proceed is True
            assert outcome.ticket is not None
            token = CURRENT_PERMISSION_TICKET.set(outcome.ticket)
            try:
                response = await toolkit.fn(file_path=str(target), content="replacement")
            finally:
                CURRENT_PERMISSION_TICKET.reset(token)
        payload = _payload(response)
        assert "必须先 Read" in payload["error"]
        assert target.read_text(encoding="utf-8") == "original"
        provider.get_file.assert_not_called()


async def test_remote_write_treats_missing_sandbox_file_as_a_create():
    """A provider-level not-found must not resolve a local-only exception name."""
    from core.sandbox import SandboxError

    toolkit = _Toolkit()
    register_write(
        toolkit,
        chat_id="chat-remote-write",
        user_id="user-1",
        state=ReadStateTracker(),
        interactive=False,
    )
    assert toolkit.fn is not None

    provider = MagicMock(name="remote-provider")
    provider.name = "opensandbox"
    provider.get_file = AsyncMock(side_effect=SandboxError("missing"))
    provider.put_file = AsyncMock()

    with (
        patch("core.config.local_mode.local_mode_enabled", return_value=False),
        patch("core.sandbox.get_sandbox_provider", return_value=provider),
        patch(
            "core.llm.tools.write_tool.sandbox_exec_bash",
            new=AsyncMock(return_value=(0, "", "")),
        ),
    ):
        response = await toolkit.fn(
            file_path="/workspace/scratch/new.txt",
            content="created remotely",
        )

    payload = _payload(response)
    assert payload["ok"] is True
    assert payload["type"] == "create"
    provider.put_file.assert_awaited_once()


async def test_remote_edit_handles_a_sandbox_read_error_without_a_scope_error():
    from core.sandbox import SandboxError

    path = "/workspace/scratch/existing.txt"
    original = b"before"
    state = ReadStateTracker()
    state.record(
        path,
        ReadEntry(
            content=original,
            sha256=hashlib.sha256(original).hexdigest(),
            offset=None,
            limit=None,
        ),
    )
    toolkit = _Toolkit()
    register_edit(
        toolkit,
        chat_id="chat-remote-edit",
        user_id="user-1",
        state=state,
        interactive=False,
    )
    assert toolkit.fn is not None

    provider = MagicMock(name="remote-provider")
    provider.get_file = AsyncMock(side_effect=SandboxError("read failed"))

    with (
        patch("core.config.local_mode.local_mode_enabled", return_value=False),
        patch("core.sandbox.get_sandbox_provider", return_value=provider),
    ):
        response = await toolkit.fn(
            file_path=path,
            old_string="before",
            new_string="after",
        )

    payload = _payload(response)
    assert payload["error"] == "读取文件失败: read failed"
