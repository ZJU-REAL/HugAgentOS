"""Personal Agent Workers on the core chat runtime (SUPERAGENT_DESIGN §6, stage 2).

Runs are real core ChatRuns on a SQLite database: accepted by ChatSequencer and
launched by ``admission.start_run``, with only the asyncio worker left out. A test
plays the run's part by moving it through running to a terminal status.
"""
import time
from types import SimpleNamespace

import pytest
from agentscope.message import TextBlock, ToolCallBlock, ToolResultState
from agentscope.tool._response import ToolChunk
from sqlalchemy import select

from core.chat.sequencer import ChatSequencer
from core.db.models import ChatMessage, ChatRun, ChatSteerQueueItem
from core.db.models.command import CommandTask
from personal_agent.context import AgentContext
from personal_agent.models import PersonalAssignment, PersonalWorkItem
from tests.orchestration.hook_support import make_adapter
from tests.test_personal_agent import sessions, personal_agent_on  # noqa: F401
import personal_agent.telemetry  # noqa: F401  (its table is created with the others)


@pytest.fixture
def world(sessions, monkeypatch):  # noqa: F811
    return make_world(sessions, monkeypatch)


def make_world(sessions, monkeypatch):  # noqa: F811
    """An executor whose runs are admitted and launched for real, on the test database."""
    import core.runs.runtime as runtime
    from personal_agent.worker import executor as module
    from personal_agent.worker import activation
    for target in ("personal_agent.worker.executor.SessionLocal", "personal_agent.worker.tools.SessionLocal",
                   "personal_agent.tasks.guards.SessionLocal", "core.chat.steer.SessionLocal",
                   "core.sandbox.commands.store.SessionLocal", "personal_agent.computer.computer_wait.SessionLocal"):
        monkeypatch.setattr(target, sessions)

    async def quiet(*args, **kwargs):
        return None
    monkeypatch.setattr("core.chat.steer.link_request", quiet)
    monkeypatch.setattr("core.chat.steer._notify", quiet)
    # The run is admitted and launched for real; only the asyncio worker that would stream it is left out.
    monkeypatch.setattr(runtime, "register_run_task", lambda run_id, coro, name: coro.close())
    started = []

    async def start(accepted):
        started.append(accepted)
        await activation.start(accepted)

    async def finalize(goal, text, provider_id=None):
        return text.strip()
    monkeypatch.setattr("personal_agent.tasks.runtime.finalize", finalize)
    return SimpleNamespace(executor=module.Executor(start=start), sessions=sessions, started=started, module=module)


def submit(world, goal="Prepare the weekly report", key="one"):
    with world.sessions() as db:
        return AgentContext(db, "owner").tasks.submit(goal, key)["id"]


def steer(world, task_id, text, key):
    with world.sessions() as db:
        AgentContext(db, "owner").tasks.steer(task_id, text, key)


async def launched(world, goal="Prepare the weekly report", key="one"):
    task_id = submit(world, goal, key)
    asg_id = world.executor.claim()
    await world.executor.launch(asg_id)
    run_id = world.started[-1].run.run_id
    running(world, run_id)
    return task_id, asg_id, run_id


def running(world, run_id):
    with world.sessions() as db:
        assert ChatSequencer(db).mark_running(run_id)


def end_run(world, run_id, status="completed", reason=None):
    with world.sessions() as db:
        assert ChatSequencer(db).release_writer(run_id, terminal_status=status)
        if reason:
            db.get(ChatRun, run_id).failure_reason = reason
            db.commit()


def reply(world, run_id, text):
    """What the run wrote into its preallocated assistant row."""
    with world.sessions() as db:
        db.get(ChatMessage, db.get(ChatRun, run_id).message_id).content = text
        db.commit()


def report(world, task_id, status, summary, question=""):
    from personal_agent.worker.tools import register_report
    tools = []
    register_report(tools, task_id, "owner")
    return tools[0]._func(status, summary, question)


def rows(world, model, **where):
    with world.sessions() as db:
        query = select(model)
        for key, value in where.items():
            query = query.where(getattr(model, key) == value)
        return db.scalars(query).all()


def get(world, model, ident):
    with world.sessions() as db:
        return db.get(model, ident)


