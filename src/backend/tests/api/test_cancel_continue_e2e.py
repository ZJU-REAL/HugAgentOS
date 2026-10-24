"""HTTP cancel → reload → continue, with isolated DB/Redis and controlled model I/O.

The production HTTP admission, worker, cancellation, persistence, history loader,
formatter and response transports run unchanged. The model/tool workflow is a
fixture so cancellation timing and accidental private-text echo are deterministic.
"""

from __future__ import annotations

import asyncio
import contextlib

import core.agent_runtime.runner as runner
import core.db.engine as core_db_engine
import core.runs.tasks as tasks
import fakeredis.aioredis
import httpx
import pytest
from api.routes.v1 import agent_responses, chat_runs, chats
from core.auth.backend import UserContext, get_current_user
from core.db import engine as db_engine
from core.db.engine import Base, get_db
from core.db.models import ChatRun, ChatSession
from core.models.chat_models import ReasoningEchoChatFormatter
from core.llm.runtime.message_compat import session_to_msgs
from core.llm.runtime.model_steps import record_assistant_step
from core.chat.operations import invocation
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from tests.api.test_chat_stream_sequencer import _patch_common_chat_route

PRIVATE = "PRIVATE_REASONING_MUST_NOT_BECOME_AN_ANSWER"
PARTIAL = "已检查服务器，正在整理结果。"
RESULT = "22/tcp open"


@pytest.fixture
def environment(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'cancel-continue.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        db.add(ChatSession(chat_id="chat-1", user_id="user-1", title="test"))
        db.commit()
    monkeypatch.setattr(db_engine, "SessionLocal", sessions)
    monkeypatch.setattr(core_db_engine, "SessionLocal", sessions)
    monkeypatch.setattr("core.chat.plan_progress.SessionLocal", sessions)
    monkeypatch.setattr('core.chat.steer.SessionLocal', sessions)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr('core.runs.events.store.get_redis', lambda **_: redis)
    _patch_common_chat_route(monkeypatch)
    monkeypatch.setattr(invocation, 'resolve_explicit_capability_invocation', lambda db, r, u: r)

    async def preferences(*_):
        return (None, None, None), {}

    monkeypatch.setattr('core.chat.operations.admission._read_preferences', preferences)
    app = FastAPI()
    for router in (agent_responses.router, chats.router, chat_runs.router):
        app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="user-1", user_center_id="center-1", username="test"
    )

    def database():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = database
    tasks.process_state().active_runs.clear()
    yield app, sessions
    tasks.process_state().active_runs.clear()
    engine.dispose()


def step(*blocks):
    return {
        "type": "model_step",
        "step": record_assistant_step(
            blocks, provider="deepseek", model="deepseek", protocol="openai_chat"
        ),
    }


