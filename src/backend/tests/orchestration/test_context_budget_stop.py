
import asyncio

import core.runs.events.writer as writer
import core.runs.ownership as ownership
import core.runs.settings as settings
import pytest
from agentscope.message import UserMsg
from core.db.models import ChatMessage, ChatRun
from core.llm.context.safety import ContextBudgetExceeded, request_admission_scope
from core.llm.runtime.manifest_agent import ManifestBoundAgent
from core.llm.tools.ontology_toolkit import OntologyFilteredToolkit
from core.chat.compaction.budget_checkpoint import ContextBudgetJournal, ContextStopCheckpoint
from core.runs.journal import RunLeaseLost
from tests.llm.test_context_safety import CaptureModel
from tests.orchestration.test_model_steps_pipeline import _result, _run, _step
from tests.orchestration.test_model_steps_pipeline import run_env as run_env


@pytest.mark.asyncio
@pytest.mark.parametrize("swallowed", [False, True])
async def test_budget_stop_saves_checkpoint_and_pauses_worker(run_env, monkeypatch, swallowed):
    monkeypatch.setattr(
        'core.chat.compaction.budget_checkpoint.ContextBudgetJournal',
        lambda *args: ContextBudgetJournal(run_env),
    )
    model = CaptureModel()
    agent = ManifestBoundAgent(
        name="probe",
        system_prompt="rules",
        model=model,
        toolkit=OntologyFilteredToolkit(),
    )

    async def workflow(**kwargs):
        yield _step({"type": "tool_call", "id": "done-tool", "name": "bash", "input": "{}"})
        yield _result("done-tool", "DO-NOT-REPEAT")
        yield {"type": "ai_message", "delta": "saved answer"}
        try:
            await agent.reply(UserMsg(name="user", content="x" * 10000))
        except ContextBudgetExceeded:
            if not swallowed:
                raise
        yield {"type": "meta"}

    run = await _run(workflow, monkeypatch)
    with run_env() as db:
        stored = db.get(ChatRun, run.run_id)
        assert stored.status == "needs_attention"
        stop = stored.recovery_snapshot["context_budget_stop"]
        assert stop["manifest"]["over_budget"] is True
        assert stop["model_context"]
        assert stop["assistant_content"] == "saved answer"
        assert stop["model_steps"][-1]["blocks"][0]["output"] == "DO-NOT-REPEAT"
        row = db.get(ChatMessage, run.message_id)
        assert row.content == "saved answer"
        assert row.model_steps[-1]["blocks"][0]["output"] == "DO-NOT-REPEAT"
    assert model.calls == []


@pytest.mark.asyncio
async def test_checkpoint_failure_never_completes_or_calls_model(run_env, monkeypatch):
    def broken_save(*args):
        raise OSError("checkpoint disk failure")

    monkeypatch.setattr(ContextBudgetJournal, "checkpoint_context", broken_save)
    model = CaptureModel()
    agent = ManifestBoundAgent(
        name="probe",
        system_prompt="rules",
        model=model,
        toolkit=OntologyFilteredToolkit(),
    )

    async def workflow(**kwargs):
        await agent.reply(UserMsg(name="user", content="x" * 10000))
        yield {"type": "meta"}

    run = await _run(workflow, monkeypatch)
    with run_env() as db:
        assert db.get(ChatRun, run.run_id).status == "failed"
    assert model.calls == []


@pytest.mark.asyncio
async def test_stop_latch_blocks_another_agent_in_same_run():
    first = CaptureModel()
    second = CaptureModel()
    second.context_size = 100000
    with request_admission_scope():
        for model, text in [(first, "x" * 10000), (second, "small")]:
            agent = ManifestBoundAgent(
                name="probe",
                system_prompt="rules",
                model=model,
                toolkit=OntologyFilteredToolkit(),
            )
            with pytest.raises(ContextBudgetExceeded):
                await agent.reply(UserMsg(name="user", content=text))
    assert first.calls == second.calls == []


@pytest.mark.asyncio
async def test_unrelated_scope_can_continue():
    model = CaptureModel()
    with request_admission_scope():
        agent = ManifestBoundAgent(
            name="large",
            system_prompt="rules",
            model=model,
            toolkit=OntologyFilteredToolkit(),
        )
        with pytest.raises(ContextBudgetExceeded):
            await agent.reply(UserMsg(name="user", content="x" * 10000))
    model.context_size = 100000
    with request_admission_scope():
        agent = ManifestBoundAgent(
            name="small",
            system_prompt="rules",
            model=model,
            toolkit=OntologyFilteredToolkit(),
        )
        await agent.reply(UserMsg(name="user", content="small"))
    assert len(model.calls) == 1


def test_checkpoint_preserves_post_steer_assistant_identity():
    checkpoint = ContextStopCheckpoint()
    checkpoint.observe(_step({"type": "text", "text": "old"}))
    checkpoint.observe(
        {
            "type": "steer_applied",
            "message_id": "user-steer",
            "next_assistant_message_id": "new-assistant",
        }
    )
    checkpoint.observe({"type": "ai_message", "delta": "new"})
    assert checkpoint.message_id == "new-assistant"
    assert checkpoint.steps == []
    assert checkpoint.content == "new"


def test_lost_owner_cannot_save_budget_checkpoint(run_env, monkeypatch):
    journal = ownership.journal()
    journal.accept(
        run_id="budget-run",
        message_id="budget-msg",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "chat"},
        recovery_snapshot={"kind": "chat"},
    )
    assert journal.claim("budget-run", owner="right-owner", lease_seconds=60)
    monkeypatch.setattr(
        'core.chat.compaction.budget_checkpoint.ContextBudgetJournal',
        lambda *args: ContextBudgetJournal(run_env),
    )
    stopped = ContextBudgetExceeded(
        manifest={"used_tokens": 2, "total_budget": 1}, context=[], summary=""
    )
    with pytest.raises(RunLeaseLost):
        ContextStopCheckpoint().save(
            {"run_id": "budget-run", "journal_owner": "wrong-owner"}, stopped
        )