def run_context(world, run_id):
    with world.sessions() as db:
        return db.get(ChatRun, run_id).recovery_snapshot["worker_args"]["context"]


def command(world, chat_id, run_id, ident="cmd_long", status="running", background=True, notice="none"):
    with world.sessions() as db:
        row = db.get(CommandTask, ident)
        if row is None:
            row = CommandTask(task_id=ident, chat_id=chat_id, user_id="owner", sandbox_session_id="box",
                              source_run_id=run_id, command="sleep 30; echo done", status=status, background=background)
            db.add(row)
        row.status, row.notice_status = status, notice
        if status not in ("starting", "running"):
            row.exit_code = 0
        db.commit()


# ── launch ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_task_runs_as_a_core_chat_run_of_the_worker_profile_on_its_own_session(world):
    task_id, asg_id, run_id = await launched(world)
    task = get(world, PersonalWorkItem, task_id)
    asg = get(world, PersonalAssignment, asg_id)
    run = get(world, ChatRun, run_id)
    assert (task.status, task.run_id, task.owner) == ("running", asg_id, world.executor.identity)
    assert (asg.status, asg.slot, asg.run_id, asg.chat_id) == ("running", "task", run_id, task.chat_id)
    assert run.chat_id == task.chat_id and run.request_payload["wake_reason"] == "personal_task"
    context = run_context(world, run_id)
    assert (context["profile"], context["profile_ref"], context["automation_run"]) == ("personal_agent", task_id, True)
    assert context["sandbox_session_id"].startswith("pa_box_") and "chat_mode" in context and "enable_thinking" in context
    # The history is the session's own: a hidden brief, then the goal.
    history = world.started[-1].history
    assert "Task ID (<task_id> in the rules): " + task_id in history[0]["content"]
    assert history[-1] == {"role": "user", "content": "Prepare the weekly report"}
    with world.sessions() as db:
        from personal_agent.tasks.task_threads import history as projection
        shown = projection(AgentContext(db, "owner"), task_id)["messages"]
    assert [m["role"] for m in shown] == ["user", "assistant"] and shown[0]["content"] == "Prepare the weekly report"
    assert shown[1]["is_streaming"]


@pytest.mark.asyncio
async def test_a_ready_report_settles_the_task_and_ends_the_assignment(world):
    from personal_agent.conversation.message_routing import pending
    task_id, asg_id, run_id = await launched(world)
    await report(world, task_id, "progress", "Collected the numbers")
    assert get(world, PersonalWorkItem, task_id).checkpoint["progress"] == {"summary": "Collected the numbers"}
    await report(world, task_id, "ready", "The report is ready.")
    await world.executor.step(asg_id)
    assert get(world, PersonalWorkItem, task_id).status == "running"  # The run is still live.
    end_run(world, run_id)
    await world.executor.step(asg_id)
    task, asg = get(world, PersonalWorkItem, task_id), get(world, PersonalAssignment, asg_id)
    assert (task.status, task.result) == ("completed", "The report is ready.")
    assert (asg.status, asg.ended_reason) == ("ended", "finished")
    with world.sessions() as db:
        assert len(pending(AgentContext(db, "owner"))) == 1  # The agent tells the user in its next turn.


@pytest.mark.asyncio
async def test_without_a_report_the_last_reply_is_the_result(world):
    task_id, asg_id, run_id = await launched(world)
    reply(world, run_id, "Here is the summary.")
    end_run(world, run_id)
    await world.executor.step(asg_id)
    assert get(world, PersonalWorkItem, task_id).result == "Here is the summary."


@pytest.mark.asyncio
async def test_a_blocked_report_waits_for_the_answer_which_resumes_the_same_session(world):
    task_id, asg_id, run_id = await launched(world)
    await report(world, task_id, "blocked", "Need the floor", "Which floor?")
    end_run(world, run_id)
    await world.executor.step(asg_id)
    task = get(world, PersonalWorkItem, task_id)
    assert (task.status, task.checkpoint["pending_question"]["text"]) == ("waiting", "Which floor?")
    with world.sessions() as db:
        from personal_agent.tasks import task_ops
        svc = AgentContext(db, "owner")
        message = svc.conversation.append(svc.identity.agent(), "user", "The third", None, "answer")
        task_ops.update_task(svc, message, task_id, "The third")
        db.commit()
    assert get(world, PersonalWorkItem, task_id).status == "queued"
    second = world.executor.claim()
    await world.executor.launch(second)
    resumed = world.started[-1]
    assert resumed.chat_id == task.chat_id and resumed.content == "The third"
    assert [m["content"] for m in resumed.history if m["role"] == "user"][-2:] == ["Prepare the weekly report", "The third"]


