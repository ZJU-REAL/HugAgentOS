"""The manifest and AgentScope request must consume one frozen surface."""

from __future__ import annotations

import pytest
from agentscope.message import TextBlock, UserMsg
from agentscope.model import ChatResponse, ChatUsage
from agentscope.skill import Skill, SkillLoaderBase
from agentscope.tool import FunctionTool, ToolChunk

from core.llm.context_adapter import (
    AgentScopeContextAdapter,
    PROVIDER_CONTEXT_META_KEY,
    next_request_sequence,
    render_context_item,
)
from core.llm.compaction import build_compacted_history
from core.llm.context_ir import (
    KIND_USER_INPUT,
    POLICY_NEVER,
    make_text_context_item,
)
from core.llm.execution_manifest import PromptManifestBuilder, stable_hash
from core.llm.manifest_agent import ManifestBoundAgent
from core.llm.message_compat import session_to_msgs
from core.ontology.toolkit import OntologyFilteredToolkit


class CountingSkillLoader(SkillLoaderBase):
    def __init__(self, *, moving: bool = False) -> None:
        self.calls = 0
        self.moving = moving

    async def list_skills(self):  # noqa: ANN201
        self.calls += 1
        suffix = str(self.calls) if self.moving else "stable"
        return [
            Skill(
                f"skill-{suffix}",
                f"Skill surface {suffix}",
                f"/skills/{suffix}",
                f"instructions-{suffix}",
                float(self.calls),
            )
        ]


class CaptureModel:
    model = "capture-model"
    context_size = 32_768

    def __init__(self) -> None:
        self.calls = []

    async def __call__(self, messages, tools=None, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append({"messages": messages, "tools": tools, "kwargs": kwargs})
        return ChatResponse(
            content=[TextBlock(type="text", text="done")],
            is_last=True,
            usage=ChatUsage(input_tokens=1, output_tokens=1, time=0.01),
        )

    async def count_tokens(self, messages, tools=None):  # noqa: ANN001, ANN201
        return 1


class ProviderRetryCaptureModel(CaptureModel):
    def set_context_rewrite_listener(self, listener) -> None:  # noqa: ANN001
        self.rewrite_listener = listener

    async def __call__(self, messages, tools=None, **kwargs):  # noqa: ANN001, ANN201
        retry = [
            {"role": "system", "content": "rules"},
            {
                "role": "system",
                "content": "PROJECT-MATERIAL-MARKER",
                PROVIDER_CONTEXT_META_KEY: {
                    "kind": "project_material",
                    "origin": "workspace:project-1",
                    "trust": "workspace",
                    "priority": 900,
                },
            },
            {
                "role": "user",
                "content": "remembered fact",
                PROVIDER_CONTEXT_META_KEY: {
                    "kind": "memory",
                    "origin": "memory:frozen",
                    "trust": "memory",
                    "priority": 700,
                },
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                    for call_id in ("parallel-1", "parallel-2")
                ],
            },
            {"role": "tool", "tool_call_id": "parallel-1", "content": "first"},
            {"role": "tool", "tool_call_id": "parallel-2", "content": "second"},
            {"role": "user", "content": "hello"},
            {
                "role": "user",
                "name": "system-reminder",
                "content": [{"type": "text", "text": "media unavailable"}],
                PROVIDER_CONTEXT_META_KEY: {
                    "kind": "reminder",
                    "origin": "harness:multimodal_fallback",
                    "trust": "system",
                    "priority": 800,
                },
            },
        ]
        wire = await self.rewrite_listener(retry)
        self.calls.append({"messages": messages, "tools": tools, "wire": wire, "kwargs": kwargs})
        return ChatResponse(
            content=[TextBlock(type="text", text="done")],
            is_last=True,
            usage=ChatUsage(input_tokens=1, output_tokens=1, time=0.01),
        )


@pytest.mark.asyncio
async def test_frozen_surface_is_enumerated_once_and_replayed_via_public_reply():
    loader = CountingSkillLoader()
    toolkit = OntologyFilteredToolkit(skills_or_loaders=[loader])

    snapshot = await toolkit.freeze_execution_surface()
    manifest_schemas = snapshot.tool_schemas
    manifest_skills = snapshot.skill_instructions
    model = CaptureModel()
    agent = ManifestBoundAgent(
        name="snapshot-test",
        system_prompt="base-system-prompt",
        model=model,
        toolkit=toolkit,
    )
    reply = await agent.reply(UserMsg(name="user", content="hello"))
    model_input = model.calls[0]

    assert loader.calls == 1
    assert reply.get_text_content() == "done"
    assert model_input["tools"] == manifest_schemas
    assert model_input["messages"][0].get_text_content() == "base-system-prompt\n" + manifest_skills
    assert model_input["tools"][0]["function"]["name"] == toolkit.builtin_skill_viewer.tool.name