@pytest.mark.asyncio
async def test_non_chat_run_saves_and_preserves_budget_pause(run_env, monkeypatch):
    from core.llm.runtime.state import AgentRuntimeState
    from core.runs.journal import durable_run_binding

    monkeypatch.setattr(
        'core.chat.compaction.budget_checkpoint.ContextBudgetJournal',
        lambda *args: ContextBudgetJournal(run_env),
    )
    model = CaptureModel()
    with pytest.raises(ContextBudgetExceeded):
        async with durable_run_binding(
            user_id="user-1",
            chat_id="chat-1",
            kind="personal_agent_task",
            external_id="probe",
            session_factory=run_env,
        ) as binding:
            agent = ManifestBoundAgent(
                name="personal",
                system_prompt="rules",
                model=model,
                toolkit=OntologyFilteredToolkit(),
                state=AgentRuntimeState(run_id=binding.run_id, journal_owner=binding.owner),
            )
            await agent.reply(UserMsg(name="user", content="x" * 10000))
    with run_env() as db:
        row = db.get(ChatRun, binding.run_id)
        assert row.status == "needs_attention"
        assert row.recovery_snapshot["context_budget_stop"]["model_context"]
    assert model.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped_error", [False, True])
async def test_queued_completed_events_are_drained_before_stop(run_env, monkeypatch, wrapped_error):
    monkeypatch.setattr(
        'core.chat.compaction.budget_checkpoint.ContextBudgetJournal',
        lambda *args: ContextBudgetJournal(run_env),
    )
    model = CaptureModel()
    agent = ManifestBoundAgent(
        name="queued",
        system_prompt="rules",
        model=model,
        toolkit=OntologyFilteredToolkit(),
    )
    published = []
    original_emit = writer.xadd_event

    async def capture(run_id, offset, event, **kwargs):
        published.append(event)
        return await original_emit(run_id, offset, event, **kwargs)

    monkeypatch.setattr(writer, 'xadd_event', capture)

    async def workflow(**kwargs):
        try:
            await asyncio.create_task(agent.reply(UserMsg(name="user", content="x" * 10000)))
        except ContextBudgetExceeded:
            pass
        yield _step({"type": "tool_call", "id": "queued-call", "name": "bash", "input": "{}"})
        yield _result("queued-call", "QUEUED-COMPLETE-RESULT")
        yield {"type": "ai_message", "delta": "queued answer"}
        if wrapped_error:
            raise RuntimeError("wrapped framework failure")
        yield {"type": "meta"}

    run = await _run(workflow, monkeypatch)
    with run_env() as db:
        stored = db.get(ChatRun, run.run_id)
        stop = stored.recovery_snapshot["context_budget_stop"]
        assert stored.status == "needs_attention"
        assert stop["model_steps"][-1]["blocks"][0]["output"] == "QUEUED-COMPLETE-RESULT"
        assert stop["assistant_content"] == "queued answer"
        assert db.get(ChatMessage, run.message_id).content == "queued answer"
    assert any(event.get("type") == "error" for event in published)
    assert any(event.get("type") == settings.TERMINAL_TYPE for event in published)
    assert not any(event.get("type") == "meta" for event in published)
    assert model.calls == []


@pytest.mark.asyncio
async def test_non_chat_checkpoint_uses_actual_reply_and_completed_output(run_env, monkeypatch):
    from core.llm.runtime.state import AgentRuntimeState
    from core.runs.journal import durable_run_binding
    from personal_agent.tasks.task_threads import StreamRecord

    monkeypatch.setattr(
        'core.chat.compaction.budget_checkpoint.ContextBudgetJournal',
        lambda *args: ContextBudgetJournal(run_env),
    )
    model = CaptureModel()
    with pytest.raises(ContextBudgetExceeded):
        async with durable_run_binding(
            user_id="user-1",
            chat_id="chat-1",
            kind="personal_agent_task",
            external_id="actual-reply",
            session_factory=run_env,
        ) as binding:

            def persist_output():
                record = StreamRecord()
                record.take(
                    "model_step",
                    _step(
                        {
                            "type": "tool_call",
                            "id": "done",
                            "name": "bash",
                            "input": "{}",
                        },
                    )["step"],
                )
                record.take("model_step", _result("done", "PERSONAL-RESULT")["step"])
                record.take("text_delta", "PERSONAL-ANSWER")
                with run_env() as db:
                    row = ChatMessage(
                        message_id="real-personal-reply",
                        chat_id="chat-1",
                        role="assistant",
                        content="",
                    )
                    db.add(row)
                    db.flush()
                    record.store(row, record.text)
                    db.commit()

            await asyncio.to_thread(persist_output)
            agent = ManifestBoundAgent(
                name="personal",
                system_prompt="rules",
                model=model,
                toolkit=OntologyFilteredToolkit(),
                state=AgentRuntimeState(run_id=binding.run_id, journal_owner=binding.owner),
            )
            await agent.reply(UserMsg(name="user", content="x" * 10000))
    with run_env() as db:
        stop = db.get(ChatRun, binding.run_id).recovery_snapshot["context_budget_stop"]
        assert stop["message_id"] == "real-personal-reply"
        assert stop["model_steps"][-1]["blocks"][0]["output"] == "PERSONAL-RESULT"
        assert stop["assistant_content"] == "PERSONAL-ANSWER"
    assert model.calls == []