# ── delivery ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_three_messages_during_a_long_bash_arrive_at_one_boundary_and_its_end_wakes_the_worker(world):
    from personal_agent.extension import _steering
    from core.sandbox.commands import delivery, store
    task_id, asg_id, run_id = await launched(world)
    chat_id = get(world, PersonalWorkItem, task_id).chat_id
    middleware = make_adapter(*_steering(None).specs())
    agent = SimpleNamespace(state=SimpleNamespace(run_id=run_id, context=[], steer_delivery=None))
    texts = ["Use last week's numbers", "Add a chart", "Send it to Ann"]

    async def bash(**_kwargs):
        # `sleep 30; echo done` passes yield_time_ms and continues in the background.
        command(world, chat_id, run_id)
        for index, text in enumerate(texts):
            steer(world, task_id, text, "u" + str(index))
            await world.executor.step(asg_id)
        yield ToolChunk(content=[TextBlock(type="text", text='{"status": "running"}')], state=ToolResultState.SUCCESS)

    results = [item async for item in middleware.on_acting(
        agent, {"tool_call": ToolCallBlock(id="t", name="Bash", input="{}")}, bash)]
    assert results[-1].state == ToolResultState.SUCCESS  # The issued command is not interrupted.
    before = len(agent.state.context)

    async def model(**_kwargs):
        yield "continued"
    assert [item async for item in middleware.on_reasoning(agent, {}, model)] == ["continued"]
    # One boundary, one injected item, all three in order.
    assert len(agent.state.context) == before + 1
    text = agent.state.context[-1].content[0].text
    assert text.index(texts[0]) < text.index(texts[1]) < text.index(texts[2])
    assert [item["message"] for item in agent.state.steer_delivery["batch"]] == texts
    assert {row.status for row in rows(world, ChatSteerQueueItem, target_run_id=run_id)} == {"claimed"}

    # The reply ends while the command runs: the Assignment keeps its slot and waits.
    end_run(world, run_id)
    await world.executor.step(asg_id)
    assert get(world, PersonalAssignment, asg_id).status == "running"
    assert get(world, PersonalWorkItem, task_id).status == "running"
    assert len(rows(world, ChatRun, chat_id=chat_id)) == 1

    # The command finishes: core wakes the Worker session with the Worker profile.
    command(world, chat_id, run_id, status="completed", notice="pending")
    await world.executor.step(asg_id)
    wake = delivery.prepare(store.pending_notices(chat_id), "commands")
    woken = get(world, ChatRun, wake)
    assert woken.chat_id == chat_id and woken.request_payload["wake_reason"] == "command_finish"
    assert run_context(world, run_id)["profile"] == "personal_agent"
    delivery.acknowledge(["cmd_long"], wake)
    running(world, wake)
    await world.executor.step(asg_id)
    assert get(world, PersonalAssignment, asg_id).run_id == wake

    await report(world, task_id, "ready", "Report sent to Ann.")
    end_run(world, wake)
    await world.executor.step(asg_id)
    assert get(world, PersonalWorkItem, task_id).status == "completed"


@pytest.mark.asyncio
async def test_updates_for_an_idle_worker_wake_it_once_with_all_of_them(world):
    task_id, asg_id, run_id = await launched(world)
    command(world, get(world, PersonalWorkItem, task_id).chat_id, run_id)  # Keeps the Assignment open.
    end_run(world, run_id)
    steer(world, task_id, "First change", "u1")
    steer(world, task_id, "Second change", "u2")
    await world.executor.step(asg_id)
    woken = world.started[-1]
    assert woken.run.run_id != run_id and woken.content == "First change\n\nSecond change"
    assert get(world, PersonalAssignment, asg_id).run_id == woken.run.run_id
    running(world, woken.run.run_id)
    await world.executor.step(asg_id)
    assert len(world.started) == 2  # Delivered once.


