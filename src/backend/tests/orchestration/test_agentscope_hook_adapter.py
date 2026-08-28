"""AgentScope fake events map to stable neutral Invocations."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultState, UserMsg
from agentscope.middleware import MiddlewareBase
from agentscope.tool._response import ToolResponse
from core.harness.hooks import Decision, HookBus, HookContractError, HookSpec, HookStage
from core.harness.usage import MemoryUsageRecorder
from core.llm.agentscope_hook_adapter import (
    AgentScopeHookAdapter,
    adapt_agentscope_event,
)
from core.llm.middleware_migration import MIDDLEWARE_MIGRATION, deletion_checklist


def _fake(name: str, **fields):
    event = type(name, (), {})()
    for field, value in fields.items():
        setattr(event, field, value)
    return event


def test_fake_model_and_tool_events_have_stable_neutral_shape():
    start = adapt_agentscope_event(
        _fake("ModelCallStartEvent", model_name="provider/model"),
        run_id="run-adapter",
    )
    end = adapt_agentscope_event(
        _fake("ModelCallEndEvent", input_tokens=12, output_tokens=4),
        run_id="run-adapter",
    )
    tool = adapt_agentscope_event(
        _fake(
            "ToolResultEndEvent",
            tool_call_name="write",
            tool_call_id="tc-1",
            state="success",
        ),
        run_id="run-adapter",
    )

    assert start.stage == HookStage.BEFORE_MODEL
    assert start.operation_name == "provider/model"
    assert end.stage == HookStage.AFTER_MODEL
    assert end.data["response"] == {
        "prompt_tokens": 12,
        "completion_tokens": 4,
    }
    assert tool.stage == HookStage.AFTER_TOOL
    assert tool.operation_name == "write"
    assert tool.data["metadata"]["tool_call_id"] == "tc-1"


def test_every_registered_business_middleware_has_a_deletion_entry():
    expected = {
        "DynamicModelMiddleware",
        "FileContextMiddleware",
        "SteerMiddleware",
        "WorkspacePinHintMiddleware",
        "IterBudgetReminderMiddleware",
        "StallInterventionMiddleware",
        "OntologyGateMiddleware",
        "ToolPermissionMiddleware",
        "CitationAnchorMiddleware",
        "ActingToolCallIdMiddleware",
        "ToolEffectMiddleware",
        "JobLedgerReminderMiddleware",
        "FinishPinGuardMiddleware",
    }
    assert {item.middleware for item in MIDDLEWARE_MIGRATION} == expected
    assert all(item.status == "adapter-covered" for item in MIDDLEWARE_MIGRATION)
    assert all(item.delete_after for item in MIDDLEWARE_MIGRATION)
    assert len(deletion_checklist()) == len(expected)


@pytest.mark.asyncio
async def test_adapter_applies_context_and_observes_model_result():
    bus = HookBus()
    bus.register(
        HookSpec(
            "context",
            HookStage.TRANSFORM_CONTEXT,
            lambda invocation: Decision.modify(
                {"messages": ["changed"], "context": {"source": "hook"}}
            ),
            mutable_fields=frozenset({"messages", "context"}),
        )
    )
    adapter = AgentScopeHookAdapter(bus)
    agent = SimpleNamespace(
        state=SimpleNamespace(run_id="run-adapter", context={"source": "old"})
    )

    seen_reply = {}

    async def reply_handler(**kwargs):
        seen_reply.update(kwargs)
        if False:
            yield None

    assert [
        item async for item in adapter.on_reply(agent, {"inputs": "old"}, reply_handler)
    ] == []
    assert seen_reply == {"inputs": ["changed"]}
    assert agent.state.context == {"source": "hook"}

    seen_model = {}

    async def model_handler(**kwargs):
        seen_model.update(kwargs)
        return {"answer": "original"}

    result = await adapter.on_model_call(
        agent,
        {
            "current_model": SimpleNamespace(model="original-model", max_retries=0),
            "messages": ["original-prompt"],
            "tools": [],
            "tool_choice": None,
        },
        model_handler,
    )
    assert seen_model["current_model"].model == "original-model"
    assert seen_model["messages"] == ["original-prompt"]
    assert seen_model["tools"] == []
    assert result == {"answer": "original"}


@pytest.mark.asyncio
async def test_adapter_restores_hook_appended_message_dicts_to_agentscope_msgs():
    bus = HookBus()

    def append_message(invocation):
        return Decision.modify(
            {
                "messages": [
                    invocation.data["messages"],
                    UserMsg(name="hook", content="added").model_dump(mode="json"),
                ]
            }
        )

    bus.register(
        HookSpec(
            "append-message",
            HookStage.TRANSFORM_CONTEXT,
            append_message,
            mutable_fields=frozenset({"messages"}),
        )
    )
    adapter = AgentScopeHookAdapter(bus)
    agent = SimpleNamespace(state=SimpleNamespace(run_id="run-adapter", context=[]))
    seen = {}

    async def handler(**kwargs):
        seen.update(kwargs)
        if False:
            yield None

    original = UserMsg(name="user", content="original")
    assert [
        item async for item in adapter.on_reply(agent, {"inputs": original}, handler)
    ] == []
    assert len(seen["inputs"]) == 2
    assert all(isinstance(item, Msg) for item in seen["inputs"])
    assert seen["inputs"][1].get_text_content() == "added"


@pytest.mark.asyncio
async def test_adapter_restores_messages_when_original_inputs_are_empty():
    injected = UserMsg(name="hook", content="injected").model_dump(mode="json")
    bus = HookBus()
    bus.register(
        HookSpec(
            "inject-message",
            HookStage.TRANSFORM_CONTEXT,
            lambda invocation: Decision.modify(
                {"messages": [injected], "context": [injected]}
            ),
            mutable_fields=frozenset({"messages", "context"}),
        )
    )
    adapter = AgentScopeHookAdapter(bus)
    agent = SimpleNamespace(state=SimpleNamespace(run_id="run-adapter", context=[]))
    seen = {}

    async def handler(**kwargs):
        seen.update(kwargs)
        if False:
            yield None

    assert [
        item async for item in adapter.on_reply(agent, {"inputs": None}, handler)
    ] == []
    assert isinstance(seen["inputs"][0], Msg)
    assert isinstance(agent.state.context[0], Msg)


def test_model_and_tool_inputs_are_read_only_at_post_manifest_permission_seams():
    bus = HookBus()
    with pytest.raises(HookContractError, match="outside before_model"):
        bus.register(
            HookSpec(
                "rewrite-model",
                HookStage.BEFORE_MODEL,
                lambda invocation: Decision.modify({"model": "unsafe"}),
                mutable_fields=frozenset({"model"}),
            )
        )
    with pytest.raises(HookContractError, match="outside before_tool"):
        bus.register(
            HookSpec(
                "rewrite-tool",
                HookStage.BEFORE_TOOL,
                lambda invocation: Decision.modify({"tool_args": {"unsafe": True}}),
                mutable_fields=frozenset({"tool_args"}),
            )
        )


@pytest.mark.asyncio
async def test_legacy_policy_executes_only_through_adapter_and_is_accounted():
    class LegacyPolicy(MiddlewareBase):
        async def on_reply(self, agent, input_kwargs, next_handler):
            async for item in next_handler(**input_kwargs):
                yield item

    usage = MemoryUsageRecorder()
    adapter = AgentScopeHookAdapter(
        HookBus(usage_recorder=usage),
        legacy_middlewares=(LegacyPolicy(),),
    )
    agent = SimpleNamespace(state=SimpleNamespace(run_id="run-adapter", context=[]))

    async def handler(**kwargs):
        yield UserMsg(name="assistant", content="done")

    events = [item async for item in adapter.on_reply(agent, {"inputs": None}, handler)]
    assert len(events) == 1
    rows = usage.attempts("run-adapter")
    assert [(row.kind, row.operation_name, row.status) for row in rows] == [
        ("hook", "LegacyPolicy.on_reply", "success")
    ]


@pytest.mark.asyncio
async def test_adapter_observes_tool_args_and_final_result():
    bus = HookBus()

    adapter = AgentScopeHookAdapter(bus)
    agent = SimpleNamespace(state=SimpleNamespace(run_id="run-adapter"))
    seen = {}

    async def handler(**kwargs):
        seen.update(kwargs)
        yield ToolResponse(
            content=[TextBlock(type="text", text="ok")],
            state=ToolResultState.SUCCESS,
        )

    events = [
        item
        async for item in adapter.on_acting(
            agent,
            {
                "tool_call": ToolCallBlock(
                    id="tc-1", name="write", input=json.dumps({"value": 1})
                )
            },
            handler,
        )
    ]
    assert json.loads(seen["tool_call"].input) == {"value": 1}
    assert len(events) == 1 and events[0].metadata == {}
