from types import SimpleNamespace

import pytest
from agentscope.message import TextBlock, UserMsg
from agentscope.model import ChatResponse, ChatUsage
from core.llm.runtime.manifest_agent import ManifestBoundAgent
from core.llm.tools.ontology_toolkit import OntologyFilteredToolkit
from core.chat.compaction.history import normalize_rows


class CaptureModel:
    from agentscope.formatter import OpenAIChatFormatter
    formatter = OpenAIChatFormatter()
    model = "budget-probe"
    context_size = 100

    def __init__(self):
        self.calls = []

    async def __call__(self, messages, tools=None, **kwargs):
        self.calls.append(messages)
        return ChatResponse(
            content=[TextBlock(type="text", text="ok")],
            is_last=True,
            usage=ChatUsage(input_tokens=1, output_tokens=1, time=0.01),
        )

    async def count_tokens(self, messages, tools=None):
        return 1


@pytest.mark.parametrize("steps", [None, []])
def test_display_records_cannot_enter_model_history(steps):
    row = SimpleNamespace(
        role="assistant",
        content="answer",
        model_steps=steps,
        thinking=[{"content": "DISPLAY-ONLY-THINKING"}],
        tool_calls=[{"tool_name": "unrecorded", "result": "DISPLAY-ONLY-RESULT"}],
        extra_data={
            "segments": [
                {"type": "compaction", "progress": {"status": "failed"}},
                {"type": "future-ui-widget"},
            ]
        },
    )
    assert normalize_rows([row]) == [
        {"role": "assistant", "content": [{"type": "text", "text": "answer"}]}
    ]


@pytest.mark.asyncio
async def test_over_budget_request_never_reaches_model():
    model = CaptureModel()
    agent = ManifestBoundAgent(
        name="probe",
        system_prompt="rules",
        model=model,
        toolkit=OntologyFilteredToolkit(),
    )
    with pytest.raises(Exception, match="context_budget_exceeded"):
        await agent.reply(UserMsg(name="user", content="x" * 10000))
    assert model.calls == []
    assert agent.request_context_manifest["over_budget"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "empty", "disabled"])
async def test_compaction_failure_cannot_replace_history_with_fake_summary(monkeypatch, failure):
    from dataclasses import replace

    from agentscope.agent import Agent
    from core.config import settings as settings_module
    from core.llm.context.compacting_agent import CompactingAgent
    from core.llm.context.safety import ContextBudgetExceeded
    from core.llm.runtime.state import AgentRuntimeState
    from core.chat.compaction import service as compaction_service

    engine_calls = []

    async def compact(*args, **kwargs):
        engine_calls.append(True)
        if failure == "timeout":
            raise TimeoutError("summary timeout")
        return None

    async def forbidden_framework(*args, **kwargs):
        raise AssertionError("A failed summary must never enter a second compressor")

    monkeypatch.setattr(compaction_port, "run_compaction", compact)
    monkeypatch.setattr(Agent, "compress_context", forbidden_framework)
    if failure == "disabled":
        monkeypatch.setattr(
            settings_module,
            "settings",
            replace(
                settings_module.settings,
                compaction=replace(settings_module.settings.compaction, enabled=False),
            ),
        )
    model = CaptureModel()
    agent = CompactingAgent(
        name="failure-probe",
        system_prompt="rules",
        model=model,
        toolkit=OntologyFilteredToolkit(),
        state=AgentRuntimeState(chat_id="failure-chat"),
    )
    with pytest.raises(ContextBudgetExceeded):
        await agent.reply(UserMsg(name="user", content="ORIGINAL-CONTEXT-" * 1000))
    assert model.calls == []
    assert agent.state.summary == ""
    assert any("ORIGINAL-CONTEXT-" in message.get_text_content() for message in agent.state.context)
    assert bool(engine_calls) is (failure != "disabled")


@pytest.mark.asyncio
async def test_budget_stop_also_blocks_another_compaction_attempt(monkeypatch):
    from core.llm.context.compacting_agent import CompactingAgent
    from core.llm.context.safety import ContextBudgetExceeded, request_admission_scope
    from core.chat.compaction import service as compaction_service

    calls = []

    async def compact(*args, **kwargs):
        calls.append(True)
        return None

    monkeypatch.setattr(compaction_port, "run_compaction", compact)
    model = CaptureModel()
    agent = CompactingAgent(
        name="stopped-compactor",
        system_prompt="rules",
        model=model,
        toolkit=OntologyFilteredToolkit(),
    )
    with request_admission_scope() as admission:
        admission.stopped = ContextBudgetExceeded(
            manifest={"used_tokens": 2, "total_budget": 1},
            context=[],
            summary="",
        )
        with pytest.raises(ContextBudgetExceeded):
            await agent.compress_context()
    assert calls == []
    assert model.calls == []

from core.harness.registry import compaction as compaction_port
