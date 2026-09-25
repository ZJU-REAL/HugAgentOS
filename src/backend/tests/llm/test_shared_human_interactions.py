"""Human interaction contracts across independently imported worker registries."""

import asyncio
import importlib.util
import sys
import uuid
from pathlib import Path

import pytest
from core.llm.tools import user_questions, _myspace_confirm as confirms


@pytest.fixture(autouse=True, params=["local", "redis", "delayed"])
def shared_store(request, monkeypatch):
    from core.infra import ephemeral

    if request.param in {"local", "delayed"}:

        class DelayedState(ephemeral.LocalEphemeralState):
            async def put(self, key, value, *, ttl):
                await asyncio.sleep(0.02)
                await super().put(key, value, ttl=ttl)

        store = DelayedState() if request.param == "delayed" else ephemeral.LocalEphemeralState()
    else:
        import fakeredis.aioredis

        client = fakeredis.aioredis.FakeRedis(decode_responses=True, protocol=2)
        monkeypatch.setattr(ephemeral, "get_redis", lambda: client)
        store = ephemeral.RedisEphemeralState()
    monkeypatch.setattr(ephemeral, "get_ephemeral_state", lambda: store)
    monkeypatch.setattr("core.llm.interaction_store.get_ephemeral_state", lambda: store)
    monkeypatch.setattr("core.services.yida_service.get_ephemeral_state", lambda: store)
    return store


async def pending_on(worker, chat):
    for _ in range(100):
        result = await worker.get_all_pending_shared(chat)
        if result:
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("pending interaction was never published")


def worker_copy(module):
    name = module.__name__ + "_worker_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, module.__file__)
    worker = importlib.util.module_from_spec(spec)
    sys.modules[name] = worker
    spec.loader.exec_module(worker)
    return worker


@pytest.mark.asyncio
async def test_answer_on_other_worker_resumes_original_tool():
    receiver = worker_copy(user_questions)
    chat = "cross-worker-" + uuid.uuid4().hex
    waiting = asyncio.create_task(
        user_questions.ask(
            chat_id=chat,
            questions=[
                {
                    "id": "scope",
                    "question": "Which?",
                    "options": [{"label": "One"}, {"label": "Two"}],
                }
            ],
            timeout=3,
        )
    )
    try:
        for _ in range(100):
            pending = await receiver.get_all_pending_shared(chat)
            if pending:
                break
            await asyncio.sleep(0.01)
        request = pending[0]
        assert await receiver.answer_shared(
            chat, request["request_id"], [{"id": "scope", "selected": ["option_1"]}]
        ) == {"ok": True, "outcome": "answered"}
        result = await asyncio.wait_for(waiting, 1)
        assert result["answers"][0]["selected_labels"] == ["One"]
        assert await receiver.get_all_pending_shared(chat) == []
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["myspace", "automation", "local_cmd", "tool_permission", "tool:demo"]
)
async def test_confirmation_on_other_worker_resumes_once(kind, monkeypatch):
    monkeypatch.setattr(confirms, "_confirm_enabled", lambda _kind: True)
    receiver = worker_copy(confirms)
    chat = "confirm-worker-" + uuid.uuid4().hex
    waiting = asyncio.create_task(
        confirms.gate(
            chat_id=chat,
            op="write",
            logical_path="/myspace/example.txt",
            interactive=True,
            kind=kind,
            timeout=3,
        )
    )
    try:
        for _ in range(100):
            pending = await receiver.get_all_pending_shared(chat)
            if pending:
                break
            await asyncio.sleep(0.01)
        cid = pending[0]["confirm_id"]
        assert (await receiver.set_decision_shared(chat, cid, "allow"))["ok"]
        assert not (await receiver.set_decision_shared(chat, cid, "deny"))["ok"]
        assert await asyncio.wait_for(waiting, 1) is None
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.asyncio
async def test_parallel_duplicate_writes_execute_only_once(monkeypatch):
    monkeypatch.setattr(confirms, "_confirm_enabled", lambda _kind: True)
    receiver = worker_copy(confirms)
    chat = "dedup-" + uuid.uuid4().hex
    tasks = [
        asyncio.create_task(
            confirms.gate(
                chat_id=chat,
                op="write",
                logical_path="/myspace/same.txt",
                interactive=True,
                timeout=2,
            )
        )
        for _ in range(3)
    ]
    try:
        pending = await pending_on(receiver, chat)
        assert len(pending) == 1
        assert (await receiver.set_decision_shared(chat, pending[0]["confirm_id"], "allow"))["ok"]
        results = await asyncio.wait_for(asyncio.gather(*tasks), 1)
        assert results.count(None) == 1
        assert sum(isinstance(r, dict) and r["status"] == "deduplicated" for r in results) == 2
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_design_choice_cancel_timeout_and_isolation():
    receiver = worker_copy(confirms)
    chat = "pick-" + uuid.uuid4().hex
    waiting = asyncio.create_task(
        confirms.pick(
            chat_id=chat,
            question="Style?",
            options=[{"id": "blue", "title": "Blue"}],
            interactive=True,
            timeout=2,
        )
    )
    try:
        cid = (await pending_on(receiver, chat))[0]["confirm_id"]
        assert not (await receiver.set_decision_shared(chat + "other", cid, "choice", "blue"))["ok"]
        assert not (await receiver.set_decision_shared(chat, cid, "allow"))["ok"]
        assert not (await receiver.set_decision_shared(chat, cid, "choice", "unknown"))["ok"]
        assert (await receiver.set_decision_shared(chat, cid, "choice", "blue"))["ok"]
        assert await waiting == {"status": "chosen", "option_id": "blue"}
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
    question_worker = worker_copy(user_questions)
    task = asyncio.create_task(
        user_questions.ask(chat_id=chat, questions=[{"id": "q", "question": "Go?"}], timeout=2)
    )
    request = (await pending_on(question_worker, chat))[0]
    assert (await question_worker.cancel_shared(chat, request["request_id"]))["ok"]
    assert (await task)["status"] == "cancelled"
    assert await question_worker.get_all_pending_shared(chat) == []
    assert (
        await user_questions.ask(
            chat_id=chat, questions=[{"id": "q", "question": "Go?"}], timeout=0.03
        )
    )["status"] == "timeout"