@pytest.mark.asyncio
async def test_input_steered_into_a_run_that_died_comes_back_with_the_retry(world):
    task_id, asg_id, run_id = await launched(world)
    steer(world, task_id, "Use the new template", "u1")
    await world.executor.step(asg_id)
    assert len(rows(world, ChatSteerQueueItem, target_run_id=run_id)) == 1
    end_run(world, run_id, "failed", "Error code: 429 - rate limited")
    await world.executor.step(asg_id)
    asg = get(world, PersonalAssignment, asg_id)
    assert asg.status == "running" and asg.retries == 1 and asg.retry_at > time.time() + 10
    with world.sessions() as db:
        db.get(PersonalAssignment, asg_id).retry_at = time.time() - 1
        db.commit()
    await world.executor.step(asg_id)
    retried = world.started[-1]
    assert retried.content == "Use the new template"
    assert "临时故障" in retried.history[-2]["content"]


@pytest.mark.asyncio
async def test_transient_failures_retry_three_times_then_the_task_fails(world):
    task_id, asg_id, run_id = await launched(world)
    for attempt in range(3):
        end_run(world, run_id, "failed", "503 Service Unavailable")
        await world.executor.step(asg_id)
        with world.sessions() as db:
            db.get(PersonalAssignment, asg_id).retry_at = time.time() - 1
            db.commit()
        await world.executor.step(asg_id)
        run_id = world.started[-1].run.run_id
        assert world.started[-1].content.startswith("继续")
        running(world, run_id)
    end_run(world, run_id, "failed", "503 Service Unavailable")
    await world.executor.step(asg_id)
    task = get(world, PersonalWorkItem, task_id)
    assert task.status == "failed" and get(world, PersonalAssignment, asg_id).ended_reason == "failed"


@pytest.mark.asyncio
async def test_other_failures_fail_at_once_and_unknown_outcomes_wait_for_the_user(world):
    task_id, asg_id, run_id = await launched(world)
    end_run(world, run_id, "failed", "ValueError: bad tool arguments")
    await world.executor.step(asg_id)
    assert get(world, PersonalWorkItem, task_id).status == "failed"
    task_id, asg_id, run_id = await launched(world, "Send the mail", "two")
    with world.sessions() as db:
        ChatSequencer(db).release_writer(run_id, terminal_status="completed")
        db.get(ChatRun, run_id).status = "needs_attention"
        db.commit()
    await world.executor.step(asg_id)
    task = get(world, PersonalWorkItem, task_id)
    assert (task.status, task.waiting_reason) == ("waiting", "操作结果未知，需核对后继续")


# ── slots and background work ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_slots_hold_the_owner_limit_and_a_follow_up_takes_the_extra_slot(world):
    for index in range(4):
        submit(world, "Independent task " + str(index), str(index))
    claimed = [world.executor.claim() for _ in range(3)]
    assert all(claimed) and world.executor.claim() is None  # Per-owner limit.
    # A task that ran before and has a new message from the user.
    earlier = submit(world, "Earlier task", "earlier")
    with world.sessions() as db:
        task = db.get(PersonalWorkItem, earlier)
        db.add(PersonalAssignment(id="pa_asg_old", work_item_id=earlier, agent_id=task.agent_id,
                                  chat_id="pa_chat_task_old", status="ended"))
        db.commit()
    steer(world, earlier, "One more thing", "follow")
    extra = world.executor.claim()
    assert get(world, PersonalAssignment, extra).slot == "follow_up"
    assert get(world, PersonalAssignment, extra).work_item_id == earlier
    assert world.executor.claim() is None  # One extra slot only.


