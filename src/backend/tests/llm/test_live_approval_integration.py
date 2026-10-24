"""End-to-end permission-change handling through the shared interaction store."""

import asyncio
import uuid
import pytest
from core.llm.tools import confirmation as confirms
from core.auth.users import UserService
from tests.llm.test_shared_human_interactions import shared_store, pending_on
from tests.storage.project_fixtures import team


@pytest.fixture(autouse=True)
def isolated_local_confirm_state(monkeypatch):
    monkeypatch.setattr(confirms.state.process_state(), "CHATS", {})


@pytest.mark.asyncio
async def test_full_setting_releases_waiting_permissions_but_not_questions(team, monkeypatch):
    db, _ = team
    monkeypatch.setattr(confirms.state, "_confirm_enabled", lambda kind: True)
    permission = asyncio.create_task(
        confirms.gate(
            chat_id="alice",
            op="delete",
            logical_path="/myspace/a.txt",
            interactive=True,
            timeout=5,
            approval_user_id="alice",
        )
    )
    choice = asyncio.create_task(
        confirms.pick(
            chat_id="alice",
            question="Choose a design",
            options=[{"id": "one", "title": "One"}],
            interactive=True,
            timeout=5,
        )
    )
    try:
        for _ in range(100):
            pending = await confirms.get_all_pending_shared("alice")
            if len(pending) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(pending) == 2
        UserService(db).update_user_metadata("alice", {"tool_approval_mode": "full"})
        assert await asyncio.wait_for(permission, 2) is None
        assert not choice.done()
    finally:
        permission.cancel()
        choice.cancel()
        await asyncio.gather(permission, choice, return_exceptions=True)


@pytest.mark.asyncio
async def test_full_never_publishes_new_permission_dialog(team, monkeypatch):
    db, _ = team
    monkeypatch.setattr(confirms.state, "_confirm_enabled", lambda kind: True)
    UserService(db).update_user_metadata("alice", {"tool_approval_mode": "full"})
    queue = confirms.get_ui_queue("alice")
    while queue is not None and not queue.empty():
        queue.get_nowait()
    assert (
        await confirms.gate(
            chat_id="alice",
            op="delete",
            logical_path="/myspace/no-dialog.txt",
            interactive=True,
            timeout=2,
            approval_user_id="alice",
        )
        is None
    )
    queue = confirms.get_ui_queue("alice")
    signals = []
    while queue is not None and not queue.empty():
        signals.append(queue.get_nowait())
    assert all(signal.get("resolved") for signal in signals)


@pytest.mark.asyncio
async def test_account_auto_approval_cannot_deduplicate_module_prompt(team, monkeypatch):
    db, _ = team
    monkeypatch.setattr(confirms.state, "_confirm_enabled", lambda kind: True)
    UserService(db).update_user_metadata("alice", {"tool_approval_mode": "full"})
    scoped = asyncio.create_task(
        confirms.gate(
            chat_id="alice",
            op="delete",
            logical_path="/myspace/same.txt",
            interactive=True,
            timeout=3,
        )
    )
    try:
        pending = await pending_on(confirms, "alice")
        assert all("_approval_user_id" not in item for item in pending)
        assert (
            await confirms.gate(
                chat_id="alice",
                op="delete",
                logical_path="/myspace/same.txt",
                interactive=True,
                timeout=2,
                approval_user_id="alice",
            )
            is None
        )
        assert not scoped.done()
    finally:
        scoped.cancel()
        await asyncio.gather(scoped, return_exceptions=True)


@pytest.mark.asyncio
async def test_account_session_grant_cannot_release_module_prompt(team, monkeypatch):
    monkeypatch.setattr(confirms.state, "_confirm_enabled", lambda kind: True)
    first = asyncio.create_task(
        confirms.gate(
            chat_id="alice",
            op="delete",
            logical_path="/myspace/grant.txt",
            interactive=True,
            timeout=3,
            approval_user_id="alice",
        )
    )
    module = None
    try:
        pending = await pending_on(confirms, "alice")
        assert (
            await confirms.set_decision_shared("alice", pending[0]["confirm_id"], "allow_session")
        )["ok"]
        assert await first is None
        assert (
            await confirms.gate(
                chat_id="alice",
                op="delete",
                logical_path="/myspace/grant-again.txt",
                interactive=True,
                timeout=3,
                approval_user_id="alice",
            )
            is None
        )
        module = asyncio.create_task(
            confirms.gate(
                chat_id="alice",
                op="delete",
                logical_path="/myspace/module.txt",
                interactive=True,
                timeout=3,
            )
        )
        await pending_on(confirms, "alice")
        assert not module.done()
    finally:
        first.cancel()
        if module is not None:
            module.cancel()
        await asyncio.gather(*[task for task in (first, module) if task], return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_claim_execution(monkeypatch):
    monkeypatch.setattr(confirms.state, "_confirm_enabled", lambda kind: True)
    chat = "cancel-execution-" + uuid.uuid4().hex

    async def ask():
        return await confirms.gate(
            chat_id=chat, op="edit", logical_path="/myspace/same.txt", interactive=True, timeout=3
        )

    first = asyncio.create_task(ask())
    second = asyncio.create_task(ask())
    try:
        pending = await pending_on(confirms, chat)
        cid = pending[0]["confirm_id"]
        for _ in range(100):
            if confirms.state.process_state().CHATS[chat].pending[cid].waiters == 2:
                break
            await asyncio.sleep(0.01)
        assert (await confirms.set_decision_shared(chat, cid, "allow"))["ok"]
        first.cancel()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert results[1] is None
    finally:
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