@pytest.mark.asyncio
async def test_public_reply_uses_injected_context_adapter_factory():
    created = []

    class RecordingAdapter(AgentScopeContextAdapter):
        def items_from_messages(self, *args, **kwargs):  # noqa: ANN002,ANN003
            created.append("items")
            return super().items_from_messages(*args, **kwargs)

    agent = ManifestBoundAgent(
        name="adapter-injection-test",
        system_prompt="rules",
        model=CaptureModel(),
        toolkit=OntologyFilteredToolkit(),
        context_adapter_factory=RecordingAdapter,
    )

    await agent.reply(UserMsg(name="user", content="hello"))

    assert agent.context_adapter_factory is RecordingAdapter
    assert created == ["items"]


@pytest.mark.asyncio
async def test_invalidated_surface_creates_explicit_next_generation():
    loader = CountingSkillLoader(moving=True)
    toolkit = OntologyFilteredToolkit(skills_or_loaders=[loader])
    seen = []
    toolkit.set_execution_surface_listener(lambda snapshot: seen.append(snapshot.generation))

    first = await toolkit.freeze_execution_surface()
    toolkit.invalidate_execution_surface()
    second = await toolkit.freeze_execution_surface()

    assert first.generation == 1
    assert second.generation == 2
    assert "skill-1" in first.skill_instructions
    assert "skill-2" in second.skill_instructions
    assert loader.calls == 2
    assert seen == [1, 2]


@pytest.mark.asyncio
async def test_public_reply_assembles_context_and_binds_request_manifest():
    toolkit = OntologyFilteredToolkit()
    model = CaptureModel()
    builder = PromptManifestBuilder(context={"workspace_id": "ws-1"})
    builder.add_prompt_section(
        "system/base",
        "base-system-prompt",
        origin="prompt:test",
        trust="platform",
        priority=1000,
        cache_class="stable",
    )
    base_manifest = builder.build(final_prompt="base-system-prompt")
    seen = []

    async def bind_request(manifest):  # noqa: ANN001, ANN202
        seen.append(manifest)

    agent = ManifestBoundAgent(
        name="context-test",
        system_prompt="base-system-prompt",
        model=model,
        toolkit=toolkit,
    )
    agent.bind_execution_surface(base_manifest)
    agent.set_context_manifest_listener(bind_request)

    await agent.reply(UserMsg(name="user", content="hello context"))

    request = seen[0]
    context_manifest = request.to_dict()["context_manifest"]
    assert request.context_manifest_hash == stable_hash(context_manifest)
    assert request.aggregate_hash == agent.execution_manifest.aggregate_hash
    assert context_manifest["included"][-1]["kind"] == "user_input"
    assert context_manifest["included"][-1]["origin"] == "user:chat"
    assert context_manifest["budget_details"]["context_window"] == model.context_size
    assert context_manifest["budget_details"]["message_budget"] < model.context_size
    assert model.calls[0]["messages"][-1].get_text_content() == "hello context"
    assert (
        model.calls[0]["messages"][-1].metadata["harness_context_items"][0]["content_hash"]
        == context_manifest["included"][-1]["content_hash"]
    )