@pytest.mark.asyncio
async def test_background_commands_past_the_limit_are_stopped_and_the_task_waits(world, monkeypatch):
    stopped = []

    class Commands:
        async def cancel(self, tid, user_id, session_id):
            stopped.append(tid)
    monkeypatch.setattr("core.llm.ports.command_execution", lambda: Commands())
    task_id, asg_id, run_id = await launched(world)
    command(world, get(world, PersonalWorkItem, task_id).chat_id, run_id)
    end_run(world, run_id)
    await world.executor.step(asg_id)
    with world.sessions() as db:
        db.get(PersonalAssignment, asg_id).idle_since = time.time() - world.module.BACKGROUND_LIMIT - 1
        db.commit()
    await world.executor.step(asg_id)
    task = get(world, PersonalWorkItem, task_id)
    assert stopped == ["cmd_long"] and task.status == "waiting"
    assert task.checkpoint["killed_commands"] == ["sleep 30; echo done"]
    assert get(world, PersonalAssignment, asg_id).ended_reason == "background_limit"
    with world.sessions() as db:
        AgentContext(db, "owner").tasks.retry(task_id)
    await world.executor.launch(world.executor.claim())
    assert "sleep 30; echo done" in world.started[-1].history[-2]["content"]


@pytest.mark.asyncio
async def test_handing_over_releases_the_slot_and_leaves_the_users_process_running_quietly(world):
    task_id, asg_id, run_id = await launched(world)
    chat_id = get(world, PersonalWorkItem, task_id).chat_id
    command(world, chat_id, run_id, ident="cmd_preview")
    await report(world, task_id, "ready", "The preview runs on port 8080.")
    end_run(world, run_id)
    await world.executor.step(asg_id)
    assert get(world, PersonalWorkItem, task_id).status == "completed"
    preview = get(world, CommandTask, "cmd_preview")
    assert preview.status == "running" and not preview.background  # Its end will not wake the Worker.


@pytest.mark.asyncio
async def test_a_stopped_task_cancels_its_run_and_commands(world, monkeypatch):
    cancelled, stopped = [], []

    async def cancel_run(run_id, *, user_id):
        cancelled.append(run_id)

    class Commands:
        async def cancel(self, tid, user_id, session_id):
            stopped.append(tid)
    monkeypatch.setattr("core.runs.runtime.cancel_run", cancel_run)
    monkeypatch.setattr("core.llm.ports.command_execution", lambda: Commands())
    task_id, asg_id, run_id = await launched(world)
    command(world, get(world, PersonalWorkItem, task_id).chat_id, run_id)
    with world.sessions() as db:
        AgentContext(db, "owner").tasks.cancel(task_id)
    await world.executor.step(asg_id)
    assert cancelled == [run_id] and stopped == ["cmd_long"]
    assert get(world, PersonalAssignment, asg_id).ended_reason == "stopped"


# ── recovery ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recovery_takes_live_runs_over_and_restarts_dead_ones_with_a_note(world):
    from personal_agent.worker.executor import Executor
    live_task, live_asg, live_run = await launched(world)
    dead_task, dead_asg, dead_run = await launched(world, "Second task", "two")
    end_run(world, dead_run, "failed", "worker lost")
    with world.sessions() as db:
        for ident in (live_asg, dead_asg):
            db.get(PersonalAssignment, ident).lease_until = time.time() - 1
        db.commit()
    other = Executor(start=world.executor.start_run)
    other.recover()
    assert get(world, PersonalAssignment, live_asg).owner == other.identity
    assert get(world, PersonalWorkItem, live_task).owner == other.identity
    assert get(world, PersonalAssignment, dead_asg).ended_reason == "restarted"
    dead = get(world, PersonalWorkItem, dead_task)
    assert dead.status == "queued" and dead.checkpoint["recovery_note"]
    await other.launch(other.claim())
    restarted = world.started[-1]
    assert restarted.chat_id == dead.chat_id and "服务重启" in restarted.history[-2]["content"]
    assert "recovery_note" not in get(world, PersonalWorkItem, dead_task).checkpoint


# ── tools and profile ───────────────────────────────────────────────────


def test_worker_profile_exposes_core_bash_and_steers_in_batches():
    from core.chat.steer_hook import SteeringHook
    from personal_agent.extension import WORKER_TOOLS, _steering
    hook = _steering(None)
    assert isinstance(hook, SteeringHook) and hook.take_batch and not hook.interrupt_tools
    assert WORKER_TOOLS == {"Bash", "write_stdin", "list_processes", "load_plugin"}


