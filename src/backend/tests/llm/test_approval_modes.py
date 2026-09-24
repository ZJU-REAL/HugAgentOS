"""用户自选的权限档（逐项确认 / 替我批准 / 完全放开）如何影响工具确认。"""

from __future__ import annotations

import asyncio
from pathlib import Path

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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "asks"),
    [(APPROVAL_ASK, True), (APPROVAL_AUTO, False), (APPROVAL_FULL, False)],
)
async def test_myspace_writeback_follows_the_preset(mode, asks, monkeypatch):
    """沙盒改写「我的空间」里已有文件时的确认也要认这一档。

    这条确认不经过 ToolPermissionMiddleware 的判定，历史上一律弹框——用户明明选了
    「完全放开」，跑个 bash 照样被逐个文件拦下来。发起方现在是文件系统登记器，它跑在
    自己的任务里读不到 ContextVar，所以按用户设置里的档位判。
    """
    from core.myspace import mirror, watcher

    monkeypatch.setattr(
        "core.llm.tool_permissions.resolve_approval_mode",
        lambda explicit, *, user_id: mode,
    )
    monkeypatch.setattr(watcher, "_HANDOFF_GRACE_S", 0)

    async def _claim(user_id, rel, stamp):
        return True

    monkeypatch.setattr(watcher, "_claim", _claim)
    monkeypatch.setattr(watcher, "_confirm_chat", lambda uid: "chat-1")
    monkeypatch.setattr(
        watcher,
        "_stat_all",
        lambda uid, rels: {
            rel: mirror.MirrorEntry(rel=rel, path=Path("/tmp") / rel, size=1, mtime=1.0)
            for rel in rels
        },
    )
    monkeypatch.setattr(
        mirror,
        "classify_claimed",
        lambda *, user_id, entries: {rel: mirror.VERDICT_MODIFIED for rel in entries},
    )
    monkeypatch.setattr(
        mirror, "register_entry", lambda *, user_id, entry: {"file_id": "f1"}
    )

    reg = watcher.MySpaceRegistry()
    reg._budget = watcher._Budget(watcher._INFLIGHT_BUDGET_BYTES)
    with patch(
        "core.llm.tools._myspace_confirm.gate", new=AsyncMock(return_value=None)
    ) as gate:
        await reg._process("u1", ["报告.xlsx"])
    assert gate.await_count == (1 if asks else 0)


class _StreamItem:
    """SDK 的 StreamItem：一行输出，且**不带**行尾换行（生产实测形态）。"""

    def __init__(self, text):
        self.text = text


def test_sandbox_stdout_keeps_its_line_breaks():
    """SDK 把 stdout 拆成一行一项且不带换行；粘成一行会让逐行解析全错。"""
    from core.sandbox._opensandbox_internals import _join_logs

    assert _join_logs([_StreamItem("AAA"), _StreamItem("BBB")]) == "AAA\nBBB\n"
    # 已经自带换行的分片不重复加
    assert _join_logs([_StreamItem("AAA\n"), _StreamItem("BBB\n")]) == "AAA\nBBB\n"
    assert _join_logs([]) == ""


def test_join_composes_exactly_with_the_upstream_blank_line_fix():
    """升到 execd v1.1.0 后，空行会作为一条内容为 "\\n" 的事件发过来。

    上游 c1d19b7c 对空行 ``onExecute("\\n")``、对正常行发不带换行的内容。补换行
    时"自带换行的不再补"这一条，正是让两者拼起来逐字节还原的关键——否则空行会
    变成两个换行。
    """
    from core.sandbox._opensandbox_internals import _join_logs

    # printf 'A\n\n\nB\n' 在修复后的 execd 上收到的事件序列
    items = [_StreamItem("A"), _StreamItem("\n"), _StreamItem("\n"), _StreamItem("B")]
    assert _join_logs(items) == "A\n\n\nB\n"