@pytest.mark.asyncio
async def test_public_provider_retry_rebinds_manifest_to_exact_second_wire_request():
    model = ProviderRetryCaptureModel()
    builder = PromptManifestBuilder(context={"workspace_id": "ws-1"})
    builder.add_prompt_section(
        "system/base",
        "rules",
        origin="prompt:test",
        trust="platform",
        priority=1_000,
        cache_class="stable",
    )
    builder.add_prompt_section(
        "runtime/project",
        "PROJECT-MATERIAL-MARKER",
        origin="workspace:project-1",
        trust="workspace",
        priority=900,
        cache_class="workspace",
        reference="project:project-1",
        sensitive=True,
    )
    agent = ManifestBoundAgent(
        name="provider-retry-test",
        system_prompt="rules",
        model=model,
        toolkit=OntologyFilteredToolkit(),
    )
    agent.bind_execution_surface(builder.build(final_prompt="rules"))

    await agent.reply(UserMsg(name="user", content="hello"))

    wire = model.calls[0]["wire"]
    assert all(PROVIDER_CONTEXT_META_KEY not in message for message in wire)
    assert str(wire).count("PROJECT-MATERIAL-MARKER") == 1
    manifest = agent.request_context_manifest
    retry_entries = [
        entry for entry in manifest["included"] if entry["item_id"].startswith("provider-retry:")
    ]
    assert len({entry["message_group"] for entry in retry_entries}) == len(wire)
    reminder = next(entry for entry in retry_entries if entry["kind"] == "reminder")
    project = next(entry for entry in retry_entries if entry["kind"] == "project_material")
    memory = next(entry for entry in retry_entries if entry["kind"] == "memory")
    assert reminder["origin"] == "harness:multimodal_fallback"
    assert reminder["trust"] == "system"
    assert project["origin"] == "workspace:project-1"
    assert project["trust"] == "workspace"
    assert memory["origin"] == "memory:frozen"
    assert memory["trust"] == "memory"
    tool_entries = [
        entry for entry in retry_entries if entry["kind"] in {"tool_call", "tool_result"}
    ]
    assert [entry["kind"] for entry in tool_entries].count("tool_call") == 2
    assert [entry["kind"] for entry in tool_entries].count("tool_result") == 2
    assert {entry["pair_id"] for entry in tool_entries} == {"provider-tool-batch:3"}
    assert reminder["content_hash"] == stable_hash(wire[-1])
    assert agent.execution_manifest.context_manifest_hash == stable_hash(manifest)


@pytest.mark.asyncio
async def test_public_reply_keeps_multiturn_history_before_current_user():
    model = CaptureModel()
    agent = ManifestBoundAgent(
        name="sequence-test",
        system_prompt="rules",
        model=model,
        toolkit=OntologyFilteredToolkit(),
    )
    history = [
        {"role": "user", "content": "question one"},
        {"role": "assistant", "content": "answer one"},
        {"role": "user", "content": "question two"},
        {"role": "assistant", "content": "answer two"},
    ]
    agent.state.context.extend(session_to_msgs(history))
    request_seq = next_request_sequence(agent.state.context)
    current = render_context_item(
        make_text_context_item(
            "current question",
            item_id=f"request:{request_seq}",
            kind=KIND_USER_INPUT,
            origin="user:chat",
            trust="user",
            created_seq=request_seq,
            priority=1_000,
            token_budget=100_000,
            truncation_policy=POLICY_NEVER,
        )
    )

    await agent.reply(current)

    texts = [message.get_text_content() for message in model.calls[0]["messages"]]
    assert texts[-5:] == [
        "question one",
        "answer one",
        "question two",
        "answer two",
        "current question",
    ]


@pytest.mark.asyncio
async def test_public_reply_keeps_compaction_summary_after_retained_history():
    model = CaptureModel()
    agent = ManifestBoundAgent(
        name="compaction-sequence-test",
        system_prompt="rules",
        model=model,
        toolkit=OntologyFilteredToolkit(),
    )
    compacted = build_compacted_history(
        ["retained one", "retained two"],
        "handoff summary",
    )
    agent.state.context.extend(session_to_msgs(compacted))
    request_seq = next_request_sequence(agent.state.context)
    current = render_context_item(
        make_text_context_item(
            "current question",
            item_id=f"request:{request_seq}",
            kind=KIND_USER_INPUT,
            origin="user:chat",
            trust="user",
            created_seq=request_seq,
            priority=1_000,
            token_budget=100_000,
            truncation_policy=POLICY_NEVER,
        )
    )

    await agent.reply(current)

    texts = [message.get_text_content() for message in model.calls[0]["messages"]]
    assert texts[-4:] == [
        "retained one",
        "retained two",
        "handoff summary",
        "current question",
    ]


@pytest.mark.asyncio
async def test_public_reply_manifest_accounts_for_every_over_budget_history_candidate():
    model = CaptureModel()
    model.context_size = 1_400
    agent = ManifestBoundAgent(
        name="complete-candidate-test",
        system_prompt="rules",
        model=model,
        toolkit=OntologyFilteredToolkit(),
    )
    history = [
        {"role": "user", "content": "A" * 4_000},
        {"role": "assistant", "content": "B" * 4_000},
        {"role": "user", "content": "C" * 4_000},
        {"role": "assistant", "content": "D" * 4_000},
    ]
    agent.state.context.extend(session_to_msgs(history))
    request_seq = next_request_sequence(agent.state.context)
    current = render_context_item(
        make_text_context_item(
            "current",
            item_id=f"request:{request_seq}",
            kind=KIND_USER_INPUT,
            origin="user:chat",
            trust="user",
            created_seq=request_seq,
            priority=1_000,
            token_budget=100_000,
            truncation_policy=POLICY_NEVER,
        )
    )

    await agent.reply(current)

    manifest = agent.request_context_manifest
    decisions = [*manifest["included"], *manifest["excluded"]]
    history_ids = {
        entry["item_id"] for entry in decisions if entry["item_id"].startswith("session:")
    }
    assert history_ids == {
        "session:0:block:0",
        "session:1:block:0",
        "session:2:block:0",
        "session:3:block:0",
    }
    assert manifest["excluded"]