@pytest.mark.asyncio
async def test_bash_runs_guarded_in_the_task_folder_with_redacted_credentials(world, monkeypatch):
    from core.llm.factory.tools.definitions import function_tool
    from personal_agent.worker import tools as worker_tools
    calls = []

    def core_bash(toolkit, **options):
        async def Bash(command: str, timeout: int | None = None, yield_time_ms: int = 60000, run_in_background: bool = False) -> ToolChunk:
            """默认 60000"""
            calls.append((command, yield_time_ms, options))
            token = secrets[0][1].split("HUGAGENT_API_KEY=")[1].split()[0]  # The command may print its credential.
            return ToolChunk(content=[TextBlock(type="text", text='{"status": "running", "stdout": "' + token + '"}')])

        async def write_stdin(session_id: str, chars: str = "", yield_time_ms: int = 0) -> ToolChunk:
            return ToolChunk(content=[TextBlock(type="text", text="{}")])

        async def list_processes() -> ToolChunk:
            return ToolChunk(content=[TextBlock(type="text", text="{}")])
        for fn in (Bash, write_stdin, list_processes):
            toolkit.append(function_tool(fn))

    async def nothing(*args, **kwargs):
        return None

    async def mint(task_id, uid, ttl):
        minted.append(ttl)
        return "pa_cli_" + "s" * 40
    minted = []
    monkeypatch.setattr("personal_agent.sessions.session_auth.mint", mint)
    monkeypatch.setattr("core.llm.tools.register_bash", core_bash)
    monkeypatch.setattr("personal_agent.sessions.session_cli_install.ensure_cli", nothing)
    secrets = []

    class Provider:
        async def put_secret(self, box, content, user_id=None):
            secrets.append((box, content, user_id))
            return "/tmp/pa-secret-1"
    monkeypatch.setattr("personal_agent.computer.sandbox.get_personal_sandbox_provider", Provider)
    task_id, asg_id, run_id = await launched(world)
    request = SimpleNamespace(run_id=run_id, chat_id="chat", sandbox_session_id="pa_box_1")
    toolkit = []
    worker_tools.register_bash(toolkit, request, None, task_id, "owner")
    bash = {tool.name: tool for tool in toolkit}["Bash"]
    result = await bash._func("sleep 30; echo done")
    script, yield_ms, options = calls[0]
    assert yield_ms == 5000 and "默认 5000" in bash._func.__doc__
    # The command text is persisted with the command record: the credential only travels in the secret file.
    assert "pa_cli_" not in script and script.startswith(". /tmp/pa-secret-1; rm -f /tmp/pa-secret-1\n")
    assert secrets[0][0].startswith("pa_box_") and "HUGAGENT_API_KEY=pa_cli_" in secrets[0][1] and secrets[0][2] == "owner"
    await bash._func("sleep 30", yield_time_ms=60000)
    assert calls[1][1] == 5000  # A longer wait would keep new input from the Worker.
    assert minted[0] == world.module.BACKGROUND_LIMIT + 60  # The credential outlives a background command.
    assert "cd /workspace/work/" + task_id + "\nsleep 30; echo done" in script
    assert options["source_run_id"] == run_id and options["sandbox_session_id"] == "pa_box_1"
    assert "pa_cli_" not in result.content[0].text and "[task credential]" in result.content[0].text
    with world.sessions() as db:
        AgentContext(db, "owner").tasks.cancel(task_id)
    with pytest.raises(PermissionError):
        await bash._func("ls")


@pytest.mark.parametrize("text, waits", [
    ("sleep 50; echo waited", True), ("sleep 5", True), ("sleep 1m && echo ok", True), ("sleep 2; sleep 3; true", True),
    ("sleep 60; echo done-e2e-d", True), ("sleep 2; cat out.txt", False), ("echo $(sleep 5)", False),
    ("sleep 5 & python run.py", False), ("ls", False), ("sleep 5; echo `id`", False),
    # Real scripts that merely start with a short wait still run.
    ("sleep 5\necho checking\ncurl -s http://localhost:8000/health -o out.json\npython3 report.py out.json", False),
    ("sleep 3 && echo ok\nrm -rf build\nnpm run build", False), ("sleep 1; echo x\ngit push origin main", False),
    ("sleep 10; echo waited > file", False),
])
def test_wait_only_commands_are_recognised(text, waits):
    from personal_agent.worker.tools import waits_only
    assert waits_only(text) is waits


