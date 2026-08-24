"""Durable physical-attempt and append-only event coverage."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from core.db.engine import Base
from core.harness.events import Event, EventSink
from core.harness.hooks import HookBus
from core.harness.usage import AttemptUsage, MemoryUsageRecorder, UsageAttempt
from core.llm.agentscope_hook_adapter import AgentScopeHookAdapter
from core.llm.model_usage import (
    instrument_model_usage,
    model_usage_scope,
    record_provider_failure,
)
from core.services.harness_ledger import DurableEventStore, HarnessUsageLedger
from core.services.run_journal import RunJournal
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def ledger_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'harness-ledger.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    RunJournal(sessions).accept(
        run_id="run-ledger",
        message_id="msg-ledger",
        chat_id="chat-ledger",
        user_id="user-ledger",
        request_payload={"kind": "chat"},
        recovery_snapshot={"kind": "chat", "worker_args": {}},
    )
    yield sessions
    engine.dispose()


def test_attempt_rows_are_append_only_and_aggregate_is_derived(ledger_env):
    ledger = HarnessUsageLedger(ledger_env)
    first = ledger.record_attempt(
        UsageAttempt(
            run_id="run-ledger",
            kind="model",
            operation_name="provider-model",
            provider="provider",
            model="provider-model",
            status="failed",
            latency_ms=20,
            usage=AttemptUsage(prompt_tokens=5),
        )
    )
    second = ledger.record_attempt(
        UsageAttempt(
            run_id="run-ledger",
            kind="model",
            operation_name="provider-model",
            provider="provider",
            model="provider-model",
            status="success",
            latency_ms=30,
            retry_of=first.attempt_seq,
            usage=AttemptUsage(
                prompt_tokens=10,
                completion_tokens=4,
                cache_read_tokens=3,
                cache_write_tokens=2,
            ),
        )
    )
    ledger.record_attempt(
        UsageAttempt(
            run_id="run-ledger",
            kind="tool",
            operation_name="write",
            effect_id="eff-1",
            status="success",
            latency_ms=9,
        )
    )

    assert first.attempt_seq == 1 and first.retry_of is None
    assert second.attempt_seq == 2 and second.retry_of == 1
    rows = ledger.attempts("run-ledger")
    assert [row.attempt_seq for row in rows] == [1, 2, 3]
    assert rows[2].effect_id == "eff-1"
    assert ledger.aggregate("run-ledger") == {
        "attempt_count": 3,
        "failed_attempts": 1,
        "by_kind": {"model": 2, "tool": 1, "hook": 0},
        "prompt_tokens": 15,
        "completion_tokens": 4,
        "cache_read_tokens": 3,
        "cache_write_tokens": 2,
        "total_tokens": 19,
        "latency_ms": 59,
    }


@pytest.mark.asyncio
async def test_model_retry_wrapper_records_each_physical_call(ledger_env):
    ledger = HarnessUsageLedger(ledger_env)

    class FakeModel:
        model = "retry-model"
        max_retries = 1

        def __init__(self):
            self.calls = 0

        async def _call_api(self, model_name, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("provider unavailable")
            return SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=21,
                    output_tokens=8,
                    cache_input_tokens=5,
                    cache_creation_input_tokens=2,
                )
            )

        async def __call__(self):
            last = None
            for _ in range(self.max_retries + 1):
                try:
                    return await self._call_api(self.model)
                except Exception as exc:  # noqa: BLE001
                    last = exc
            raise last

    model = FakeModel()
    instrument_model_usage(model, provider="fake-provider")
    with model_usage_scope("run-ledger", ledger):
        await model()

    rows = [row for row in ledger.attempts("run-ledger") if row.kind == "model"]
    assert [row.status for row in rows] == ["failed", "success"]
    assert rows[1].retry_of == rows[0].attempt_seq
    assert rows[1].provider == "fake-provider"
    assert rows[1].usage == AttemptUsage(21, 8, 5, 2)
    assert ledger.aggregate("run-ledger")["total_tokens"] == 29

    RunJournal(ledger_env).accept(
        run_id="run-ledger-2",
        message_id="msg-ledger-2",
        chat_id="chat-ledger",
        user_id="user-ledger",
        request_payload={"kind": "chat"},
        recovery_snapshot={"kind": "chat", "worker_args": {}},
    )
    # Re-instrumenting a cached model must not stack wrappers or leak the
    # previous run binding/retry chain into the next request.
    instrument_model_usage(model, provider="fake-provider")
    with model_usage_scope("run-ledger-2", ledger):
        await model()
    assert len(ledger.attempts("run-ledger")) == 2
    second_run = ledger.attempts("run-ledger-2")
    assert len(second_run) == 1
    assert second_run[0].retry_of is None

    RunJournal(ledger_env).accept(
        run_id="run-ledger-3",
        message_id="msg-ledger-3",
        chat_id="chat-ledger",
        user_id="user-ledger",
        request_payload={"kind": "chat"},
        recovery_snapshot={"kind": "chat", "worker_args": {}},
    )

    class StructuredModel:
        model = "structured-model"
        max_retries = 0

        async def _call_api(self, model_name, **kwargs):
            return SimpleNamespace(
                usage=SimpleNamespace(input_tokens=7, output_tokens=3)
            )

        async def _call_api_with_structured_output(self, model_name, **kwargs):
            # Matches AgentScope's default implementation: the structured
            # helper delegates to the physical _call_api method.
            return await self._call_api(model_name, **kwargs)

    structured = instrument_model_usage(StructuredModel())
    with model_usage_scope("run-ledger-3", ledger):
        await structured._call_api_with_structured_output(structured.model)
    structured_rows = ledger.attempts("run-ledger-3")
    assert len(structured_rows) == 1
    assert structured_rows[0].usage == AttemptUsage(7, 3)


@pytest.mark.asyncio
async def test_durable_event_store_allocates_sequence_before_observers(ledger_env):
    sink = EventSink(DurableEventStore(ledger_env))
    seen = []
    sink.subscribe(lambda event: seen.append((event.event_seq, event.payload["value"])))
    one = await sink.append(
        Event.create(
            run_id="run-ledger",
            event_type="one",
            phase="before_run",
            payload={"value": 1},
        )
    )
    two = await sink.append(
        Event.create(
            run_id="run-ledger",
            event_type="two",
            phase="before_model",
            payload={"value": 2},
        )
    )
    assert (one.event_seq, two.event_seq) == (1, 2)
    assert seen == [(1, 1), (2, 2)]


@pytest.mark.asyncio
async def test_compaction_http_retry_is_also_physical_model_usage(
    ledger_env, monkeypatch
):
    import core.services.compaction_service as compaction

    ledger = HarnessUsageLedger(ledger_env)
    monkeypatch.setattr(
        compaction,
        "_resolve_summarizer_model",
        lambda: ("http://fake", "key", "summary-model", "summary-provider"),
    )
    monkeypatch.setattr(compaction, "_load_base_system_prompt", lambda: "")

    class Response:
        def __init__(self, status_code, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    responses = [
        Response(400, text="maximum context length exceeded"),
        Response(
            200,
            {
                "choices": [{"message": {"content": "摘要"}}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 6},
            },
        ),
    ]

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return responses.pop(0)

    monkeypatch.setattr(compaction.httpx, "AsyncClient", Client)
    history = [{"role": "user", "content": f"old-{index}"} for index in range(10)]
    with model_usage_scope("run-ledger", ledger):
        assert await compaction._summarize(history, timeout=1) == "摘要"

    attempts = ledger.attempts("run-ledger")
    assert [row.status for row in attempts] == ["failed", "success"]
    assert attempts[1].retry_of == attempts[0].attempt_seq
    assert {row.provider for row in attempts} == {"summary-provider"}
    assert attempts[1].usage == AttemptUsage(prompt_tokens=30, completion_tokens=6)


@pytest.mark.asyncio
async def test_agent_level_retry_keeps_chain_across_model_middleware_calls(ledger_env):
    ledger = HarnessUsageLedger(ledger_env)

    class Model:
        model = "agent-retry-model"
        max_retries = 0

        def __init__(self):
            self.calls = 0

        async def _call_api(self, model_name, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("first physical call failed")
            return SimpleNamespace(
                usage=SimpleNamespace(input_tokens=9, output_tokens=2)
            )

    model = instrument_model_usage(Model())
    adapter = AgentScopeHookAdapter(HookBus(usage_recorder=ledger))
    agent = SimpleNamespace(
        state=SimpleNamespace(run_id="run-ledger"),
        model=model,
    )

    async def model_handler(**kwargs):
        return await kwargs["current_model"]._call_api(model.model)

    async def reasoning_handler(**kwargs):
        for _ in range(2):
            try:
                await adapter.on_model_call(
                    agent,
                    {
                        "current_model": model,
                        "messages": [],
                        "tools": [],
                        "tool_choice": None,
                    },
                    model_handler,
                )
                break
            except RuntimeError:
                continue
        if False:
            yield None

    assert [
        item async for item in adapter.on_reasoning(agent, {}, reasoning_handler)
    ] == []
    rows = ledger.attempts("run-ledger")
    assert [row.status for row in rows] == ["failed", "success"]
    assert rows[1].retry_of == rows[0].attempt_seq


@pytest.mark.asyncio
async def test_model_internal_provider_fallback_records_both_requests(ledger_env):
    ledger = HarnessUsageLedger(ledger_env)

    class Model:
        model = "multimodal-fallback-model"
        max_retries = 0

        async def _call_api(self, model_name, **kwargs):
            started = __import__("time").monotonic()
            try:
                raise RuntimeError("provider rejected image")
            except RuntimeError as exc:
                await record_provider_failure(
                    self,
                    model_name,
                    exc,
                    started=started,
                    provider="fake-provider",
                )
            return SimpleNamespace(
                usage=SimpleNamespace(input_tokens=13, output_tokens=4)
            )

    model = instrument_model_usage(Model(), provider="fake-provider")
    with model_usage_scope("run-ledger", ledger):
        await model._call_api(model.model)
    rows = ledger.attempts("run-ledger")
    assert [row.status for row in rows] == ["failed", "success"]
    assert rows[1].retry_of == rows[0].attempt_seq


@pytest.mark.asyncio
async def test_followup_http_call_is_included_in_run_usage(ledger_env, monkeypatch):
    from orchestration import followups

    monkeypatch.setattr(
        followups,
        "_resolve_followup_config",
        lambda: ("http://fake", "key", "followup-model", "followup-provider"),
    )

    class Response:
        status_code = 200
        text = ""

        def json(self):
            return {
                "choices": [{"message": {"content": '["下一步是什么？"]'}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 3},
            }

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(followups.httpx, "AsyncClient", Client)
    generator = followups.FollowUpGenerator()
    generator.enabled = True
    questions = await generator.generate(
        "请解释实现",
        "这是一段足够长的回答，用来确保追问生成器不会因为回答太短而跳过真实模型调用。"
        * 2,
        run_id="run-ledger",
        usage_recorder=HarnessUsageLedger(ledger_env),
    )
    assert questions == ["下一步是什么？"]
    rows = HarnessUsageLedger(ledger_env).attempts("run-ledger")
    assert len(rows) == 1
    assert rows[0].metadata["source"] == "followup"
    assert rows[0].provider == "followup-provider"
    assert rows[0].usage == AttemptUsage(8, 3)


@pytest.mark.asyncio
async def test_vision_fallback_records_each_physical_model_attempt(monkeypatch):
    from core.services.model_config import ResolvedModelConfig
    from core.vision.provider import VisionCallResult, VisionProvider

    provider = VisionProvider(
        ResolvedModelConfig(
            base_url="http://vision.invalid/v1",
            api_key="key",
            model_name="vision-model",
            max_tokens=100,
            timeout=5,
            provider="openai_compatible",
        )
    )
    calls = 0

    async def fake_call(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("first mode timed out")
        return VisionCallResult(
            text="{}",
            mode="json_object",
            usage={
                "prompt_tokens": 13,
                "completion_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 4},
            },
        )

    monkeypatch.setattr(provider, "_call", fake_call)
    recorder = MemoryUsageRecorder()
    with model_usage_scope("run-vision", recorder):
        result = await provider.describe(b"image", "image/png", "describe")

    assert result.ok
    rows = recorder.attempts("run-vision")
    assert [row.status for row in rows] == ["timeout", "success"]
    assert rows[1].retry_of == rows[0].attempt_seq
    assert rows[1].provider == "openai_compatible"
    assert rows[1].model == "vision-model"
    assert rows[1].usage == AttemptUsage(13, 2, 4, 0)


@pytest.mark.asyncio
async def test_stream_usage_aggregates_ledger_but_keeps_primary_context(monkeypatch):
    import core.services.harness_ledger as ledger_module
    from orchestration.streaming import StreamingAgent

    rows = (
        UsageAttempt(
            run_id="run-stream",
            kind="model",
            operation_name="vision-model",
            provider="vision",
            model="vision-model",
            status="success",
            latency_ms=1,
            usage=AttemptUsage(4, 1),
            metadata={"source": "vision"},
            attempt_seq=1,
        ),
        UsageAttempt(
            run_id="run-stream",
            kind="model",
            operation_name="main-model",
            provider="main",
            model="main-model",
            status="success",
            latency_ms=1,
            usage=AttemptUsage(20, 5),
            attempt_seq=2,
        ),
        UsageAttempt(
            run_id="run-stream",
            kind="model",
            operation_name="followup-model",
            provider="followup",
            model="followup-model",
            status="success",
            latency_ms=1,
            usage=AttemptUsage(3, 2),
            metadata={"source": "followup"},
            attempt_seq=3,
        ),
    )

    class Ledger:
        def attempts(self, run_id):
            assert run_id == "run-stream"
            return rows

    monkeypatch.setattr(ledger_module, "HarnessUsageLedger", Ledger)
    streaming = StreamingAgent(
        SimpleNamespace(state=SimpleNamespace(run_id="run-stream")),
        mcp_clients=[],
    )
    usage = await streaming.aget_usage()
    assert usage["total_tokens"] == 35
    assert usage["llm_call_count"] == 3
    assert usage["context_tokens"] == 25
