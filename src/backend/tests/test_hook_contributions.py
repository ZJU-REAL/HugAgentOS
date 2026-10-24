"""Contributed hooks reach each assembled agent with isolated request state."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from core.harness.registry import register_hook_factory
from core.harness.registry import hook_factories
from core.harness.hooks import Decision, HookRejected, HookSpec, HookStage, Invocation
from core.llm.factory.request import AgentRequest
from core.llm.factory.runtime.middleware_stack import middleware_stack


@pytest.mark.asyncio
async def test_factory_hooks_reach_each_agent(monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setattr('core.chat.steer.take_pending_steer', AsyncMock(return_value=None))
    monkeypatch.setattr(hook_factories.registration_state(), 'factories', [])
    requests, called = [], []
    def factory(request):
        requests.append(request.run_id)
        def guard(invocation):
            called.append(request.run_id)
            return Decision.reject("policy denied")
        yield HookSpec("module-guard", HookStage.BEFORE_TOOL, guard)
    register_hook_factory(factory)
    register_hook_factory(factory)
    buses = []
    for run_id in ("first", "second"):
        toolkit = Mock(function_tools=[], permission_specs={})
        _, adapters, _, _ = await middleware_stack(
            AgentRequest(run_id=run_id), _api_scope=None, _eval_scope=None,
            _build_toolkit=lambda: toolkit, _interactive=True, _ontology_runtime={},
            _plan_tool_enabled=False, _proj_scope=None, _sbx_sess=None,
            _subagent_model_pinned=False, _tool_approval_available=False,
            default_model=SimpleNamespace(model="test"),
            profile=SimpleNamespace(intervention_rules=[]), tool_schemas=[], toolkit=toolkit)
        bus = adapters[0].bus
        # Isolate persistent event/usage recording; execute the real hook policy.
        from core.harness.events import EventSink
        bus.event_sink = EventSink()
        bus.usage_recorder = None
        with pytest.raises(HookRejected, match="policy denied"):
            await bus.enforce(Invocation.create(run_id=run_id, stage=HookStage.BEFORE_TOOL, operation_name="test"))
        buses.append(bus)
    assert requests == ["first", "second"]
    assert called == ["first", "second"]
    assert buses[0] is not buses[1]