def test_wait_check_is_linear_on_long_scripts():
    import time
    from personal_agent.worker.tools import waits_only
    started = time.perf_counter()
    assert not waits_only("sleep 1\n" + "echo a\n" * 5000 + "|")
    assert time.perf_counter() - started < 0.5


@pytest.mark.asyncio
async def test_a_sleep_that_only_waits_for_a_running_command_is_refused(world, monkeypatch):
    from core.llm.factory.tools.definitions import function_tool
    from personal_agent.worker import tools as worker_tools
    calls = []

    def core_bash(toolkit, **options):
        async def Bash(command: str, timeout: int | None = None, yield_time_ms: int = 60000, run_in_background: bool = False) -> ToolChunk:
            calls.append(command)
            return ToolChunk(content=[TextBlock(type="text", text='{"status": "exited", "exit_code": 0}')])

        async def write_stdin(session_id: str, chars: str = "", yield_time_ms: int = 0) -> ToolChunk:
            return ToolChunk(content=[TextBlock(type="text", text="{}")])

        async def list_processes() -> ToolChunk:
            return ToolChunk(content=[TextBlock(type="text", text="{}")])
        for fn in (Bash, write_stdin, list_processes):
            toolkit.append(function_tool(fn))

    async def nothing(*args, **kwargs):
        return None

    async def mint(task_id, uid, ttl):
        return "pa_cli_" + "s" * 40

    class Provider:
        async def put_secret(self, box, content, user_id=None):
            return "/tmp/pa-secret-1"
    monkeypatch.setattr("personal_agent.sessions.session_auth.mint", mint)
    monkeypatch.setattr("core.llm.tools.register_bash", core_bash)
    monkeypatch.setattr("personal_agent.sessions.session_cli_install.ensure_cli", nothing)
    monkeypatch.setattr("personal_agent.computer.sandbox.get_personal_sandbox_provider", Provider)
    monkeypatch.setattr("core.infra.ephemeral.get_ephemeral_state", lambda: SimpleNamespace(drop=nothing))
    task_id, asg_id, run_id = await launched(world)
    chat_id = "pa_chat_task_" + task_id.removeprefix("pa_task_")
    toolkit = []
    worker_tools.register_bash(toolkit, SimpleNamespace(run_id=run_id, chat_id=chat_id, sandbox_session_id="b"), None, task_id, "owner")
    bash = {tool.name: tool for tool in toolkit}["Bash"]
    # Nothing else running: a sleep may be the user's own command.
    await bash._func("sleep 60; echo done")
    assert len(calls) == 1
    command(world, chat_id, run_id)
    refused = worker_tools.payload(await bash._func("sleep 50; echo waited"))
    assert len(calls) == 1 and "cmd_long" in refused["error"] and "End your reply" in refused["next"]
    await bash._func("cat result.txt")  # Real work alongside the running command still runs.
    assert len(calls) == 2
    stdin = {tool.name: tool for tool in toolkit}["write_stdin"]
    assert worker_tools.payload(await stdin._func("cmd_long")) == {}  # One look at its output is fine.
    again = worker_tools.payload(await stdin._func("cmd_long", yield_time_ms=3000))
    assert "still running" in again["error"] and "End your reply" in again["next"]
    assert worker_tools.payload(await stdin._func("cmd_long", "y\n")) == {}  # Real input still goes through.
    command(world, chat_id, run_id, status="completed")
    await bash._func("sleep 1")
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_telemetry_follows_the_assignment_across_its_core_runs(world):
    from core.harness.hooks import HookStage, Invocation
    from personal_agent.telemetry import PersonalAssignmentTelemetry, hooks
    task_id, asg_id, _ = await launched(world)
    for run in range(2):
        tool, model, finish = hooks(task_id, asg_id)
        await tool.handler(Invocation.create(run_id="r", stage=HookStage.BEFORE_TOOL, operation_name="Bash",
                                             data={"raw_input": {"command": "ls " + str(run)}}))
        await model.handler(Invocation.create(run_id="r", stage=HookStage.AFTER_MODEL, operation_name="model",
                                              data={"response": {"usage": {"input_tokens": 10, "output_tokens": 2}}}))
        await finish.handler(Invocation.create(run_id="r", stage=HookStage.BEFORE_FINISH, operation_name="agent_reply"))
    [row] = rows(world, PersonalAssignmentTelemetry)
    assert (row.work_item_id, row.run_id, row.steps, row.tool_counts) == (task_id, asg_id, 2, {"Bash": 2})
    assert (row.prompt_tokens, row.completion_tokens) == (20, 4)


