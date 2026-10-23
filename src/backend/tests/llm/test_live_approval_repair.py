"""Live dispatch authority and account/module isolation regressions."""

from dataclasses import replace
from unittest.mock import AsyncMock
import pytest
from tests.orchestration.hook_support import run_permission
from core.llm.tool_permissions.pending import approve_current_policy
from tests.llm.test_approval_modes import _service, _tool_call
from tests.storage.project_fixtures import team


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial,current,executes", [("ask", "full", True), ("full", "ask", False)]
)
async def test_running_dispatch_rechecks_account_mode(monkeypatch, initial, current, executes):
    service = _service(initial)
    service.runtime = replace(service.runtime, account_approval=True)
    monkeypatch.setattr("core.sandbox.permissions.account.read_account_mode", lambda *a: current)
    gate = AsyncMock(return_value={"error": "not approved"})
    monkeypatch.setattr("core.llm.tools.confirmation.gate", gate)

    async def execute(**kwargs):
        yield "executed"

    result = [
        x
        async for x in run_permission(service, {"tool_call": _tool_call("space_delete", {"path": "/myspace/a.txt"})}, execute)
    ]
    assert (result == ["executed"]) is executes
    assert gate.await_count == (0 if executes else 1)


@pytest.mark.asyncio
async def test_explicit_module_policy_is_not_replaced(monkeypatch):
    monkeypatch.setattr(
        "core.sandbox.permissions.account.read_account_mode",
        lambda *a: (_ for _ in ()).throw(AssertionError("account cannot replace module")),
    )
    gate = AsyncMock(return_value={"error": "not approved"})
    monkeypatch.setattr("core.llm.tools.confirmation.gate", gate)

    async def execute(**kwargs):
        yield "executed"

    result = [
        x
        async for x in run_permission(_service("ask"), {"tool_call": _tool_call("space_delete", {"path": "/myspace/a.txt"})}, execute)
    ]
    assert result != ["executed"]
    gate.assert_awaited_once()


@pytest.mark.asyncio
async def test_unavailable_policy_blocks_without_prompt(monkeypatch):
    service = _service("full")
    service.runtime = replace(service.runtime, account_approval=True)

    def unavailable(*args):
        raise OSError("database offline")

    monkeypatch.setattr("core.sandbox.permissions.account.read_account_mode", unavailable)
    gate = AsyncMock()
    monkeypatch.setattr("core.llm.tools.confirmation.gate", gate)

    async def execute(**kwargs):
        yield "executed"

    result = [
        x
        async for x in run_permission(service, {"tool_call": _tool_call("space_delete", {"path": "/myspace/a.txt"})}, execute)
    ]
    assert result != ["executed"]
    gate.assert_not_called()


def test_api_visitor_and_other_owner_cannot_use_account_policy(team):
    from core.db.models import ChatSession
    from core.sandbox.permissions.account import read_account_mode

    db, _ = team
    from core.auth.users import UserService
    UserService(db).update_user_metadata("alice", {"tool_approval_mode": "full"})
    db.add_all(
        [
            ChatSession(
                chat_id="visitor",
                user_id="alice",
                extra_data={"agent_api_scope": {"public_chat_visitor": "visitor"}},
            ),
            ChatSession(
                chat_id="api",
                user_id="alice",
                extra_data={"project_api_scope": {"api_key_id": "key"}},
            ),
        ]
    )
    db.commit()
    for chat in ("visitor", "api", "bob", "missing"):
        assert read_account_mode("alice", chat) is None


@pytest.mark.asyncio
async def test_pending_policy_never_answers_untrusted_or_design_prompts(monkeypatch):
    read = lambda *a: (_ for _ in ()).throw(AssertionError("no account authority"))
    monkeypatch.setattr("core.llm.tool_permissions.pending.read_account_mode", read)
    assert not await approve_current_policy("chat", "one", {"kind": "myspace"})
    assert not await approve_current_policy(
        "chat", "two", {"kind": "design_pick", "_approval_user_id": "alice"}
    )
