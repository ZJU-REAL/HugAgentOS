import asyncio
from types import SimpleNamespace

from core.llm.context_usage import (
    build_compaction_context_usage,
    build_context_usage_snapshot,
)


def _agent():
    manifest = {
        "used_tokens": 80,
        "budget_details": {
            "context_window": 1_000,
            "tool_reserve_tokens": 20,
        },
        "included": [
            {"kind": "system_rule", "visibility": "model", "final_tokens": 30},
            {"kind": "assistant_history", "visibility": "model", "final_tokens": 25},
            {"kind": "tool_result", "visibility": "model", "final_tokens": 15},
            {"kind": "attachment", "visibility": "model", "final_tokens": 10},
            {"kind": "reference", "visibility": "manifest_only", "final_tokens": 999},
        ],
    }
    return SimpleNamespace(
        request_context_manifest=manifest,
        model=SimpleNamespace(context_size=1_000, model="demo-model"),
        state=SimpleNamespace(model_name="demo-model", model_provider_id="provider-1"),
    )


def test_provider_usage_is_the_exact_total_and_breakdown_is_reconciled():
    snapshot = build_context_usage_snapshot(
        _agent(),
        prompt_tokens=120,
        completion_tokens=30,
        model_call_index=2,
    )

    assert snapshot["source"] == "provider"
    assert snapshot["exact"] is True
    assert snapshot["prompt_tokens"] == 120
    assert snapshot["completion_tokens"] == 30
    assert snapshot["used_tokens"] == 150
    assert snapshot["context_window"] == 1_000
    assert snapshot["model_call_index"] == 2
    assert sum(snapshot["breakdown"].values()) == 150
    assert snapshot["breakdown"] == {
        "messages": 60,
        "tools": 18,
        "thinking": 0,
        "files": 12,
        "system": 60,
        "input": 0,
    }


def test_missing_provider_usage_keeps_tool_definitions_out_of_tool_calls():
    snapshot = build_context_usage_snapshot(
        _agent(),
        prompt_tokens=0,
        completion_tokens=0,
        model_call_index=1,
    )

    assert snapshot["source"] == "backend_estimate"
    assert snapshot["exact"] is False
    assert snapshot["prompt_tokens"] == 100
    assert snapshot["completion_tokens"] == 0
    assert snapshot["used_tokens"] == 100
    assert snapshot["breakdown"] == {
        "messages": 25,
        "tools": 15,
        "thinking": 0,
        "files": 10,
        "system": 50,
        "input": 0,
    }


def test_partial_provider_usage_keeps_missing_prompt_explicitly_estimated():
    snapshot = build_context_usage_snapshot(
        _agent(),
        prompt_tokens=0,
        completion_tokens=30,
        model_call_index=1,
    )

    assert snapshot["source"] == "backend_estimate"
    assert snapshot["exact"] is False
    assert snapshot["prompt_tokens"] == 100
    assert snapshot["completion_tokens"] == 30
    assert snapshot["used_tokens"] == 130
    assert sum(snapshot["breakdown"].values()) == 130


def test_compaction_snapshot_uses_post_compaction_backend_estimate():
    snapshot = build_compaction_context_usage(
        {
            "system_prompt_tokens": 40,
            "tool_schema_tokens": 20,
            "message_tokens": 15,
            "provider_overhead_tokens": 7,
            "total_estimated_tokens": 82,
        },
        context_window=1_000,
        model_name="demo-model",
        model_provider_id="provider-1",
    )

    assert snapshot["source"] == "compaction_estimate"
    assert snapshot["exact"] is False
    assert snapshot["used_tokens"] == 82
    assert sum(snapshot["breakdown"].values()) == 82
    assert snapshot["breakdown"]["system"] == 67
    assert snapshot["breakdown"]["tools"] == 0
    assert snapshot["breakdown"]["messages"] == 15


