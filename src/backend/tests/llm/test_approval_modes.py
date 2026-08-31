"""用户自选的权限档（逐项确认 / 替我批准 / 完全放开）如何影响工具确认。"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from agentscope.message import ToolCallBlock
from core.llm.tool_permissions import (
    APPROVAL_ASK,
    APPROVAL_AUTO,
    APPROVAL_FULL,
    READ,
    WRITE,
    PermissionRuntime,
    ToolPermissionRegistry,
    ToolPermissionService,
    local_path_tool,
    normalize_approval_mode,
)
from core.llm.tools._myspace_confirm import OP_DELETE, OP_WRITE
from core.sandbox.local_policy import DANGER_REASON_PREFIX, DELETE, danger_categories


def _tool_call(name: str, arguments: dict) -> ToolCallBlock:
    return ToolCallBlock(id="tc-1", name=name, input=json.dumps(arguments, ensure_ascii=False))


def _service(mode: str) -> ToolPermissionService:
    registry = ToolPermissionRegistry()
    registry.register(
        "Write",
        local_path_tool("file_path", WRITE, tool_name="Write", myspace_op=OP_WRITE),
        source="native",
    )
    registry.register(
        "Delete",
        local_path_tool("path", WRITE, tool_name="Delete", myspace_op=OP_DELETE),
        source="native",
    )
    registry.register(
        "Read",
        local_path_tool("file_path", READ, tool_name="Read"),
        source="native",
    )
    return ToolPermissionService(
        registry,
        PermissionRuntime(
            chat_id="chat-1",
            user_id="user-1",
            interactive=True,
            approval_available=True,
            approval_mode=mode,
        ),
    )


def test_unknown_and_legacy_presets_fall_back_to_asking():
    assert normalize_approval_mode(None) == APPROVAL_ASK
    assert normalize_approval_mode("yolo") == APPROVAL_ASK
    # 旧版本存过的名字不能静默变成放行
    assert normalize_approval_mode("standard") == APPROVAL_ASK
    assert normalize_approval_mode("readonly") == APPROVAL_ASK
    assert normalize_approval_mode(" FULL ") == APPROVAL_FULL


def test_danger_categories_are_recovered_from_verdict_reasons():
    assert danger_categories([f"{DANGER_REASON_PREFIX}{DELETE}", "write intent: /tmp/x"]) == {
        DELETE
    }
    assert danger_categories(["workspace write intent"]) == set()


@pytest.mark.asyncio
async def test_ask_preset_still_confirms_an_ordinary_write():
    gate = AsyncMock(return_value={"status": "blocked", "error": "等确认"})
    with patch("core.llm.tools._myspace_confirm.gate", gate):
        outcome = await _service(APPROVAL_ASK).authorize(
            _tool_call("Write", {"file_path": "/myspace/a.txt", "content": "x"})
        )

    gate.assert_awaited_once()
    assert outcome.proceed is False


@pytest.mark.asyncio
async def test_approve_for_me_passes_writes_but_still_asks_before_deleting():
    gate = AsyncMock(return_value={"status": "blocked", "error": "等确认"})
    with patch("core.llm.tools._myspace_confirm.gate", gate):
        service = _service(APPROVAL_AUTO)
        written = await service.authorize(
            _tool_call("Write", {"file_path": "/myspace/a.txt", "content": "x"})
        )
        deleted = await service.authorize(_tool_call("Delete", {"path": "/myspace/a.txt"}))

    assert written.proceed is True
    assert written.ticket is not None
    # 删除仍然停下来问，且只问了这一次
    gate.assert_awaited_once()
    assert gate.await_args.kwargs["op"] == OP_DELETE
    assert deleted.proceed is False


@pytest.mark.asyncio
async def test_full_access_never_asks_even_to_delete():
    gate = AsyncMock()
    with patch("core.llm.tools._myspace_confirm.gate", gate):
        outcome = await _service(APPROVAL_FULL).authorize(
            _tool_call("Delete", {"path": "/myspace/a.txt"})
        )

    gate.assert_not_awaited()
    assert outcome.proceed is True


@pytest.mark.asyncio
async def test_trusted_unattended_runs_ignore_the_chat_preset():
    """渠道 / 定时这类无人值守入口不受聊天界面档位影响，行为与改造前一致。"""
    registry = ToolPermissionRegistry()
    registry.register(
        "Delete",
        local_path_tool("path", WRITE, tool_name="Delete", myspace_op=OP_DELETE),
        source="native",
    )
    service = ToolPermissionService(
        registry,
        PermissionRuntime(
            chat_id="chat-1",
            user_id="user-1",
            interactive=True,
            approval_available=False,
            default_allow=True,
            approval_mode=APPROVAL_ASK,
        ),
    )
    outcome = await service.authorize(_tool_call("Delete", {"path": "/myspace/a.txt"}))
    assert outcome.proceed is True
