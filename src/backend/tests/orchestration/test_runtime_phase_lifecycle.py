"""Exercise real preparation and stream adapters at consumer exit boundaries."""
from core.agent_runtime.integrations import memory_identity as memory_identity_module
from core.agent_runtime.integrations import memory_session as memory_session_module
from core.ontology import output_review_state as output_review_state_module
from core.ontology import reviewer as reviewer_module
from core.llm.runtime import streaming as streaming_module
from core.llm.citations import allocator as allocator_module
from core.harness import log_writer

import asyncio
from types import SimpleNamespace

import pytest
from core.agent_runtime import budget, runner, targets
from core.agent_runtime.integrations import evolution, memory
from core.capabilities.catalog import resolver as catalog_resolver
from core.llm.citations import anchor as citation_anchor
from core.llm import factory
from core.llm.runtime import streaming
from core.ontology import reviewer, validator
from core.harness import log_writer as log_service


@pytest.fixture
def runtime(monkeypatch):
    observed = SimpleNamespace(
        messages=None,
        created=0,
        closed=0,
        stream_closed=0,
        claimed=False,
        logs=[],
        review_started=0,
    )

    async def empty(*args, **kwargs):
        return ""

    async def session(**kwargs):
        return memory_session_module.SessionMemory(
            chat_id="", scope_user_id="", workspace_id="default", memory_enabled=False
        )

    async def create(**kwargs):
        observed.created += 1
        return (
            SimpleNamespace(
                state=SimpleNamespace(ontology_runtime=kwargs["ontology_runtime"]), model=None
            ),
            [],
        )

    class Adapter:
        def __init__(self, agent, clients):
            self.agent = agent

        async def stream(self, messages, context, **kwargs):
            observed.messages = messages
            try:
                yield "text_delta", "Draft"
                yield "user_question", {"id": "question"}
            finally:
                observed.stream_closed += 1

        async def shutdown(self):
            observed.closed += 1

        async def aget_usage(self):
            return {}

        def get_context_usage(self, usage):
            return {}

    def claim(runtime, **kwargs):
        observed.claimed = True
        return True

    def release(runtime, **kwargs):
        observed.claimed = False

    async def review(**kwargs):
        observed.review_started += 1
        return {"verdict": "pass", "answer": kwargs["answer"], "attempts": 1}

    async def finish(*args, **kwargs):
        observed.logs.append(kwargs["status"])

    monkeypatch.setattr(factory, "create_agent_executor", create)
    monkeypatch.setattr(streaming_module, 'StreamingAgent', Adapter)
    monkeypatch.setattr(memory_session_module, 'open_session_memory', session)
    monkeypatch.setattr(memory_session_module, 'resolve_session_memory', empty)
    monkeypatch.setattr(memory_identity_module, 'build_user_identity_block', empty)
    monkeypatch.setattr(allocator_module, 'anchor_start_for_chat', lambda *_: 0)
    monkeypatch.setattr(targets, "_resolve_mode_spec", lambda *_: None)
    monkeypatch.setattr(targets, "_runtime_visible_subagents", lambda *_: [])
    monkeypatch.setattr(
        targets,
        "_load_direct_user_agent",
        lambda *_: SimpleNamespace(name="Direct", extra_config={}),
    )
    for name in (
        "enabled_skill_ids_from_context",
        "enabled_mcp_ids_from_context",
        "enabled_kb_ids_from_context",
    ):
        monkeypatch.setattr(catalog_resolver, name, lambda *_: [])
    monkeypatch.setattr(budget, "_resolve_agent_model_runtime", lambda *_: ("test", 32000))
    monkeypatch.setattr(budget, "_compaction_budget_inputs", lambda *_: {})
    monkeypatch.setattr(output_review_state_module, 'claim_output_review', claim)
    monkeypatch.setattr(output_review_state_module, 'release_output_review', release)
    monkeypatch.setattr(output_review_state_module, 'complete_output_review', release)
    monkeypatch.setattr(reviewer_module, 'review_ontology_output', review)
    monkeypatch.setattr(log_writer, 'start_subagent_log', empty)
    monkeypatch.setattr(log_writer, 'finish_subagent_log', finish)
    monkeypatch.setattr(evolution, "_assemble_episode_background", lambda **_: None)
    return observed


@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("stop", ["thinking", "user_question", "ontology_review", "meta", None])
async def test_real_phases_close_stream_and_review_claim(runtime, direct, stop):
    context = {"user_id": "phase-user", "memory_enabled": False}
    if direct:
        context["agent_id"] = "phase-agent"
    stream = runner.astream_chat_workflow(
        session_messages=[{"role": "user", "content": "Question"}],
        user_message="Question",
        context=context,
    )
    events = []
    try:
        async for event in stream:
            events.append(event)
            if event["type"] == stop:
                break
    finally:
        await stream.aclose()
    assert not runtime.claimed
    if stop == "thinking":
        assert runtime.created == 0
        assert runtime.closed == 0
    else:
        assert runtime.created == 1
        assert runtime.messages[-1]["content"] == "Question"
        assert runtime.stream_closed == 1
        assert runtime.closed >= 1
    if stop == "ontology_review":
        assert runtime.review_started == 0
        assert events[-1]["status"] == "started"
    if direct:
        assert len(runtime.logs) == 1
        assert runtime.logs[0] == ("success" if stop in ("meta", None) else "cancelled")
    if stop is None:
        assert any(event["type"] == "meta" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True])
async def test_cancel_while_review_is_running_joins_task(runtime, monkeypatch, direct):
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def blocking_review(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(reviewer_module, 'review_ontology_output', blocking_review)
    context = {"user_id": "phase-user", "memory_enabled": False}
    if direct:
        context["agent_id"] = "phase-agent"

    async def consume():
        stream = runner.astream_chat_workflow(
            session_messages=[{"role": "user", "content": "Question"}],
            user_message="Question",
            context=context,
        )
        try:
            async for _ in stream:
                pass
        finally:
            await stream.aclose()

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(started.wait(), 2)
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(consumer, 2)
        assert stopped.is_set()
        assert not runtime.claimed
        assert runtime.stream_closed == 1
        assert runtime.closed >= 1
        if direct:
            assert runtime.logs == ["cancelled"]
    finally:
        if not consumer.done():
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
