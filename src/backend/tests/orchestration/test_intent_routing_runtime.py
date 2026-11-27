"""智能路由的运行时：识别请求复用主请求前缀、模型选择、用量记账、prepare_main 接线。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from agentscope.message import SystemMsg, TextBlock, ToolCallBlock

from core.agent_runtime import budget, intent_routing, runner, targets
from core.agent_runtime.integrations import evolution
from core.agent_runtime.integrations import memory_identity as memory_identity_module
from core.agent_runtime.integrations import memory_session as memory_session_module
from core.capabilities.catalog import resolver as catalog_resolver
from core.chat import mode_route_decision as decision
from core.chat import mode_router
from core.llm import factory
from core.llm.citations import allocator as allocator_module
from core.llm.runtime import streaming as streaming_module
from core.llm.runtime import stream_loop

TOOLS = [{"type": "function", "function": {"name": "internet_search", "parameters": {}}}]
HISTORY = [
    {"role": "user", "content": "第一问"},
    {"role": "assistant", "content": "第一答"},
    {"role": "user", "content": "第二问：帮我写一份报告"},
]


class FakeAgent:
    """Mimics the parts of the manifest-bound agent the probe touches."""

    def __init__(self):
        self.state = SimpleNamespace(context=[], model_provider_id="prov-main", summary=None,
                                     apply_request_context=lambda ctx, text: None)
        self._jx_context_manifest_listener = "listener"
        self._jx_execution_manifest = "base-manifest"
        self.model = SimpleNamespace(context_size=128_000)

    async def _prepare_model_input(self):
        # Like ManifestBoundAgent: rewrites evidence attrs while preparing.
        self._jx_execution_manifest = "request-manifest"
        self._jx_context_assembly = "assembly"
        return {"messages": [SystemMsg(name="system", content="SYSTEM PROMPT"), *self.state.context],
                "tools": TOOLS}


def _dump(messages):
    return [(m.role, m.get_text_content()) for m in messages]


@pytest.mark.asyncio
async def test_probe_prefix_is_the_main_request_prefix():
    probe_agent = FakeAgent()
    turn = SimpleNamespace(agent=probe_agent, session_messages=list(HISTORY), context={},
                           user_message=HISTORY[-1]["content"])
    messages, tools = await intent_routing._request_input(turn, "INSTRUCTION")

    # What the main stream will send: same loader, same agent preparation.
    main_agent = FakeAgent()
    user_msg = await stream_loop.load_request_input(main_agent, list(HISTORY))
    main_agent.state.context.append(user_msg)
    main_input = await main_agent._prepare_model_input()

    assert _dump(messages[:-1]) == _dump(main_input["messages"])
    assert tools == main_input["tools"]
    assert messages[-1].role == "user" and messages[-1].get_text_content() == "INSTRUCTION"
    # The agent is left exactly as the main stream expects to find it.
    assert probe_agent.state.context == []
    assert probe_agent._jx_context_manifest_listener == "listener"
    assert probe_agent._jx_execution_manifest == "base-manifest"
    assert not hasattr(probe_agent, "_jx_context_assembly")


class FakeModel:
    def __init__(self, blocks, *, delay=0.0, usage=None):
        self.model = "fake-model"
        self.provider_id = "prov-x"
        self.blocks, self.delay, self.usage = blocks, delay, usage
        self.calls = []

    async def __call__(self, messages, tools, tool_choice=None):
        self.calls.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})

        async def gen():
            await asyncio.sleep(self.delay)
            yield SimpleNamespace(content=[], is_last=False, usage=None)
            yield SimpleNamespace(content=self.blocks, is_last=True, usage=self.usage)

        return gen()


@pytest.fixture
def usage_log(monkeypatch):
    from core.harness import usage

    recorded = []

    async def record(recorder, attempt, **kw):
        recorded.append(attempt)
        return attempt

    monkeypatch.setattr(usage, "record_usage_safely", record)
    return recorded


@pytest.mark.asyncio
async def test_call_returns_text_and_bills_the_intent_router_role(usage_log):
    model = FakeModel([TextBlock(text='{"mode_slug": "turbo"}')],
                      usage=SimpleNamespace(input_tokens=1200, output_tokens=9))
    turn = SimpleNamespace(context={"run_id": "run-1"})
    outcome = await intent_routing._call(turn, model, ["m"], TOOLS, timeout_s=2, started=0.0)
    assert outcome.error is None and outcome.text == '{"mode_slug": "turbo"}'
    assert model.calls[0]["tool_choice"] is None and model.calls[0]["tools"] == TOOLS
    (attempt,) = usage_log
    assert attempt.metadata == {"source": "intent_router", "role": "intent_router"}
    assert attempt.status == "success" and attempt.usage.prompt_tokens == 1200


@pytest.mark.asyncio
async def test_call_treats_a_tool_call_as_failure(usage_log):
    model = FakeModel([ToolCallBlock(id="t1", name="internet_search", input="{}")])
    outcome = await intent_routing._call(SimpleNamespace(context={}), model, [], TOOLS,
                                         timeout_s=2, started=0.0)
    assert outcome.error == "tool_call"


@pytest.mark.asyncio
async def test_call_times_out(usage_log):
    model = FakeModel([TextBlock(text="{}")], delay=1.0)
    outcome = await intent_routing._call(SimpleNamespace(context={"run_id": "r"}), model, [], [],
                                         timeout_s=0.05, started=0.0)
    assert outcome.error == "timeout"
    assert usage_log[0].status == "timeout"


@pytest.mark.asyncio
async def test_routing_model_follows_main_model_unless_role_assigned(monkeypatch):
    from core.llm.runtime import model_cache
    from core.models import config as config_module

    picked = []
    monkeypatch.setattr(model_cache, "get_provider_model",
                        lambda pid, mode: picked.append((pid, mode)) or f"model:{pid}")
    assigned = {}

    class Service:
        @staticmethod
        def get_instance():
            return Service()

        def resolve(self, role):
            assert role == "intent_router"  # never the summarizer
            return assigned.get(role)

    monkeypatch.setattr(config_module, "ModelConfigService", Service)
    turn = SimpleNamespace(agent=FakeAgent(), context={"model_provider_id": "ignored"})
    assert await intent_routing._routing_model(turn) == ("model:prov-main", False)
    assigned["intent_router"] = SimpleNamespace(provider_id="prov-small")
    assert await intent_routing._routing_model(turn) == ("model:prov-small", True)
    assert picked == [("prov-main", "fast"), ("prov-small", "fast")]


def test_fit_window_drops_oldest_history_for_a_small_model():
    from agentscope.message import AssistantMsg, UserMsg

    messages = [SystemMsg(name="system", content="S")]
    for i in range(30):
        messages.append(UserMsg(name="user", content=f"q{i} " + "字" * 400))
        messages.append(AssistantMsg(name="assistant", content=f"a{i} " + "字" * 400))
    messages += [UserMsg(name="user", content="当前问题"), UserMsg(name="user", content="INSTR")]
    small = SimpleNamespace(context_size=8_000)
    out = intent_routing._fit_window(messages, [], small, "INSTR")
    assert out[0].role == "system" and _dump(out[-2:]) == _dump(messages[-2:])
    assert len(out) < len(messages)
    assert out[1].get_text_content() != messages[1].get_text_content()  # oldest went first


# ── prepare_main 接线 ─────────────────────────────────────────────────────


@pytest.fixture
def runtime(monkeypatch):
    observed = SimpleNamespace(created=0, closed_clients=0, specs=[], persisted=[])

    async def empty(*args, **kwargs):
        return ""

    async def session(**kwargs):
        return memory_session_module.SessionMemory(
            chat_id="", scope_user_id="", workspace_id="default", memory_enabled=False
        )

    async def create(**kwargs):
        observed.created += 1
        return SimpleNamespace(state=SimpleNamespace(ontology_runtime=kwargs["ontology_runtime"]),
                               model=None), [f"client{observed.created}"]

    class Adapter:
        def __init__(self, agent, clients):
            self.agent = agent

        async def stream(self, messages, context, **kwargs):
            yield "text_delta", "Answer"

        async def shutdown(self):
            pass

        async def aget_usage(self):
            return {}

        def get_context_usage(self, usage):
            return {}

    def resolve_spec(context):
        observed.specs.append(context.get("routed_mode_slug"))
        return None

    async def close_clients(clients):
        observed.closed_clients += 1

    from core.llm.mcp import manager

    monkeypatch.setattr(manager, "close_clients", close_clients)
    monkeypatch.setattr(factory, "create_agent_executor", create)
    monkeypatch.setattr(streaming_module, "StreamingAgent", Adapter)
    monkeypatch.setattr(memory_session_module, "open_session_memory", session)
    monkeypatch.setattr(memory_session_module, "resolve_session_memory", empty)
    monkeypatch.setattr(memory_identity_module, "build_user_identity_block", empty)
    monkeypatch.setattr(allocator_module, "anchor_start_for_chat", lambda *_: 0)
    monkeypatch.setattr(targets, "_resolve_mode_spec", resolve_spec)
    monkeypatch.setattr(targets, "_runtime_visible_subagents", lambda *_: [])
    for name in ("enabled_skill_ids_from_context", "enabled_mcp_ids_from_context",
                 "enabled_kb_ids_from_context"):
        monkeypatch.setattr(catalog_resolver, name, lambda *_: [])
    monkeypatch.setattr(budget, "_resolve_agent_model_runtime", lambda *_: ("test", 32000))
    monkeypatch.setattr(budget, "_compaction_budget_inputs", lambda *_: {})
    monkeypatch.setattr(evolution, "_assemble_episode_background", lambda **_: None)
    monkeypatch.setattr(mode_router, "persist_route",
                        lambda run_id, owner, result, tool_id: observed.persisted.append(result))
    return observed


async def _run(context):
    events = []
    async for event in runner.astream_chat_workflow(
        session_messages=[{"role": "user", "content": "帮我写报告"}],
        user_message="帮我写报告", context=context,
    ):
        events.append(event)
    return events


def _plan_needing_model():
    plan = mode_router.RoutePlan(tool_id="intent_abc", question="帮我写报告", strategy="smart_keep")
    plan.candidates = [mode_router.Candidate("standard", "标准模式"),
                       mode_router.Candidate("turbo", "极速模式", restricted=True)]
    return plan


def _card_events(events):
    return [e for e in events if e.get("tool_name") == "intent_recognition"]


@pytest.mark.asyncio
async def test_switch_rebuilds_agent_and_cards_precede_model_output(runtime, monkeypatch):
    monkeypatch.setattr(mode_router, "plan_route", lambda db, ctx, q: _plan_needing_model())

    async def ask(turn, plan):
        assert turn.agent is not None  # probe runs on the assembled agent
        return decision.ModelOutcome(text='{"mode_slug": "turbo", "reason": "查资料"}', latency_ms=12)

    monkeypatch.setattr(intent_routing, "_ask_model", ask)
    context = {"user_id": "u1", "mode_slug": "auto", "run_id": "r1", "journal_owner": "w1"}
    events = await _run(context)

    call, result = _card_events(events)
    first_content = next(i for i, e in enumerate(events) if e["type"] == "content")
    assert events.index(call) < events.index(result) < first_content
    assert call["tool_id"] == result["tool_id"] == "intent_abc"
    assert result["result"]["mode_slug"] == "turbo" and result["result"]["source"] == "llm"
    assert runtime.specs == ["standard", "turbo"]  # provisional fallback, then the routed mode
    assert runtime.created == 2 and runtime.closed_clients == 1
    assert context["routed_mode_slug"] == "turbo"
    assert context["intent_route"]["tool_id"] == "intent_abc"
    assert runtime.persisted and runtime.persisted[0]["mode_slug"] == "turbo"


@pytest.mark.asyncio
async def test_keeping_the_mode_reuses_the_agent(runtime, monkeypatch):
    plan = _plan_needing_model()
    plan.current_slug = "turbo"
    monkeypatch.setattr(mode_router, "plan_route", lambda db, ctx, q: plan)

    async def ask(turn, plan):
        return decision.ModelOutcome(text='{"mode_slug": "turbo"}')

    monkeypatch.setattr(intent_routing, "_ask_model", ask)
    events = await _run({"user_id": "u1", "mode_slug": "auto"})
    assert runtime.created == 1 and runtime.closed_clients == 0
    assert runtime.specs == ["turbo"]
    assert _card_events(events)[1]["result"]["source"] == "kept"


@pytest.mark.asyncio
async def test_resume_reuses_the_stored_route_without_recognition(runtime, monkeypatch):
    def no_plan(*_):
        raise AssertionError("must not re-plan")

    async def no_model(*_):
        raise AssertionError("must not re-recognize")

    monkeypatch.setattr(mode_router, "plan_route", no_plan)
    monkeypatch.setattr(intent_routing, "_ask_model", no_model)
    stored = {"mode_slug": "turbo", "source": "llm", "tool_id": "intent_old"}
    context = {"user_id": "u1", "mode_slug": "auto", "routed_mode_slug": "turbo",
               "intent_route": stored}
    events = await _run(context)
    call, result = _card_events(events)
    assert call["tool_id"] == result["tool_id"] == "intent_old"
    assert result["result"] == {"mode_slug": "turbo", "source": "llm"}
    assert runtime.specs == ["turbo"] and runtime.created == 1


@pytest.mark.asyncio
async def test_planning_failure_still_answers_in_fallback(runtime, monkeypatch):
    def broken(*_):
        raise RuntimeError("db down")

    monkeypatch.setattr(mode_router, "plan_route", broken)
    events = await _run({"user_id": "u1", "mode_slug": "auto"})
    result = _card_events(events)[1]["result"]
    assert result["source"] == "fallback" and result["fallback_cause"] == "router_error"
    assert any(e["type"] == "content" for e in events)


@pytest.mark.asyncio
async def test_non_auto_and_plan_chat_turns_do_not_route(runtime, monkeypatch):
    monkeypatch.setattr(mode_router, "plan_route", lambda *_: pytest.fail("routed"))
    assert not _card_events(await _run({"user_id": "u1", "mode_slug": "standard"}))
    assert not _card_events(await _run({"user_id": "u1", "mode_slug": "auto", "plan_chat": True}))