@pytest.mark.asyncio
async def test_session_allow_does_not_override_deny_or_cross_permission_scope(monkeypatch):
    monkeypatch.setattr(confirms, "_confirm_enabled", lambda _kind: True)
    receiver = worker_copy(confirms)
    chat = "grant-" + uuid.uuid4().hex
    paths = ["/project/a.txt", "/project/b.txt", "/elsewhere/c.txt"]
    tasks = [
        asyncio.create_task(
            confirms.gate(
                chat_id=chat,
                op="write",
                logical_path=path,
                interactive=True,
                kind="local_path:write",
                timeout=2,
            )
        )
        for path in paths
    ]
    try:
        for _ in range(100):
            pending = await receiver.get_all_pending_shared(chat)
            if len(pending) == 3:
                break
            await asyncio.sleep(0.01)
        ids = {p["logical_path"]: p["confirm_id"] for p in pending}
        assert (await receiver.set_decision_shared(chat, ids[paths[1]], "deny"))["ok"]
        granted = await receiver.set_decision_shared(chat, ids[paths[0]], "allow_session")
        assert granted["ok"]
        assert ids[paths[1]] not in granted["cascaded"]
        assert await tasks[0] is None
        assert (await tasks[1])["status"] == "denied_by_user"
        assert not tasks[2].done()
        assert (
            await receiver.gate(
                chat_id=chat,
                op="write",
                logical_path="/project/new.txt",
                interactive=True,
                kind="local_path:write",
                timeout=0.03,
            )
            is None
        )
        assert (
            await receiver.gate(
                chat_id=chat,
                op="write",
                logical_path="/project/new.txt",
                interactive=True,
                kind="local_path:read",
                timeout=0.03,
            )
        )["status"] == "confirm_timeout"
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_same_path_different_permission_domains_are_not_deduplicated(monkeypatch):
    monkeypatch.setattr(confirms, "_confirm_enabled", lambda _kind: True)
    receiver = worker_copy(confirms)
    chat = "domains-" + uuid.uuid4().hex
    tasks = [
        asyncio.create_task(
            confirms.gate(
                chat_id=chat,
                op="write",
                logical_path="/same",
                kind=kind,
                interactive=True,
                timeout=2,
            )
        )
        for kind in ["myspace", "tool_permission"]
    ]
    try:
        for _ in range(30):
            pending = await receiver.get_all_pending_shared(chat)
            if len(pending) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(pending) == 2
        by_kind = {p["kind"]: p["confirm_id"] for p in pending}
        await receiver.set_decision_shared(chat, by_kind["myspace"], "allow")
        await receiver.set_decision_shared(chat, by_kind["tool_permission"], "deny")
        assert await tasks[0] is None
        assert (await tasks[1])["status"] == "denied_by_user"
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_yida_qr_and_organization_selection_cross_workers(tmp_path, monkeypatch):
    import json
    from core.services import yida_service as original

    second = worker_copy(original)
    for module in [original, second]:
        monkeypatch.setattr(module, "_host_workspace_dir", lambda uid: tmp_path / uid / "workspace")
    owner, receiver = original.YidaService(), second.YidaService()

    async def start(*args, **kwargs):
        return (
            json.dumps(
                {
                    "status": "need_qr_scan",
                    "session_file": ".cache/qr.json",
                    "qr_url": "https://example.com/login",
                }
            ),
            0,
        )

    commands = []

    async def poll(uid, command, **kwargs):
        commands.append(command)
        if "--agent-select" in command:
            return json.dumps({"status": "ok", "corp_id": "corp-1"}), 0
        return (
            json.dumps(
                {
                    "status": "need_corp_selection",
                    "organizations": [{"corp_id": "corp-1", "corp_name": "One"}],
                }
            ),
            0,
        )

    monkeypatch.setattr(owner, "_run_in_sandbox", start)
    monkeypatch.setattr(receiver, "_run_in_sandbox", poll)
    assert (await owner.start_login("cross-worker-user"))["status"] == "pending"
    assert (await receiver.poll_login("cross-worker-user"))["status"] == "corp_selection"
    # The selection state must survive switching worker again.
    monkeypatch.setattr(owner, "_run_in_sandbox", poll)
    assert (await owner.poll_login("cross-worker-user", corp_id="corp-1"))["status"] == "connected"
    assert "--agent-select" in commands[-1]