@pytest.mark.asyncio
async def test_public_reply_does_not_invent_message_budget_when_window_is_consumed():
    model = CaptureModel()
    model.context_size = 5

    def oversized_tool() -> ToolChunk:
        """A deliberately oversized schema reserve."""
        return ToolChunk(content=[])

    toolkit = OntologyFilteredToolkit(
        tools=[FunctionTool(oversized_tool, description="schema" * 200)]
    )
    agent = ManifestBoundAgent(
        name="zero-budget-test",
        system_prompt="rules",
        model=model,
        toolkit=toolkit,
    )

    await agent.reply(UserMsg(name="user", content="current"))

    assert agent.request_context_manifest["total_budget"] == 0
    assert agent.request_context_manifest["budget_details"]["message_budget"] == 0


@pytest.mark.asyncio
async def test_public_multimodal_request_keeps_or_drops_each_image_whole(monkeypatch):
    from core.llm import middlewares as mw

    monkeypatch.setattr(
        mw,
        "_build_file_context",
        lambda files, user_id=None: "TEXT-FILE-EVIDENCE",
    )
    monkeypatch.setattr(mw, "_effective_model_supports_vision", lambda state: True)
    monkeypatch.setattr(
        mw,
        "_fetch_image_base64",
        lambda file, user_id=None: ("A" * 4_000, "image/png"),
    )
    model = CaptureModel()
    model.context_size = 1_400
    agent = ManifestBoundAgent(
        name="image-budget-test",
        system_prompt="rules",
        model=model,
        toolkit=OntologyFilteredToolkit(),
        middlewares=[mw.FileContextMiddleware()],
        state=mw.AgentRuntimeState(
            uploaded_files=[
                {"file_id": "doc", "name": "notes.txt", "mime_type": "text/plain"},
                {"file_id": "one", "name": "one.png", "mime_type": "image/png"},
                {"file_id": "two", "name": "two.png", "mime_type": "image/png"},
            ],
            historical_files=[],
            user_id="user-1",
        ),
    )
    agent.state.context.extend(
        session_to_msgs(
            [
                {"role": "user", "content": "older question"},
                {"role": "assistant", "content": "older answer"},
            ]
        )
    )
    request_seq = next_request_sequence(agent.state.context)
    current = render_context_item(
        make_text_context_item(
            "inspect the images",
            item_id=f"request:{request_seq}",
            kind=KIND_USER_INPUT,
            origin="user:chat",
            trust="user",
            created_seq=request_seq,
            priority=1_000,
            token_budget=100_000,
            truncation_policy=POLICY_NEVER,
        )
    )

    await agent.reply(current)

    blocks = [block for message in model.calls[0]["messages"] for block in message.content]
    data_blocks = [
        block
        for block in blocks
        if (block.get("type") if isinstance(block, dict) else getattr(block, "type", "")) == "data"
    ]
    assert len(data_blocks) == 1
    data_payload = (
        data_blocks[0]
        if isinstance(data_blocks[0], dict)
        else data_blocks[0].model_dump(mode="json")
    )
    assert data_payload["source"]["data"] == "A" * 4_000
    assert not any(isinstance(block, str) and '"type":"data"' in block for block in blocks)
    assert model.calls[0]["messages"][-1].get_text_content() == "inspect the images"
    assert any(
        "TEXT-FILE-EVIDENCE" in message.get_text_content()
        for message in model.calls[0]["messages"][:-1]
    )
    manifest = agent.request_context_manifest
    attachment_entries = [
        entry
        for section in ("included", "excluded")
        for entry in manifest[section]
        if entry["kind"] == "attachment"
    ]
    assert len(attachment_entries) == 4  # file evidence + image prefix + two atomic images
    assert sum(entry["action"] == "excluded" for entry in attachment_entries) == 1
