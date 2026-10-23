"""Configured endpoint windows must survive model construction and compaction."""

from types import SimpleNamespace

import core.chat.runtime.compaction as compaction
from core.models.config import ResolvedModelConfig


def test_model_builder_uses_selected_provider_window(monkeypatch):
    from core.models import chat_models

    calls = []
    monkeypatch.setattr(
        chat_models, "make_chat_model", lambda **kw: calls.append(kw) or SimpleNamespace()
    )
    cfg = ResolvedModelConfig(
        base_url="https://example.invalid",
        api_key="fake",
        model_name="shared-model",
        context_length=36000,
        provider_id="resolved-provider",
    )
    model = chat_models.build_model_for_mode(cfg, mode="fast")
    assert model.resolved_model_provider_id == "resolved-provider"
    assert calls[0]["context_size"] == 36000


def test_post_turn_trigger_uses_frozen_window_not_same_name_provider(monkeypatch):
    pass
    from core.models import context_window as context_manager
    from core.chat.compaction import service as compaction_service

    monkeypatch.setattr(context_manager, "resolve_model_context_window", lambda name: 128000)
    monkeypatch.setattr(compaction_service, "resolve_trigger_ratio", lambda: 0.82)
    assert compaction._should_schedule_compaction(
        model_name="shared-model",
        usage={"context_tokens": 40000},
        budget_inputs={"context_window": 36000},
    )


def test_background_compaction_receives_the_frozen_window(monkeypatch):
    import asyncio

    from core.chat.runtime.compaction import _spawn_compaction_task
    from core.models import context_window as context_manager
    from core.chat.compaction import service as compaction_service

    observed = []
    monkeypatch.setattr(context_manager, "resolve_model_context_window", lambda name: 128000)
    monkeypatch.setattr(compaction_service, "resolve_trigger_ratio", lambda: 0.82)

    async def compact(chat_id, **kwargs):
        observed.append(kwargs)
        return True

    monkeypatch.setattr(compaction_service, "run_post_turn_compaction", compact)

    async def run():
        _spawn_compaction_task(
            run_id="run",
            chat_id="audit",
            model_name="shared-model",
            usage={"context_tokens": 40000},
            budget_inputs={"context_window": 36000},
        )
        await asyncio.sleep(0)

    asyncio.run(run())
    assert observed[0]["token_limit"] == 29520
    assert observed[0]["budget_inputs"]["context_window"] == 36000


def test_default_model_identity_reaches_usage_and_frozen_budget():
    from core.agent_runtime.budget import _compaction_budget_inputs
    from core.llm.context.usage import build_context_usage_snapshot

    agent = SimpleNamespace(
        model=SimpleNamespace(
            model="shared-model", context_size=36000, resolved_model_provider_id="resolved-provider"
        ),
        state=SimpleNamespace(model_provider_id=""),
        request_context_manifest={},
    )
    usage = build_context_usage_snapshot(
        agent, prompt_tokens=40000, completion_tokens=2, model_call_index=1
    )
    inputs = _compaction_budget_inputs(agent, 36000)
    assert usage["model_provider_id"] == inputs["model_provider_id"] == "resolved-provider"


def test_failover_usage_uses_actual_endpoint_window_over_primary_manifest():
    from core.llm.context.usage import build_context_usage_snapshot

    agent = SimpleNamespace(
        model=SimpleNamespace(
            model="fallback", context_size=128000, resolved_model_provider_id="fallback-provider"
        ),
        state=SimpleNamespace(model_provider_id="primary-provider"),
        request_context_manifest={"budget_details": {"context_window": 36000}},
    )
    usage = build_context_usage_snapshot(
        agent, prompt_tokens=40000, completion_tokens=2, model_call_index=1
    )
    assert usage["context_window"] == 128000
    assert usage["model_provider_id"] == "fallback-provider"