def test_model_call_end_emits_live_context_usage():
    from orchestration.streaming import StreamingAgent

    class ModelCallEndEvent:
        input_tokens = 120
        output_tokens = 30

    agent = _agent()
    agent.observe_context_tokens = lambda prompt, completion: None
    streaming = StreamingAgent(agent, mcp_clients=[])

    async def collect():
        return [item async for item in streaming._map_event(ModelCallEndEvent())]

    events = asyncio.run(collect())
    usage_events = [payload for kind, payload in events if kind == "context_usage"]
    assert len(usage_events) == 1
    assert usage_events[0]["source"] == "provider"
    assert usage_events[0]["used_tokens"] == 150
    assert streaming.get_usage()["context_tokens"] == 150


def test_auxiliary_call_can_restore_primary_context_snapshot():
    from orchestration.streaming import StreamingAgent

    class ModelCallEndEvent:
        input_tokens = 120
        output_tokens = 30

    agent = _agent()
    agent.observe_context_tokens = lambda prompt, completion: None
    streaming = StreamingAgent(agent, mcp_clients=[])

    async def collect():
        return [item async for item in streaming._map_event(ModelCallEndEvent())]

    asyncio.run(collect())
    primary = streaming.get_context_usage()
    streaming.restore_context_usage({**primary, "used_tokens": 999})
    assert streaming.get_context_usage()["used_tokens"] == 999

    streaming.restore_context_usage(primary)
    assert streaming.get_context_usage()["used_tokens"] == 150


def test_compaction_projection_is_hidden_after_a_newer_provider_snapshot():
    from datetime import datetime, timezone

    from core.services.compaction_service import get_compaction_context_state

    compacted = build_compaction_context_usage(
        {
            "system_prompt_tokens": 40,
            "tool_schema_tokens": 20,
            "message_tokens": 15,
            "provider_overhead_tokens": 7,
        },
        context_window=1_000,
    )
    checkpoint = SimpleNamespace(
        message_id="checkpoint-1",
        created_at=datetime.now(timezone.utc),
        extra_data={
            "replacement_history": [{"role": "system", "content": "summary"}],
            "replacement_manifest": {"replacement_context_usage": compacted},
        },
    )
    rows = [SimpleNamespace(chat_seq=10, extra_data={"context_usage": compacted})]
    repo = SimpleNamespace(
        list_recent_by_chat=lambda chat_id, limit: rows,
        count_visible_through_seq=lambda chat_id, seq: 10,
    )
    service = SimpleNamespace(
        get_latest_compaction_checkpoint=lambda chat_id: checkpoint,
        _checkpoint_covered_seq=lambda ckpt: 10,
        message_repo=repo,
    )

    assert get_compaction_context_state(service, "chat-1")["context_usage"] == compacted

    rows.append(SimpleNamespace(chat_seq=11, extra_data={"context_usage": compacted}))
    assert "context_usage" not in get_compaction_context_state(service, "chat-1")


def test_latest_persisted_snapshot_uses_newest_assistant_measurement():
    from core.llm.context_usage import latest_persisted_context_usage

    older = build_context_usage_snapshot(
        _agent(), prompt_tokens=100, completion_tokens=10, model_call_index=1
    )
    newer = build_context_usage_snapshot(
        _agent(), prompt_tokens=120, completion_tokens=30, model_call_index=2
    )
    rows = [
        SimpleNamespace(extra_data={"context_usage": older}),
        SimpleNamespace(extra_data={}),
        SimpleNamespace(extra_data={"context_usage": newer}),
        SimpleNamespace(
            extra_data={
                "context_usage": {
                    **newer,
                    "schema_version": "context-usage.v1",
                    "used_tokens": 999,
                }
            }
        ),
    ]
    service = SimpleNamespace(
        message_repo=SimpleNamespace(
            list_recent_by_chat=lambda chat_id, limit: rows,
        )
    )

    assert latest_persisted_context_usage(service, "chat-1") == newer