def make_workflow(scenario):
    ready = asyncio.Event()
    calls = []
    observed_wire = []
    formatter = ReasoningEchoChatFormatter(
        replay_provider="deepseek", replay_model="deepseek", replay_protocol="openai_chat"
    )

    async def workflow(**kwargs):
        if kwargs.get("user_message") == "继续":
            wire = await formatter.format(session_to_msgs(kwargs["session_messages"]))
            observed_wire.extend(wire)
            # An adversarial echo endpoint makes text promotion observable in
            # the final HTTP answer, rather than only in an internal snapshot.
            texts = str([row.get("content") for row in wire])
            answer = PRIVATE if PRIVATE in texts else "已继续：端口检查结果已整理。"
            yield {"type": "ai_message", "delta": answer}
            yield step({"type": "text", "text": answer})
            yield {"type": "meta"}
            return
        if scenario == "completed_tool_then_partial":
            calls.append("scan")
            yield step(
                {"type": "thinking", "thinking": "COMPLETE_NATIVE_REASONING"},
                {"type": "tool_call", "id": "call-1", "name": "scan", "input": "{}"},
            )
            yield {"type": "tool_call", "tool_id": "call-1", "tool_name": "scan", "tool_args": {}}
            yield {
                "type": "model_step",
                "step": {
                    "schema": "harness.steps.v1",
                    "kind": "tool_result",
                    "blocks": [
                        {
                            "type": "tool_result",
                            "id": "call-1",
                            "name": "scan",
                            "output": RESULT,
                            "state": "success",
                        }
                    ],
                },
            }
            yield {"type": "tool_result", "tool_id": "call-1", "content": RESULT}
        yield {"type": "thinking", "delta": PRIVATE, "structured_reasoning": True}
        if scenario != "thinking_only":
            yield {"type": "ai_message", "delta": PARTIAL}
        ready.set()
        await asyncio.Event().wait()

    return workflow, ready, calls, observed_wire


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["legacy", "completed_tool_then_partial", "thinking_only"])
async def test_cancel_reload_continue_never_echoes_private_reasoning(
    environment, monkeypatch, scenario
):
    app, sessions = environment
    workflow, ready, calls, observed_wire = make_workflow(scenario)
    monkeypatch.setattr(runner, "astream_chat_workflow", workflow)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = asyncio.create_task(
            client.post(
                "/v1/agents/responses",
                json={
                    "chat_id": "chat-1",
                    "message": "检查服务器",
                    "stream": True,
                    "model_name": "deepseek",
                },
            )
        )
        try:
            waiting = asyncio.create_task(ready.wait())
            done, _ = await asyncio.wait(
                [first, waiting], timeout=5, return_when=asyncio.FIRST_COMPLETED
            )
            if first in done:
                assert first.result().status_code == 200, first.result().text
            if not waiting.done():
                waiting.cancel()
            assert ready.is_set(), "worker never reached model fixture"
            with sessions() as db:
                run_id = db.query(ChatRun).one().run_id
            cancelled = await client.post(f"/v1/chat-runs/{run_id}/cancel")
            assert cancelled.status_code == 200
            assert cancelled.json()["data"]["cancelled"] is True
            first_response = await asyncio.wait_for(first, timeout=5)
            assert first_response.status_code == 200
            assert PRIVATE in first_response.text
            assert "[DONE]" in first_response.text
            history = await client.get("/v1/chats/chat-1/messages")
            assert history.status_code == 200
            assistants = [m for m in history.json()["data"]["items"] if m["role"] == "assistant"]
            assert len(assistants) == 1
            assert assistants[0]["thinking"] == [{"content": PRIVATE}]
            assert PRIVATE not in assistants[0]["content"]
            assert assistants[0]["metadata"]["cancelled"] is True
            if scenario != "thinking_only":
                assert assistants[0]["content"] == PARTIAL
            continued = await asyncio.wait_for(
                client.post(
                    "/v1/agents/responses",
                    json={
                        "chat_id": "chat-1",
                        "message": "继续",
                        "model_name": "deepseek",
                    },
                ),
                timeout=5,
            )
            assert continued.status_code == 200, continued.text
            assert continued.json()["response"] == "已继续：端口检查结果已整理。"
            assert "Historical reasoning" not in str(observed_wire)
            assert PRIVATE not in str(observed_wire)
            assert "[本轮回答被用户中断]" in str(observed_wire)
            if scenario != "thinking_only":
                assert str(observed_wire).count(PARTIAL) == 1
            if scenario == "completed_tool_then_partial":
                assert RESULT in str(observed_wire)
                assert calls == ["scan"]
                assert any(
                    row.get("reasoning_content") == "COMPLETE_NATIVE_REASONING"
                    for row in observed_wire
                )
            else:
                assert calls == []
        finally:
            if not first.done():
                first.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await first
            for worker in list(tasks.process_state().active_runs.values()):
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["legacy", "completed_tool_then_partial", "thinking_only"])
async def test_browser_cancel_reload_continue(environment, monkeypatch, scenario):
    """A real browser talks to the isolated production HTTP routes over TCP."""
    import os
    import socket
    from pathlib import Path

    import uvicorn

    if os.environ.get("RUN_REASONING_BROWSER_E2E") != "1":
        pytest.skip("Set RUN_REASONING_BROWSER_E2E=1 for Chromium end-to-end checks")
    app, _sessions = environment
    workflow, _ready, calls, observed_wire = make_workflow(scenario)
    monkeypatch.setattr(runner, "astream_chat_workflow", workflow)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    frontend = Path(__file__).resolve().parents[3] / "frontend"
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started
        process = await asyncio.create_subprocess_exec(
            "node",
            "scripts/test-cancel-continue-browser.mjs",
            f"http://127.0.0.1:{port}",
            scenario,
            cwd=frontend,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=60)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        assert process.returncode == 0, stdout.decode()
        print(stdout.decode())
        assert "Historical reasoning" not in str(observed_wire)
        assert PRIVATE not in str(observed_wire)
        assert calls == (["scan"] if scenario == "completed_tool_then_partial" else [])
    finally:
        for worker in list(tasks.process_state().active_runs.values()):
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        server.should_exit = True
        await asyncio.wait_for(serving, timeout=5)
        sock.close()
