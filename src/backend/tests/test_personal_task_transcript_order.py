"""Task history preserves the position of instructions received during execution."""
from tests.orchestration.hook_support import make_adapter
from agentscope.message import ToolCallBlock

import time
from types import SimpleNamespace

import pytest
from core.db.models import ChatRun
from personal_agent.context import AgentContext
from personal_agent.conversation.message_routing import admit
from personal_agent.models import PersonalWorkItem
from personal_agent.tasks.task_threads import Transcript, history
from tests.test_personal_agent_monitor import sessions  # noqa: F401


@pytest.fixture
def prepared(sessions, monkeypatch):
    monkeypatch.setattr("core.db.engine.SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    with sessions() as db:
        svc = AgentContext(db, "owner")
        ident = svc.tasks.submit("抓取热搜", "initial")["id"]
        task = db.get(PersonalWorkItem, ident)
        task.status, task.owner, task.run_id = "running", "worker", "run"
        task.lease_until = time.time() + 100
        db.add(ChatRun(run_id="run", chat_id=task.chat_id, user_id="owner",
                       message_id="assistant-run", status="running"))
        db.commit()
    return ident, SimpleNamespace(run_id="run", owner="journal", chat_id=task.chat_id)


@pytest.fixture
def running(prepared):
    ident, binding = prepared
    return ident, Transcript(ident, "owner", binding)


def update(sessions, ident, text, key):
    with sessions() as db:
        svc = AgentContext(db, "owner")
        return admit(svc, text, key, work_item_ids=[ident], intent="update")


def read(sessions, ident, **options):
    with sessions() as db:
        return history(AgentContext(db, "owner"), ident, **options)


def test_running_task_displays_followup_between_earlier_and_later_output(running, sessions):
    ident, log = running
    log.record("text_delta", "开始抓取")
    log.record("text_delta", "，正在检查网页")
    update(sessions, ident, "改用脚本", "steer")
    log.record("text_delta", "改用脚本抓取")
    log.flush()
    rows = read(sessions, ident)["messages"]
    assert [(m["role"], m["content"]) for m in rows] == [
        ("user", "抓取热搜"), ("assistant", "开始抓取，正在检查网页"),
        ("user", "改用脚本"), ("assistant", "改用脚本抓取"),
    ]
    assert not rows[1]["is_streaming"]
    assert rows[3]["is_streaming"]
    assert read(sessions, ident)["messages"] == rows


def test_repeated_updates_keep_stable_order_ids_and_pagination(running, sessions):
    ident, log = running
    log.record("text_delta", "初始过程")
    update(sessions, ident, "改用脚本", "first")
    log.record("text_delta", "脚本过程")
    log.flush()
    first = read(sessions, ident)["messages"]
    update(sessions, ident, "补充时间", "second")
    update(sessions, ident, "补充时间", "second")  # Admission retry is idempotent.
    log.record("text_delta", "带时间的结果")
    log.flush()
    rows = read(sessions, ident)["messages"]
    assert [m["content"] for m in rows] == [
        "抓取热搜", "初始过程", "改用脚本", "脚本过程", "补充时间", "带时间的结果",
    ]
    assert [m["message_id"] for m in rows[:4]] == [m["message_id"] for m in first]
    latest = read(sessions, ident, limit=2)
    older = read(sessions, ident, before=latest["messages"][0]["chat_seq"], limit=4)
    assert older["messages"] + latest["messages"] == rows
    assert [m["is_streaming"] for m in rows] == [False, False, False, False, False, True]


def test_tool_result_updates_original_call_across_multiple_followups(running, sessions):
    ident, log = running
    log.record("thinking_delta", "先检查网页")
    log.record("tool_call", {"id": "browser", "name": "pa_bash", "args": {"command": "fetch"}})
    update(sessions, ident, "改用脚本", "first")
    log.record("thinking_delta", "检查脚本接口")
    log.record("tool_call", {"id": "script", "name": "pa_bash", "args": {"command": "python"}})
    update(sessions, ident, "补充时间", "second")
    log.record("text_delta", "整理结果")
    log.flush()
    log.record("tool_result", {"id": "browser", "content": "网页结果", "status": "success"})
    log.record("tool_result", {"id": "script", "content": "脚本结果", "status": "success"})
    rows = read(sessions, ident)["messages"]
    assert [m["role"] for m in rows] == ["user", "assistant", "user", "assistant", "user", "assistant"]
    assert rows[1]["thinking"] == [{"content": "先检查网页"}]
    assert rows[3]["thinking"] == [{"content": "检查脚本接口"}]
    assert rows[1]["tool_calls"][0]["result"] == "网页结果"
    assert rows[3]["tool_calls"][0]["result"] == "脚本结果"
    assert rows[1]["tool_calls"][0]["status"] == rows[3]["tool_calls"][0]["status"] == "success"
    assert not rows[1]["is_streaming"] and not rows[3]["is_streaming"]
    assert not rows[5]["tool_calls"]
    assert [seg["type"] for seg in rows[3]["metadata"]["segments"]] == ["thinking", "tool"]


def test_tool_finishing_before_next_text_does_not_create_empty_response(running, sessions):
    ident, log = running
    log.record("tool_call", {"id": "tool", "name": "pa_bash", "args": {}})
    update(sessions, ident, "改用脚本", "first")
    log.record("tool_result", {"id": "tool", "content": "done", "status": "success"})
    log.flush()
    rows = read(sessions, ident)["messages"]
    assert len(rows) == 3 and rows[-1]["content"] == "改用脚本"
    assert rows[1]["tool_calls"][0]["result"] == "done"
    log.record("text_delta", "继续执行")
    log.flush()
    assert read(sessions, ident)["messages"][-1]["content"] == "继续执行"


def test_direct_steer_also_splits_and_completion_identifies_last_response(running, sessions):
    ident, log = running
    log.record("text_delta", "原始过程")
    with sessions() as db:
        AgentContext(db, "owner").tasks.steer(ident, "改用脚本", "direct")
    log.record("text_delta", "最终结果")
    log.flush()
    with sessions() as db:
        svc = AgentContext(db, "owner")
        svc.tasks.complete(ident, "最终结果", {})
    result = read(sessions, ident)
    assert [m["content"] for m in result["messages"]] == ["抓取热搜", "原始过程", "改用脚本", "最终结果"]
    assert result["terminal_message_id"] == result["messages"][-1]["message_id"]
    assert not any(m["is_streaming"] for m in result["messages"])


@pytest.mark.parametrize("change", ["lease", "owner", "run", "status"])
def test_lost_execution_cannot_append_or_change_history(running, sessions, change):
    ident, log = running
    log.record("text_delta", "原始过程")
    log.flush()
    update(sessions, ident, "改用脚本", "first")
    with sessions() as db:
        task = db.get(PersonalWorkItem, ident)
        if change == "lease":
            task.lease_until = time.time() - 1
        elif change == "owner":
            task.owner = "replacement"
        elif change == "run":
            task.run_id = "replacement"
        else:
            task.status = "waiting"
        db.commit()
    before = read(sessions, ident)["messages"]
    log.record("text_delta", "不应写入")
    log.flush()
    assert read(sessions, ident)["messages"] == before


@pytest.mark.asyncio
async def test_execute_task_consumes_midrun_instruction_and_finishes_in_order(prepared, sessions, monkeypatch):
    import asyncio
    import threading

    from personal_agent.tasks import runtime
    from personal_agent.tasks.execution import current_execution
    from personal_agent.tasks.steering import current_steering
    from personal_agent.tools.tools import register_personal_tools
    from sqlalchemy import event

    ident, binding = prepared
    main_thread = threading.get_ident()
    database_started, pulse = threading.Event(), threading.Event()

    def slow_database(conn, cursor, statement, parameters, context, executemany):
        if threading.get_ident() != main_thread and not database_started.is_set():
            database_started.set()
            assert pulse.wait(2), "Database work blocked the event loop"

    async def input_loop_pulse():
        assert await asyncio.to_thread(database_started.wait, 2)
        pulse.set()

    monkeypatch.setattr(runtime, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.steering.SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tools.tools.SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tools.space.SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tools.email.SessionLocal", sessions)
    kit = []
    register_personal_tools(kit, SimpleNamespace(profile_ref=ident, current_user_id="owner"))

    # Substitute only the model/stream boundary; admission, task tools, transcript,
    # revision consumption, completion and the history read are real.
    async def executor(**options):
        return object(), []

    class ModelStream:
        def __init__(self, instance, clients):
            pass

        async def stream(self, **options):
            yield "text_delta", "先检查网页"
            assert database_started.is_set() and pulse.is_set()
            update(sessions, ident, "改用脚本", "runtime-update")
            from personal_agent.tasks.steering import current_steering
            middleware = make_adapter(*current_steering.get().hook_policy().specs())
            agent = SimpleNamespace(state=SimpleNamespace(run_id="run", context=[], steer_delivery=None))

            async def continue_model(**kwargs):
                assert "改用脚本" in agent.state.context[-1].content[0].text
                yield "text_delta", "已按脚本要求完成"
                await {tool.name: tool._func for tool in kit}["pa_finish"]("已按脚本要求完成")

            async for event in middleware.on_reasoning(agent, {}, continue_model):
                yield event

    monkeypatch.setattr("core.llm.factory.create_agent_executor", executor)
    monkeypatch.setattr('core.llm.runtime.streaming.StreamingAgent', ModelStream)
    token = current_execution.set(None)
    steering_token = current_steering.set(None)
    engine = sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", slow_database)
    heartbeat = asyncio.create_task(input_loop_pulse())
    try:
        answer, checkpoint = await runtime.execute_task(ident, binding)
        await heartbeat
    finally:
        pulse.set()
        event.remove(engine, "before_cursor_execute", slow_database)
        heartbeat.cancel()
        current_steering.reset(steering_token)
        current_execution.reset(token)
    with sessions() as db:
        result = AgentContext(db, "owner").tasks.complete(ident, answer, checkpoint, owner="worker")
        assert result["status"] == "completed"
    result = read(sessions, ident)
    assert [m["content"] for m in result["messages"]] == [
        "抓取热搜", "先检查网页", "改用脚本", "已按脚本要求完成",
    ]
    assert result["terminal_message_id"] == result["messages"][-1]["message_id"]


def test_dense_text_stream_keeps_writes_buffered(running, sessions):
    from sqlalchemy import event

    ident, log = running
    log.record("text_delta", "开始")
    log.flush()
    statements = []
    engine = sessions.kw["bind"]

    def observe(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        # Keep this burst inside the existing flush interval without a wall-clock race.
        log.last_flush = time.monotonic() + 60
        for _ in range(100):
            log.record("text_delta", "字")
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert not any(sql.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for sql in statements)
    assert len(statements) <= 100
    update(sessions, ident, "改用脚本", "burst-update")
    log.record("text_delta", "脚本结果")
    log.flush()
    assert [m["content"] for m in read(sessions, ident)["messages"]] == [
        "抓取热搜", "开始" + "字" * 100, "改用脚本", "脚本结果",
    ]