@pytest.mark.asyncio
async def test_answer_and_cancel_race_has_one_winner():
    receiver = worker_copy(user_questions)
    chat = "race-" + uuid.uuid4().hex
    task = asyncio.create_task(
        user_questions.ask(
            chat_id=chat, questions=[{"id": "q", "question": "Continue?"}], timeout=2
        )
    )
    try:
        req = (await pending_on(receiver, chat))[0]["request_id"]
        results = await asyncio.gather(
            receiver.answer_shared(chat, req, [{"id": "q", "custom": "Yes"}]),
            receiver.cancel_shared(chat, req),
        )
        assert sum(result["ok"] for result in results) == 1
        winner = next(result["outcome"] for result in results if result["ok"])
        assert (await task)["status"] == winner
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_session_grant_cascades_and_resolves_concurrent_registration(monkeypatch):
    monkeypatch.setattr(confirms, "_confirm_enabled", lambda _kind: True)
    receiver = worker_copy(confirms)
    chat = "cascade-" + uuid.uuid4().hex
    first = asyncio.create_task(
        confirms.gate(
            chat_id=chat,
            op="write",
            logical_path="/first",
            interactive=True,
            kind="tool:scoped",
            timeout=2,
        )
    )
    tasks = [first]
    try:
        cid = (await pending_on(receiver, chat))[0]["confirm_id"]
        tasks.append(
            asyncio.create_task(
                confirms.gate(
                    chat_id=chat,
                    op="write",
                    logical_path="/second",
                    interactive=True,
                    kind="tool:scoped",
                    timeout=2,
                )
            )
        )
        await asyncio.sleep(0)
        assert (await receiver.set_decision_shared(chat, cid, "allow_session"))["ok"]
        assert await asyncio.wait_for(asyncio.gather(*tasks), 1) == [None, None]
        assert await receiver.get_all_pending_shared(chat) == []
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_thread_pool_answer_wakes_owner_without_cross_thread_event_access():
    loop = asyncio.get_running_loop()
    previous = loop.get_debug()
    loop.set_debug(True)
    chat = "thread-answer-" + uuid.uuid4().hex
    task = asyncio.create_task(
        user_questions.ask(
            chat_id=chat, questions=[{"id": "q", "question": "Continue?"}], timeout=2
        )
    )
    try:
        req = (await pending_on(user_questions, chat))[0]["request_id"]
        result = await asyncio.to_thread(
            user_questions.answer, chat, req, [{"id": "q", "custom": "Yes"}]
        )
        assert result["ok"]
        assert (await asyncio.wait_for(task, 1))["status"] == "answered"
    finally:
        loop.set_debug(previous)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