@pytest.mark.parametrize("filename", [
    "src/backend/alembic/versions/paassign01_personal_assignments.py",
    "ce/overlay/src/backend/alembic/versions/ce_0039_personal_assignments.py",
])
def test_assignment_migration_creates_and_drops_the_table(filename):
    import importlib.util
    from sqlalchemy import create_engine, inspect, text
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    spec = importlib.util.spec_from_file_location("assignment_migration", filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision in ("pahealth01", "ce_0038")
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE personal_agents (id TEXT PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE personal_work_items (id TEXT PRIMARY KEY)"))
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            module.upgrade()
            assert "personal_assignments" in inspect(conn).get_table_names()
            module.downgrade()
            assert "personal_assignments" not in inspect(conn).get_table_names()


# ── review regressions ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_superseded_assignment_leaves_its_successors_run_alone(world):
    task_id, a_id, r1 = await launched(world)
    end_run(world, r1)
    with world.sessions() as db:
        task = db.get(PersonalWorkItem, task_id); task.pause("用户暂停了任务"); db.commit()
        task = db.get(PersonalWorkItem, task_id); task.requeue(); db.commit()
    b_id = world.executor.claim()
    await world.executor.launch(b_id)
    r2 = world.started[-1].run.run_id
    running(world, r2)
    s = world.executor.observe(a_id)
    assert s.kind == "stop" and s.run_id == "" and s.commands == []


@pytest.mark.asyncio
async def test_a_failed_task_no_longer_wakes_on_its_background_command(world):
    task_id, a_id, r1 = await launched(world)
    asg = get(world, PersonalAssignment, a_id)
    command(world, asg.chat_id, r1)
    end_run(world, r1, "failed", "BadRequest: context too long")
    await world.executor.step(a_id)
    assert get(world, PersonalWorkItem, task_id).status == "failed"
    assert get(world, CommandTask, "cmd_long").background is False


@pytest.mark.asyncio
async def test_a_crash_before_activation_requeues_instead_of_failing(world):
    task_id, a_id, r1 = await launched(world)
    report(world, task_id, "progress", "half way")
    end_run(world, r1)
    with world.sessions() as db:
        db.get(PersonalAssignment, a_id).status = "ended"
        task = db.get(PersonalWorkItem, task_id); task.pause("x"); task.requeue(); db.commit()
    b_id = world.executor.claim()
    with world.sessions() as db:
        b = db.get(PersonalAssignment, b_id); b.lease_until, b.owner = 0, "dead"
        task = db.get(PersonalWorkItem, task_id); task.lease_until, task.owner = 0, "dead"; db.commit()
    world.executor.recover()
    assert get(world, PersonalAssignment, b_id).ended_reason == "restarted"
    assert get(world, PersonalWorkItem, task_id).status == "queued"


@pytest.mark.asyncio
async def test_a_step_that_keeps_failing_fails_the_task(world):
    task_id, a_id, r1 = await launched(world)
    steer(world, task_id, "also use this file", "upd1")
    with world.sessions() as db:
        task = db.get(PersonalWorkItem, task_id)
        task.checkpoint = {**(task.checkpoint or {}), "input_artifact_ids": ["art_deleted"]}; db.commit()
    for _ in range(world.module.STEP_FAILURES - 1):
        with pytest.raises(PermissionError):
            await world.executor.step(a_id)
    await world.executor.step(a_id)
    assert get(world, PersonalAssignment, a_id).status == "ended"
    assert get(world, PersonalWorkItem, task_id).status == "failed"
